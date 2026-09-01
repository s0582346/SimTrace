"""Let the model decide how many replications it needs, while it runs.

`run_replications` runs a count you pick. `precision` tells you afterwards
whether that count was enough. This module removes the guess: it keeps running
replications until the confidence interval's half-width is within a target share
of the mean **and stays there** through a look-ahead window, then stops.

That is the *Replications Algorithm* of Hoad, Robinson & Davies (2010), which
`sim_tools.output_analysis.ReplicationsAlgorithm` implements. Two pieces meet
here:

  1. `_ChunkedRunner` — the `single_run(replication_no)` adapter sim-tools asks
     for. The algorithm wants one run at a time; our replications are worth
     spreading over cores. The runner resolves that by running a whole chunk
     ahead of the question and answering from a cache, so sim-tools' sequential
     logic is untouched and the worker pool still earns its keep. The cost is
     overshoot: up to one chunk of runs more than strictly needed. The count it
     reports is still the exact one the rule picks.
  2. `find_replication_count` — validation, the target metrics, and the
     translation of a pandas summary frame into plain JSON.

The look-ahead is the point of the method. A running interval can dip below the
target by luck on one replication and climb back out on the next; stopping at
the dip reports a precision the model does not have. Nothing is accepted until
the target has held for the whole window.

Only the source model's `spec` is read, as in `replications` — its `env`, nodes,
edges and stats stay untouched, so the `verify_*` tools still refer to the last
`run_simulation`.
"""

from __future__ import annotations

import time
import warnings
from typing import Any, Dict, Iterable, List, Sequence

import numpy as np

from simtrace.model import FactoryModel
from simtrace.model import get_model as get_session_model
from simtrace.tools.simulation import parallel
from simtrace.tools.simulation.precision import (
    DEFAULT_ALPHA,
    DEFAULT_PRECISION,
    records,
    validate_settings,
)
from simtrace.tools.simulation.replications import (
    MAX_REPLICATIONS,
    MIN_REPLICATIONS,
    SEED_STRIDE,
    _run_one,
    sink_throughput_metrics,
)
from simtrace.tools.utils import require_positive_number

# Runs made before the target is first tested. sim-tools' default.
DEFAULT_INITIAL_REPLICATIONS = 3

# How long the target must hold before the search accepts it. sim-tools' default
# of 5 is the window Hoad et al. recommend.
DEFAULT_LOOK_AHEAD = 5

# Where the search gives up. Deliberately well under MAX_REPLICATIONS: a safety
# net for a model that will never settle, not a count to aim at.
DEFAULT_BUDGET = 50


class _ChunkedRunner:
    """Answer sim-tools' one-run-at-a-time questions from parallel chunks.

    sim-tools calls `single_run(0)`, `single_run(1)`, ... in order. Running them
    one at a time would leave every core but one idle. On a miss this runs
    `chunk_size` consecutive replications at once and caches them, so the
    algorithm's next few questions are already answered.

    Seeding is unchanged from `run_replications`: replication i uses
    `seed_base + i * SEED_STRIDE`, set inside whichever process runs it. A
    replication's result depends on its seed alone, so the same search replays
    exactly whatever the chunking and the worker count did.

    Attributes:
        runs: replications actually executed, including any chunk overshoot.
        workers: the worker count in use, or None before the first chunk.
    """

    def __init__(
        self,
        spec: List[Dict[str, Any]],
        until: float,
        seed_base: int,
        metrics: Sequence[str],
        chunk_size: int,
        n_jobs: int | None,
    ) -> None:
        self._spec = spec
        self._until = until
        self._seed_base = seed_base
        self._metrics = list(metrics)
        self._chunk_size = max(1, chunk_size)
        self._n_jobs = n_jobs
        self._budget = parallel.worker_budget(n_jobs)
        self._cache: Dict[int, Dict[str, float]] = {}
        self.runs = 0
        self.workers: int | None = None

    def _seed(self, replication: int) -> int:
        return self._seed_base + replication * SEED_STRIDE

    def single_run(self, replication_no: int) -> Dict[str, float]:
        """Return one replication's target metrics, running it if not cached."""
        if replication_no not in self._cache:
            self._fill_from(replication_no)
        return self._cache[replication_no]

    def _fill_from(self, start: int) -> None:
        """Run `chunk_size` replications from `start` and cache their metrics."""
        if self.workers is None:
            # The first replication doubles as the probe: the chunk size says
            # how many runs are coming, never how heavy one of them is.
            started = time.perf_counter()
            first = _run_one(self._spec, self._until, self._seed(start), start)
            seconds_each = time.perf_counter() - started
            self._store([first])
            self._decide_workers(seconds_each)
            start += 1

        pending = [
            (self._spec, self._until, self._seed(i), i)
            for i in range(start, start + self._chunk_size)
            if i not in self._cache
        ]
        if not pending:
            return

        if self.workers and self.workers > 1:
            self._store(parallel.run_on_workers(_run_one, pending, self.workers))
        else:
            self._store(_run_one(*task) for task in pending)

    def _decide_workers(self, seconds_each: float) -> None:
        """Price one chunk once, then keep that answer for every later chunk.

        Re-pricing per chunk would let the worker count flip mid-search, and
        changing it respawns the pool at roughly the cold price.
        """
        pending = self._chunk_size - 1
        if self._n_jobs is None:
            self.workers = parallel.choose_workers(
                pending, seconds_each, self._budget
            )
        else:
            # The caller named a count, so use it rather than second-guess it.
            self.workers = max(1, min(self._budget, pending))

    def _store(self, outcomes: Iterable[Dict[str, Any]]) -> None:
        """Cache each outcome's target metrics.

        Raises:
            ValueError: if a replication raised, or does not report one of the
                target metrics. Either leaves a hole in a sequence the algorithm
                reads in order, and unlike a fixed-size batch there is no
                sensible way to carry on around it.
        """
        for outcome in outcomes:
            self.runs += 1
            if "error" in outcome:
                raise ValueError(
                    f"Replication {outcome['replication']} (seed "
                    f"{outcome['seed']}) failed, so the search cannot continue: "
                    f"{outcome['error']}"
                )

            metrics = outcome["metrics"]
            missing = [name for name in self._metrics if name not in metrics]
            if missing:
                available = sorted(
                    key for key in metrics if not key.startswith("_")
                )
                raise ValueError(
                    f"Replication {outcome['replication']} reports no "
                    f"{missing}. Available metrics: {available}."
                )

            self._cache[outcome["replication"]] = {
                name: metrics[name] for name in self._metrics
            }


