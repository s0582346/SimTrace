"""Run a stochastic model many times and report statistics across the runs.

A single `run_simulation` is one sample of a random model; to say anything
defensible about throughput or utilization you need several independent
replications and a confidence interval. `run_replications` drives that loop:

  1. Validate the replication count (2..100), read the machine's worker
     budget, and snapshot the source model's build spec.
  2. Run replication 0 in this process and time it. That measurement is the
     probe for step 3; the replication count alone does not say how long one
     run takes.
  3. Run the rest, on worker processes if `parallel.choose_workers` finds the
     remaining work worth it. Either way replication i seeds with
     `random_seed_base + i * SEED_STRIDE` and rebuilds a *fresh* model from the
     spec. The stride keeps per-run seed streams apart. The seed is set inside
     whichever process runs the replication, so a result depends on its seed
     and not on how the batch was scheduled.
  4. Sort outcomes: successful runs are tagged with `_replication_info`
     (replication number, seed, timestamp) and collected; a run that raises is
     recorded separately with its seed, and the batch continues.
  5. Guard: fewer than two successful runs raises, as there is nothing to do
     statistics on.
  6. Hand the successful runs (flattened to scalar metrics) to
     `ReplicationAnalyzer` and return its analysis with the industry summary,
     the failure log, and a note of how the batch ran.
  7. Ask `precision.precision_report` how few of those runs would have sufficed
     for each metric, and return that alongside. It reads the runs already made
     and costs nothing extra.

Only the source model's `spec` is read (the session model's by default). Its
`env`, nodes, edges, `events` and `item_paths` are never touched, so its clock
and the last `run_simulation`'s stats stay intact for the `verify_*` tools. The
spec is copied once up front, so edits made to the session model mid-batch
cannot change what later replications build. Being a list of plain dicts is
also what lets it cross a process boundary; the live model cannot.
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

from simtrace.model import FactoryModel
from simtrace.model import get_model as get_session_model
from simtrace.tools.simulation import parallel
from simtrace.tools.simulation.lifecycle import run_simulation
from simtrace.tools.simulation.precision import (
    DEFAULT_PRECISION,
    precision_report,
    validate_settings,
)
from simtrace.tools.simulation.rebuild import build_from_spec
from simtrace.tools.simulation.replication_analysis import (
    ReplicationAnalyzer,
    extract_metrics,
)
from simtrace.tools.utils import require_positive_number

# Per-run seed spacing. i * SEED_STRIDE keeps replications' RNG streams far
# enough apart that they don't overlap and correlate.
SEED_STRIDE = 1000

MIN_REPLICATIONS = 2
MAX_REPLICATIONS = 100

# The stat a Sink counts finished items in. See FactorySimPy's sink.stats.
SINK_THROUGHPUT_STAT = "num_item_received"


def sink_throughput_metrics(spec: List[Dict[str, Any]]) -> List[str]:
    """Name the throughput metric of every sink in a build spec.

    Throughput is the number a study is usually about, so this is the default
    answer to "which metrics matter here" — for the precision tables below, and
    for anything else that has to focus on a few metrics out of the dozens a run
    reports.

    Reading the spec rather than a finished run means the names are available
    before anything has run.

    Args:
        spec: a model's recorded build log ({"op", "kwargs"} per step).

    Returns:
        One `sink_id.num_item_received` per `create_sink` step, in build order.
        Empty for a model with no sink.
    """
    return [
        f"{step['kwargs']['id']}.{SINK_THROUGHPUT_STAT}"
        for step in spec
        if step["op"] == "create_sink" and "id" in step["kwargs"]
    ]


def _flatten_run(result: Dict[str, Any]) -> Dict[str, float]:
    """Flatten a run_simulation result into scalar metrics keyed `owner.stat`.

    `run_simulation` returns nested per-node and per-edge stat dicts; the
    analyzer wants a flat mapping of metric name -> scalar. Walk both the
    `nodes` and `edges` sections and emit one entry per numeric leaf stat,
    keyed like `snk.num_item_received`. Non-numeric stats (and None, for
    components that never populated a stats dict) are skipped. `bool` is an
    `int` subclass, so it is excluded explicitly.
    """
    metrics: Dict[str, float] = {}
    for section in ("nodes", "edges"):
        for owner_id, stats in result.get(section, {}).items():
            if not isinstance(stats, dict):
                continue
            for stat_name, value in stats.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                metrics[f"{owner_id}.{stat_name}"] = float(value)
    return metrics


def _run_one(
    spec: List[Dict[str, Any]], until: float, seed: int, replication: int
) -> Dict[str, Any]:
    """Run one replication and return its outcome as a value.

    This is the function shipped to a worker process, so every argument is
    picklable: a build spec of plain dicts, a float and two ints. A
    `simpy.Environment` holds live generators and cannot be pickled, so the
    model is rebuilt here from the spec.

    Seeding happens inside this call, so the result depends on `seed` and not
    on which process ran it. `build_from_spec` is looked up on the module, so
    tests can patch the rebuild seam on the in-process path.

    Returns:
        `{"replication", "seed", "metrics"}` on success, where `metrics` is the
        flat scalar mapping plus `_replication_info`; or
        `{"replication", "seed", "error"}` if the run raised. Failures are
        values, not exceptions, so one bad replication does not end the batch.
    """
    # No-op here. In a worker it keeps FactorySimPy's narration off the
    # inherited stdout, which under MCP is the JSON-RPC channel.
    parallel.silence_stdout()

    random.seed(seed)
    try:
        # A fresh model per run: re-running the source model would raise on its
        # second call (until <= clock) and share stats across runs.
        replica = build_from_spec(spec)
        result = run_simulation(until, seed=None, model=replica)
    except Exception as exc:  # one bad run doesn't kill the batch
        return {
            "replication": replication,
            "seed": seed,
            "error": f"{type(exc).__name__}: {exc}",
        }

    metrics = _flatten_run(result)
    metrics["_replication_info"] = {
        "replication": replication,
        "seed": seed,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return {"replication": replication, "seed": seed, "metrics": metrics}


def run_replications(
    until: float,
    replications: int,
    random_seed_base: int = 0,
    *,
    desired_precision: float = DEFAULT_PRECISION,
    precision_tables_for: List[str] | None = None,
    model: FactoryModel | None = None,
    n_jobs: int | None = None,
) -> Dict[str, Any]:
    """Run `replications` independent runs of the assembled model and analyze them.

    Args:
        until: simulation end time for each run; must be a positive number.
        replications: number of independent runs; must be an int in
            [2, 100].
        random_seed_base: base RNG seed. Replication i uses
            `random_seed_base + i * SEED_STRIDE`, so the whole batch is
            reproducible from this one number and the per-run streams stay well
            separated.
        desired_precision: target for the confidence interval's half-width as a
            share of the mean, used only by the `precision` section. 0.10 means
            "within +/-10% of the mean".
        precision_tables_for: metrics whose full per-run precision table to
            include. None (the default) takes every sink's throughput. Pass an
            empty list for no tables at all; one table per metric would dwarf
            the rest of the payload.
        model: the model whose build spec to replicate; defaults to the session
            model.
        n_jobs: how many worker processes to allow. None (the default) inspects
            the machine and applies the measured rule then only actually spreads the batch
            if timing the first replication says it is worth the pool's start-up cost.

    Returns:
        A dict with:
          - `analysis`: the `ReplicationAnalyzer` output (per-metric stats,
            CIs, `_replication_summary`, `_individual_replications`),
          - `summary`: the `format_industry_summary` text report,
          - `precision`: the `precision_report` output — per metric, how few of
            these runs would have reached `desired_precision`,
          - `requested_replications` / `successful_replications`,
          - `failures`: list of {replication, seed, error} for runs that raised,
          - `execution`: {workers, mode, cores_detected}.

    Raises:
        ValueError: if `until` is not a positive number, `replications` is not
            an int in [2, 100], `random_seed_base` is not an int,
            `desired_precision` is not between 0 and 1, `n_jobs` is not
            None/-1/a positive int, the model has no recorded build steps, or
            fewer than two runs succeeded.
    """
    require_positive_number("until", until)

    # bool is an int subclass; exclude it so True/False aren't taken as a count.
    if isinstance(replications, bool) or not isinstance(replications, int):
        raise ValueError(f"replications must be an int (got {replications!r}).")
    if not MIN_REPLICATIONS <= replications <= MAX_REPLICATIONS:
        raise ValueError(
            f"replications must be between {MIN_REPLICATIONS} and "
            f"{MAX_REPLICATIONS} (got {replications})."
        )

    if isinstance(random_seed_base, bool) or not isinstance(random_seed_base, int):
        raise ValueError(
            f"random_seed_base must be an int (got {random_seed_base!r})."
        )

    # Checked here rather than where it is used, at the end: a typo'd target
    # should not surface only after the whole batch has run.
    validate_settings(desired_precision)

    # Ask what the machine allows before running anything
    budget = parallel.worker_budget(n_jobs)

    source = model if model is not None else get_session_model()

    # Snapshot the spec now so later edits to the source model can't affect this batch half-way through.
    spec = list(source.spec)
    if not spec:
        raise ValueError(
            "The model is empty: create nodes and edges (and connect them) "
            "before running replications."
        )

    seeds = [random_seed_base + i * SEED_STRIDE for i in range(replications)]

    # run replication 0 here and time it 
    started = time.perf_counter()
    outcomes = [_run_one(spec, until, seeds[0], 0)]
    seconds_each = time.perf_counter() - started

    pending = [(spec, until, seeds[i], i) for i in range(1, replications)]
    if n_jobs is None:
        # spread the batch only if the probe says it pays for itself.
        workers = parallel.choose_workers(len(pending), seconds_each, budget)
    else:
        # The caller named a count, so use it rather than second-guess it
        workers = min(budget, len(pending))

    if workers > 1:
        outcomes.extend(parallel.run_on_workers(_run_one, pending, workers))
    else:
        outcomes.extend(_run_one(*task) for task in pending)

    # Outcomes are in replication order on both paths, so `failures` and
    # `_individual_replications` read the same either way.
    successful: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    for outcome in outcomes:
        if "error" in outcome:
            failures.append(outcome)
        else:
            successful.append(outcome["metrics"])

    if len(successful) < MIN_REPLICATIONS:
        raise ValueError(
            f"Only {len(successful)} of {replications} replications succeeded; "
            f"need at least {MIN_REPLICATIONS} successful runs for statistics. "
            f"Failures: {failures}"
        )

    analyzer = ReplicationAnalyzer()
    analysis = analyzer.analyze_replications(successful)
    summary = analyzer.format_industry_summary(analysis)

    # Same metric set the analysis covers, in run order — the order is what the
    # confidence interval method walks.
    # `is None` rather than a falsy test: an empty list is a caller asking for
    # no tables, not for the default.
    tables_for = (
        sink_throughput_metrics(spec)
        if precision_tables_for is None
        else precision_tables_for
    )
    precision = precision_report(
        extract_metrics(successful),
        desired_precision=desired_precision,
        tables_for=tables_for,
    )

    return {
        "analysis": analysis,
        "summary": summary,
        "precision": precision,
        "requested_replications": replications,
        "successful_replications": len(successful),
        "failures": failures,
        "execution": parallel.describe(workers),
    }
