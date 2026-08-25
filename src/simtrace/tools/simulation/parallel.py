"""Spread a replication batch across worker processes.

Replications are independent, so a batch can run on several cores. This module
decides how many, and whether to use them at all.

**How many.** Half the logical cores, capped at eight, capped again at the work
left. Measured on 16 physical / 22 logical cores: past eight workers the wall
clock barely moved while CPU time kept rising. See `benchmarks/METHODIK.md`
section 3.

**Whether.** A pool costs 1.5 s to start. On a small model that exceeds the
whole batch. The replication count does not say how heavy one run is, so the
caller times one replication and passes the measurement to `choose_workers`.

Processes, not threads. SimPy is pure Python and CPU-bound, so threads would
serialise on the GIL. Components draw from the global `random` module, so two
replications in one interpreter would consume each other's numbers. Separate
processes each hold their own module state. `benchmarks/STEP2.md` checks the
result: identical metrics at every worker count.
"""

from __future__ import annotations

import math
import multiprocessing
import os
import sys
from typing import Any, Callable, List, Sequence

# Worker ceiling. Past this the wall clock stops improving.
MAX_WORKERS = 8

# Hard ceiling on every path, including an explicit n_jobs. 1 forces sequential.
MAX_WORKERS_ENV = "SIMTRACE_MAX_WORKERS"

# joblib's convention for n_jobs: every detected core.
ALL_CORES = -1

# Pool start-up. Cold spawns interpreters and imports simtrace in each; warm
# reuses loky's executor from an earlier batch. Measured at 1.51 s and 0.01 s
# for eight workers, rounded up. Changing the worker count respawns the pool at
# roughly the cold price; that is not modelled here.
COLD_POOL_SECONDS = 2.0
WARM_POOL_SECONDS = 0.15

# A replication is slower in a worker than in this process even at one worker.
# Measured at 0.77 s against 0.635 s. The probe's timing is scaled by this.
WORKER_PENALTY = 1.2

# Minimum estimated gain before a batch is spread.
MIN_SPEEDUP = 1.2

# Set once a pool exists in this process, so later batches use the warm price.
_pool_started = False

# Set once this worker's stdout points at a sink. See silence_stdout.
_stdout_silenced = False


def silence_stdout() -> None:
    """Point a worker's stdout at devnull, permanently. No-op in the main process.

    Workers inherit the parent's stdout. Under the MCP stdio transport that is
    the JSON-RPC channel, and one stray line corrupts it. `run_simulation`
    redirects stdout during a run, but FactorySimPy also prints from generator
    cleanup, which runs after that redirect is undone. This closes the gap.
    `traced_stdout` still swaps its own buffer in and out on top of this.

    A replication's result comes from the stats it returns, not the narration.
    """
    global _stdout_silenced

    if _stdout_silenced or multiprocessing.parent_process() is None:
        return
    # devnull rather than a buffer: a worker outlives many replications, and
    # anything that accumulates their narration would grow without bound.
    sys.stdout = open(os.devnull, "w")
    _stdout_silenced = True


def detect_cores() -> int:
    """Logical cores this process may use.

    Not `os.cpu_count()`: that reports the machine's processors and ignores a
    CPU affinity mask and a container's CPU quota. joblib's counter reads cgroup
    limits, the affinity mask and `LOKY_MAX_CPU_COUNT`. The stdlib calls below
    are the fallback for when joblib cannot be imported.

    Returns:
        A count of at least 1.
    """
    try:
        from joblib import cpu_count

        return max(1, cpu_count())
    except Exception:
        pass

    # 3.13+, and affinity-aware where the OS exposes a mask.
    process_cpu_count = getattr(os, "process_cpu_count", None)
    if process_cpu_count is not None:
        counted = process_cpu_count()
        if counted:
            return max(1, counted)

    # Linux before 3.13.
    sched_getaffinity = getattr(os, "sched_getaffinity", None)
    if sched_getaffinity is not None:
        return max(1, len(sched_getaffinity(0)))

    return max(1, os.cpu_count() or 1)