def _validate_counts(
    initial_replications: int,
    look_ahead: int,
    replication_budget: int,
    chunk_size: int | None,
) -> None:
    """Reject counts the search cannot run.

    Raises:
        ValueError: if any is not a non-negative int, `chunk_size` is not a
            positive int, the budget is outside [MIN_REPLICATIONS,
            MAX_REPLICATIONS], or the budget is below the initial run count.
    """
    counts = (
        ("initial_replications", initial_replications),
        ("look_ahead", look_ahead),
        ("replication_budget", replication_budget),
    )
    for name, value in counts:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an int (got {value!r}).")
        if value < 0:
            raise ValueError(f"{name} must be 0 or more (got {value}).")

    if chunk_size is not None and (
        isinstance(chunk_size, bool)
        or not isinstance(chunk_size, int)
        or chunk_size < 1
    ):
        raise ValueError(f"chunk_size must be a positive int (got {chunk_size!r}).")

    # Capped at what run_replications accepts, so the search cannot recommend a
    # count the tool that acts on it would refuse.
    if not MIN_REPLICATIONS <= replication_budget <= MAX_REPLICATIONS:
        raise ValueError(
            f"replication_budget must be between {MIN_REPLICATIONS} and "
            f"{MAX_REPLICATIONS} (got {replication_budget})."
        )
    if replication_budget < initial_replications:
        raise ValueError(
            f"replication_budget ({replication_budget}) must be at least "
            f"initial_replications ({initial_replications})."
        )


