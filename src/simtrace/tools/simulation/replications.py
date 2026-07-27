"""Run a stochastic model many times and report statistics across the runs.

A single `run_simulation` is one sample of a random model; to say anything
defensible about throughput or utilization you need several independent
replications and a confidence interval. `run_replications` drives that loop:

  1. Validate the replication count (2..100).
  2. For each replication i, set a deterministic, well-separated seed
     (`random_seed_base + i * 1000`), call `random.seed(...)`, build a *fresh*
     model via the caller's `build_model()` factory, and run it once. The
     i * 1000 spacing keeps per-run seed streams far apart so replications are
     effectively independent; a fresh model per run means no SimPy state bleeds
     between replications.
  3. Sort outcomes: successful runs are tagged with `_replication_info`
     (replication number, seed, timestamp) and collected; a run that raises is
     recorded separately with its seed, and the loop keeps going (one bad run
     does not kill the batch).
  4. Guard: with fewer than two successful runs there is nothing to do
     statistics on, so raise.
  5. Hand the successful runs (flattened to scalar metrics) to
     `ReplicationAnalyzer` and return its analysis alongside the industry
     summary and the failure log.

`build_model` is a zero-arg callable returning a freshly assembled
`FactoryModel` (e.g. a helper that runs the create_*/connect calls on a new
model). It is intentionally a callable rather than a config dict: this repo has
no config->model builder yet, so a factory is the honest way to get genuinely
independent models per replication. Once a config->model builder exists, a thin
MCP wrapper can adapt a JSON config into such a factory.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List

from simtrace.model import FactoryModel
from simtrace.tools.simulation.lifecycle import run_simulation
from simtrace.tools.simulation.replication_analysis import ReplicationAnalyzer
from simtrace.tools.utils import require_positive_number

# Per-run seed spacing. i * SEED_STRIDE keeps replications' RNG streams far
# enough apart that they don't overlap and correlate.
SEED_STRIDE = 1000

MIN_REPLICATIONS = 2
MAX_REPLICATIONS = 100


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


def run_replications(
    build_model: Callable[[], FactoryModel],
    until: float,
    replications: int,
    random_seed_base: int = 0,
) -> Dict[str, Any]:
    """Run `replications` independent runs of a fresh model and analyze them.

    Args:
        build_model: zero-arg factory returning a freshly assembled, unrun
            `FactoryModel`. Called once per replication so no SimPy state is
            shared between runs.
        until: simulation end time for each run; must be a positive number.
        replications: number of independent runs; must be an int in
            [2, 100]. Fewer than 2 leaves nothing to do statistics on; more
            than 100 is disallowed to bound the work per call.
        random_seed_base: base RNG seed. Replication i uses
            `random_seed_base + i * 1000`, so the whole batch is reproducible
            from this one number and the per-run streams stay well separated.

    Returns:
        A dict with:
          - `analysis`: the `ReplicationAnalyzer` output (per-metric stats,
            CIs, `_replication_summary`, `_individual_replications`),
          - `summary`: the `format_industry_summary` text report,
          - `requested_replications` / `successful_replications`,
          - `failures`: list of {replication, seed, error} for runs that raised.

    Raises:
        ValueError: if `until` is not a positive number, `replications` is not
            an int in [2, 100], `random_seed_base` is not an int, or fewer than
            two runs succeeded.
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

    successful: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []

    for i in range(replications):
        seed = random_seed_base + i * SEED_STRIDE
        # Seed the global RNG here (per the replication contract) and let
        # run_simulation run the model without reseeding again.
        random.seed(seed)
        try:
            model = build_model()
            result = run_simulation(until, seed=None, model=model)
        except Exception as exc:  # one bad run doesn't kill the batch
            failures.append(
                {"replication": i, "seed": seed, "error": f"{type(exc).__name__}: {exc}"}
            )
            continue

        flat = _flatten_run(result)
        flat["_replication_info"] = {
            "replication": i,
            "seed": seed,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        successful.append(flat)

    if len(successful) < MIN_REPLICATIONS:
        raise ValueError(
            f"Only {len(successful)} of {replications} replications succeeded; "
            f"need at least {MIN_REPLICATIONS} successful runs for statistics. "
            f"Failures: {failures}"
        )

    analyzer = ReplicationAnalyzer()
    analysis = analyzer.analyze_replications(successful)
    summary = analyzer.format_industry_summary(analysis)

    return {
        "analysis": analysis,
        "summary": summary,
        "requested_replications": replications,
        "successful_replications": len(successful),
        "failures": failures,
    }