def _env_ceiling() -> int | None:
    """Read `SIMTRACE_MAX_WORKERS`, or None when it is unset or blank.

    Raises:
        ValueError: if it holds anything but a positive integer. A typo would
            otherwise pass silently and return the default budget.
    """
    raw = os.environ.get(MAX_WORKERS_ENV, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ValueError(
            f"{MAX_WORKERS_ENV} must be a positive integer (got {raw!r})."
        )
    return value


def worker_budget(n_jobs: int | None = None) -> int:
    """The most workers this machine should be asked for.

    A ceiling, not a decision. `choose_workers` makes the decision.

    Args:
        n_jobs: None to apply the measured rule (half the logical cores, capped
            at `MAX_WORKERS`); `ALL_CORES` (-1) for every detected core; or a
            positive int to ask for that many outright. `SIMTRACE_MAX_WORKERS`
            caps all three.

    Returns:
        A worker count of at least 1. 1 means run sequentially.

    Raises:
        ValueError: if `n_jobs` is not None, -1, or a positive int, or if
            `SIMTRACE_MAX_WORKERS` holds a non-positive-integer value.
    """
    cores = detect_cores()

    if n_jobs is None:
        # Half the logical cores approximates the physical ones on a
        # hyperthreaded chip.
        budget = min(cores // 2, MAX_WORKERS)
    elif isinstance(n_jobs, bool) or not isinstance(n_jobs, int):
        raise ValueError(f"n_jobs must be an int or None (got {n_jobs!r}).")
    elif n_jobs == ALL_CORES:
        budget = cores
    elif n_jobs < 1:
        raise ValueError(
            f"n_jobs must be >= 1, or {ALL_CORES} for every core (got {n_jobs})."
        )
    else:
        budget = n_jobs

    ceiling = _env_ceiling()
    if ceiling is not None:
        budget = min(budget, ceiling)

    # A single-core machine, or `cores // 2` rounding to zero on two.
    return max(1, budget)


def choose_workers(
    pending: int,
    seconds_each: float,
    budget: int,
    *,
    startup_seconds: float | None = None,
) -> int:
    """Estimate both paths for `pending` more replications and pick one.

    Speedup is modelled as the square root of the worker count. This is a fit to
    the Step 2 measurements: 1.23x, 1.93x and 2.75x at 2, 4 and 8 workers,
    against sqrt's 1.41, 2.00 and 2.83. It is sublinear because one replication
    slows down as workers are added, from 0.705 s at one worker to 1.814 s at
    eight. That is contention between replications, not a serial section.

    The estimate leans towards staying sequential: `WORKER_PENALTY` inflates the
    measured time, the pool price is rounded up, and `MIN_SPEEDUP` requires a
    margin.

    Args:
        pending: replications still to run.
        seconds_each: measured time of one replication.
        budget: the ceiling from `worker_budget`.
        startup_seconds: what a pool costs before it runs anything. Defaults to
            the cold figure, or the warm one once a pool exists in this process.

    Returns:
        The worker count to use, or 1 to stay sequential.
    """
    if pending < 2 or budget < 2 or seconds_each <= 0:
        return 1

    workers = min(budget, pending)
    if workers < 2:
        return 1

    if startup_seconds is None:
        startup_seconds = WARM_POOL_SECONDS if _pool_started else COLD_POOL_SECONDS

    sequential = pending * seconds_each
    parallel = startup_seconds + sequential * WORKER_PENALTY / math.sqrt(workers)

    return workers if sequential >= parallel * MIN_SPEEDUP else 1


def run_on_workers(
    fn: Callable[..., Any], tasks: Sequence[Sequence[Any]], workers: int
) -> List[Any]:
    """Run `fn(*task)` for every task on `workers` processes, in task order.

    Every argument must pickle. A `simpy.Environment` holds live generators and
    cannot, so the caller passes a build spec of plain dicts and the worker
    rebuilds from it, at under one percent of a replication's cost.

    Results come back in submission order. `fn` must return failures as values
    rather than raise, so one bad replication does not end the batch.
    """
    global _pool_started

    from joblib import Parallel, delayed

    results = Parallel(n_jobs=workers, backend="loky")(
        delayed(fn)(*task) for task in tasks
    )
    # loky keeps the executor alive, so the next batch pays the warm price.
    _pool_started = True
    return list(results)


def describe(workers: int) -> dict:
    """A JSON-safe record of how a batch ran."""
    return {
        "workers": workers,
        "mode": "parallel" if workers > 1 else "sequential",
        "cores_detected": detect_cores(),
    }
