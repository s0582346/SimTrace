"""How many replications a batch would have needed, read off the batch itself.

`run_replications` reports the precision the requested run count happened to
reach. This module answers the other question: how few runs would have been
enough? It applies the **confidence interval method** — walk the replications in
order, keep a running mean and confidence interval, and take the first run count
whose half-width sits at or below a target share of the mean and never rises
above it again within the data.

The method is `sim_tools.output_analysis.confidence_interval_method`, from Hoad,
Robinson & Davies (2010). We call it once per metric with that metric's values
in run order and translate the result into plain JSON.

`sim_tools` is imported inside the functions. It pulls in pandas and plotly, and
every cold worker pool imports this package once per process.
"""

from __future__ import annotations

import warnings
from typing import Any, Dict, Iterable, List, Mapping, Sequence

import numpy as np

from simtrace.tools.simulation.replication_analysis import _sanitize

# Target share of the mean for the confidence interval's half-width. 10% is
# sim-tools' default and the usual figure in the literature.
DEFAULT_PRECISION = 0.10

# Significance level: a 95% confidence interval, matching the `ci_95` that
# `ReplicationAnalyzer` already reports.
DEFAULT_ALPHA = 0.05

# Runs skipped before the target is tested. Early intervals swing wildly on two
# or three points, and without this the method would seize on a lucky flat pair.
# sim-tools' own default; a batch of this size or smaller cannot be assessed at
# all, because nothing is left after the skip.
MIN_ASSESSABLE = 5

# Two points is the least the running statistics can start from.
MIN_VALUES = 2

# Decimal places kept in the returned per-run table. sim-tools rounds to 2 by
# default, which flattens a deviation of 0.0413 to 0.04; the run count is picked
# before the rounding either way, so this only affects what is displayed.
TABLE_DECIMALS = 6


def _records(frame: Any) -> List[Dict[str, Any]]:
    """Turn a sim-tools summary frame into a list of plain JSON row dicts.

    The frame is indexed by replication number (1..n) under the name
    `replications`; that index becomes a field of each row so the rows stand on
    their own. Values are forced to native floats and `nan` becomes None, which is what
    the first rows of every metric hold before a standard deviation exists.
    """
    rows: List[Dict[str, Any]] = []
    for index, row in frame.iterrows():
        record: Dict[str, Any] = {"replications": int(index)}
        for column, value in row.items():
            record[str(column)] = float(value)
        rows.append(record)
    # inf/nan -> None, so the payload is strict JSON.
    return _sanitize(rows)


def validate_settings(
    desired_precision: float,
    alpha: float = DEFAULT_ALPHA,
    min_replications: int = MIN_ASSESSABLE,
) -> None:
    """Reject settings that the method cannot act on.

    Callers that run simulations first should call this up front, so a typo'd
    target does not surface only after a whole batch has run.

    Raises:
        ValueError: if `desired_precision` or `alpha` is not a number strictly
            between 0 and 1, or `min_replications` is not a non-negative int.
    """
    for name, value in (("desired_precision", desired_precision), ("alpha", alpha)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a number (got {value!r}).")
        if not 0 < value < 1:
            raise ValueError(f"{name} must be between 0 and 1 (got {value}).")

    if isinstance(min_replications, bool) or not isinstance(min_replications, int):
        raise ValueError(
            f"min_replications must be an int (got {min_replications!r})."
        )
    if min_replications < 0:
        raise ValueError(
            f"min_replications must be 0 or more (got {min_replications})."
        )


def precision_report(
    values_by_metric: Mapping[str, Sequence[float]],
    *,
    desired_precision: float = DEFAULT_PRECISION,
    alpha: float = DEFAULT_ALPHA,
    min_replications: int = MIN_ASSESSABLE,
    tables_for: Iterable[str] = (),
) -> Dict[str, Any]:
    """Report, per metric, the fewest replications that reach `desired_precision`.

    Args:
        values_by_metric: metric name, e.g. `snk.num_item_received`, -> that
            metric's values in run order. A metric with fewer than two values is
            skipped; there is nothing to build an interval from.
        desired_precision: target for the half-width as a share of the mean.
            0.10 means "within +/-10% of the mean".
        alpha: significance level; 0.05 is a 95% interval.
        min_replications: runs skipped before the target is tested.
        tables_for: metrics whose full per-run table to include. Empty by
            default: one table per metric, on a model with dozens of metrics,
            would dwarf everything else in the payload.

    Returns:
        `settings` (the three numbers above), and `metrics`: per metric,
        `replications_needed` (an int, or None when the target is never
        reached), `reached`, `final_deviation` (the half-width share at the last
        run), and `table` for the metrics named in `tables_for`. When the batch
        is too short to assess, `metrics` is empty and `note` says so.

    Raises:
        ValueError: if a setting is out of range (see `_validate`).
    """
    validate_settings(desired_precision, alpha, min_replications)

    settings = {
        "desired_precision": desired_precision,
        "confidence_level": 1 - alpha,
        "min_replications": min_replications,
    }

    # A metric needs two values before a running mean and variance exist.
    usable = {
        name: list(values)
        for name, values in values_by_metric.items()
        if len(values) >= MIN_VALUES
    }

    # Every run after the skip is a candidate; with none left there is no
    # candidate to pick and sim-tools would report every metric as unreached.
    longest = max((len(v) for v in usable.values()), default=0)
    if longest <= min_replications:
        return {
            "settings": settings,
            "metrics": {},
            "note": (
                f"{longest} replications is too few to assess precision: the "
                f"method ignores the first {min_replications}. Run more."
            ),
        }

    wanted_tables = set(tables_for)
    metrics: Dict[str, Any] = {}

    from sim_tools.output_analysis import confidence_interval_method

    for name, values in usable.items():
        # Two kinds of noise to keep off stderr, which under the MCP stdio
        # transport shares a console with the JSON-RPC channel:
        #   - sim-tools warns once per metric that never reaches the target,
        #     which is what `reached` already says;
        #   - a metric that stays at zero divides the half-width by a zero mean.
        #     That is a legitimate "no precision to speak of", and it arrives
        #     here as the nan that `_sanitize` maps to None.
        with warnings.catch_warnings(), np.errstate(invalid="ignore", divide="ignore"):
            warnings.simplefilter("ignore", UserWarning)
            n_reps, frame = confidence_interval_method(
                values,
                alpha=alpha,
                desired_precision=desired_precision,
                min_rep=min_replications,
                decimal_places=TABLE_DECIMALS,
            )

        # -1 is sim-tools' "never reached".
        reached = int(n_reps) != -1
        entry: Dict[str, Any] = {
            "replications_needed": int(n_reps) if reached else None,
            "reached": reached,
            "final_deviation": float(frame["% deviation"].iloc[-1]),
        }
        if name in wanted_tables:
            entry["table"] = _records(frame)
        metrics[name] = entry

    return {"settings": settings, "metrics": _sanitize(metrics)}