def find_replication_count(
    until: float,
    target_metrics: Sequence[str] | None = None,
    *,
    desired_precision: float = DEFAULT_PRECISION,
    alpha: float = DEFAULT_ALPHA,
    initial_replications: int = DEFAULT_INITIAL_REPLICATIONS,
    look_ahead: int = DEFAULT_LOOK_AHEAD,
    replication_budget: int = DEFAULT_BUDGET,
    random_seed_base: int = 0,
    model: FactoryModel | None = None,
    n_jobs: int | None = None,
    chunk_size: int | None = None,
    plot_path: str | None = None,
) -> Dict[str, Any]:
    """Search for the fewest replications that hold `desired_precision`.

    Args:
        until: simulation end time for each run; must be a positive number.
        target_metrics: the metrics that must reach the target, named
            `owner_id.stat` (e.g. `snk.num_item_received`). None takes every
            sink's throughput. All of them must settle before the search stops.
        desired_precision: target for the half-width as a share of the mean.
            0.10 means "within +/-10% of the mean".
        alpha: significance level; 0.05 is a 95% interval.
        initial_replications: runs made before the target is first tested.
        look_ahead: how many further runs the target must hold for. 0 accepts
            the first run that meets it, which is what the method exists to
            avoid.
        replication_budget: where the search gives up, in [2, 100]. A few more
            runs than this can still be made: the look-ahead window has to be
            filled before a count can be refused.
        random_seed_base: base RNG seed. Replication i uses
            `random_seed_base + i * SEED_STRIDE`, so a search replays exactly.
        model: the model whose build spec to replicate; defaults to the session
            model.
        n_jobs: worker processes to allow, as in `run_replications`.
        chunk_size: replications to run ahead of the algorithm's next question.
            None covers the whole look-ahead window. It trades overshoot against
            how much of the search can run in parallel, and changes nothing
            about the counts reported: a replication's result depends on its
            seed alone.
        plot_path: where to write the precision plot. None writes nothing. With
            several metrics the metric name is appended to the stem.

    Returns:
        A dict with:
          - `replications_needed`: metric -> count, or None where the target was
            never held,
          - `converged`: whether every target metric settled,
          - `unresolved`: the metrics that did not. A metric that stays at zero
            is always here: a half-width has no share of a zero mean,
          - `runs_executed`: replications actually run, including overshoot,
          - `tables`: per metric, the running mean, interval and deviation per
            run,
          - `settings`, `execution`, `plot_paths`.

    Raises:
        ValueError: if an argument is out of range, the model has no recorded
            build steps, no target metric can be determined, or a replication
            failed (see `_ChunkedRunner._store`).
    """
    require_positive_number("until", until)
    validate_settings(desired_precision, alpha)
    _validate_counts(
        initial_replications, look_ahead, replication_budget, chunk_size
    )

    if isinstance(random_seed_base, bool) or not isinstance(random_seed_base, int):
        raise ValueError(
            f"random_seed_base must be an int (got {random_seed_base!r})."
        )

    source = model if model is not None else get_session_model()

    # Snapshot the spec, so edits to the session model mid-search cannot change
    # what later replications build.
    spec = list(source.spec)
    if not spec:
        raise ValueError(
            "The model is empty: create nodes and edges (and connect them) "
            "before searching for a replication count."
        )

    metrics = (
        sink_throughput_metrics(spec)
        if target_metrics is None
        else list(target_metrics)
    )
    if not metrics:
        raise ValueError(
            "No target metric to converge on: the model has no sink, so name "
            "the metrics explicitly (e.g. ['mach.num_item_processed'])."
        )

    runner = _ChunkedRunner(
        spec=spec,
        until=until,
        seed_base=random_seed_base,
        metrics=metrics,
        # By default one chunk covers the whole look-ahead window, so a target
        # that holds is confirmed from runs that were already made.
        chunk_size=look_ahead + 1 if chunk_size is None else chunk_size,
        n_jobs=n_jobs,
    )

    from sim_tools.output_analysis import ReplicationsAlgorithm

    algorithm = ReplicationsAlgorithm(
        alpha=alpha,
        half_width_precision=desired_precision,
        initial_replications=initial_replications,
        look_ahead=look_ahead,
        replication_budget=replication_budget,
        verbose=False,
    )

    # The same two kinds of noise as in `precision`: sim-tools warns about
    # metrics that never reach the target, which `unresolved` already reports,
    # and a metric stuck at zero divides its half-width by a zero mean.
    with warnings.catch_warnings(), np.errstate(invalid="ignore", divide="ignore"):
        warnings.simplefilter("ignore", UserWarning)
        needed, frame = algorithm.select(runner, metrics)

    # sim-tools drops the replication number from the combined frame, and each
    # metric stops being recorded once it settles, so the groups differ in
    # length. Numbering within each group puts the run number back.
    tables = {
        str(name): records(group, number_rows=True, drop=("metric",))
        for name, group in frame.groupby("metric", sort=False)
    }

    needed = {
        name: (int(value) if value is not None else None)
        for name, value in needed.items()
    }
    unresolved = sorted(name for name, value in needed.items() if value is None)

    return {
        "replications_needed": needed,
        "converged": not unresolved,
        "unresolved": unresolved,
        "runs_executed": runner.runs,
        "tables": tables,
        "settings": {
            "desired_precision": desired_precision,
            "confidence_level": 1 - alpha,
            "initial_replications": initial_replications,
            "look_ahead": look_ahead,
            "replication_budget": replication_budget,
            "until": until,
            "random_seed_base": random_seed_base,
        },
        "execution": parallel.describe(runner.workers or 1),
        "plot_paths": _write_plots(plot_path, needed, tables),
    }


def _write_plots(
    plot_path: str | None,
    needed: Dict[str, int | None],
    tables: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, str]:
    """Write one plot per metric, or nothing when no path was given."""
    if not plot_path:
        return {}

    from simtrace.tools.simulation.replication_plot import write_precision_plots

    return write_precision_plots(tables, needed, plot_path)
