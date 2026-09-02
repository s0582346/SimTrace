"""The fixed-value test: does the model produce the number it should?

**The twin.** Every distribution is replaced by its mean: `uniform(4, 6)`
becomes 5, `exp(3)` becomes 3, and "pick an out-edge at random" becomes "take
them in turns". What is left is the same plant with the randomness taken out.
Nothing of the session model is touched; the twin is built from a copy of the
build log.

**The two answers.** `analytic.expected_throughput` works out what that twin
must produce, from the wiring and the parameters alone. Then the twin is run,
and what it produced is read off. The two have to agree. Where they do not, the
plant is wired or parameterised differently from how it was described, and the
report names the stations so the difference can be traced.

**Reading the run.** A line has to fill before it delivers, so counting
everything the run produced and dividing by its length always lands a few
percent low. Instead only the departures after a warm-up point are counted, over
the length of that window. On a deterministic line that gives the exact rate.
The window is accurate to one item either way, so the tolerance is never
tighter than one item's worth of it.

**The second finding.** The deterministic throughput is also a ceiling for the
stochastic one: with finite buffers, variation costs throughput and never adds
any. So a replication mean sitting above it is not believable. Hand the means
from `run_replications` to `replication_means` and that comparison is made too,
out of the same arithmetic.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from simtrace.model import FactoryModel
from simtrace.model import get_model as get_session_model
from simtrace.tools.distributions import mean_of, spec_parts
from simtrace.tools.simulation.lifecycle import run_simulation
from simtrace.tools.simulation.rebuild import build_from_spec
from simtrace.tools.utils import require_positive_number
from simtrace.tools.validation.analytic import expected_throughput

# Deviation allowed between the calculated and the measured rate, as a share of
# the calculated one. The measured rate on a deterministic line is exact, so
# this only absorbs the rounding of a window boundary.
DEFAULT_TOLERANCE = 0.01

# Departures needed in the window before the measured rate means anything.
MIN_WINDOW_ITEMS = 5

# The stat a Sink counts finished items in. See FactorySimPy's sink.stats.
SINK_THROUGHPUT_STAT = "num_item_received"

# Build-spec fields that may hold a distribution string, per build step.
_DELAY_FIELDS: Dict[str, Tuple[str, ...]] = {
    "create_source": ("inter_arrival_time",),
    "create_machine": ("processing_delay",),
    "create_splitter": ("processing_delay",),
    "create_combiner": ("processing_delay",),
    "create_buffer": ("delay",),
    "create_fleet": ("delay", "transit_delay"),
}

# Picking an edge at random is a distribution too. Taking them in turns is the
# deterministic version of the same even split.
_SELECTION_FIELDS = ("in_edge_selection", "out_edge_selection")
_RANDOM = "RANDOM"
_ROUND_ROBIN = "ROUND_ROBIN"

# Below this many standard deviations between a normal's mean and zero, the
# clamp at zero pulls the sampler's actual mean above the stated one.
_CLAMP_SIGMAS = 3.0


def _substitution(component: str, parameter: str, was: Any, now: float) -> dict:
    return {"component": component, "parameter": parameter, "from": was, "to": now}


def deterministic_spec(
    spec: Sequence[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[dict], List[str]]:
    """Rewrite a build spec with every distribution replaced by its mean.

    Args:
        spec: a model's recorded build log ({"op", "kwargs"} per step).

    Returns:
        `(twin_spec, substitutions, warnings)`. `twin_spec` builds the same
        plant with constant delays and no random edge choice; `substitutions`
        lists every value that changed; `warnings` names the normals whose
        clamp at zero puts their true mean above the constant substituted here.
    """
    twin: List[Dict[str, Any]] = []
    substitutions: List[dict] = []
    warnings: List[str] = []

    for step in spec:
        op = step["op"]
        kwargs = dict(step["kwargs"])
        component = kwargs.get("id", op)

        for field in _DELAY_FIELDS.get(op, ()):
            if field not in kwargs:
                continue
            value = kwargs[field]
            parts = spec_parts(value)
            if parts is None:
                continue
            mean = mean_of(field, value)
            kwargs[field] = mean
            substitutions.append(_substitution(component, field, value, mean))
            name, a, b = parts
            if name in ("normal", "gauss") and b and a < _CLAMP_SIGMAS * b:
                warnings.append(
                    f"{component}.{field} is {value}: samples are clamped at "
                    f"zero, so the running model averages more than {mean:g}. "
                    "The calculated throughput is correspondingly optimistic."
                )

        for field in _SELECTION_FIELDS:
            if kwargs.get(field) == _RANDOM:
                kwargs[field] = _ROUND_ROBIN
                substitutions.append(
                    _substitution(component, field, _RANDOM, _ROUND_ROBIN)
                )

        twin.append({"op": op, "kwargs": kwargs})

    return twin, substitutions, warnings


def _sink_ids(model: FactoryModel) -> List[str]:
    return [
        node_id
        for node_id, node in model.nodes.items()
        if type(node).__name__ == "Sink"
    ]


def _departures(model: FactoryModel, sink_id: str) -> List[float]:
    """When each item reached that sink in the run just finished."""
    return [
        event["time"]
        for event in model.events
        if event.get("kind") == "received"
        and event.get("node") == sink_id
        and event.get("time") is not None
    ]


def _measure(
    model: FactoryModel, sink_ids: Sequence[str], warmup: float, until: float
) -> Dict[str, dict]:
    """Departures per sink inside the window, and the rate they imply."""
    window = until - warmup
    measured: Dict[str, dict] = {}
    for sink_id in sink_ids:
        times = _departures(model, sink_id)
        in_window = [time for time in times if time >= warmup]
        measured[sink_id] = {
            "rate": len(in_window) / window,
            "in_window": len(in_window),
            "total": len(times),
        }
    return measured


def _compare(
    expected: Dict[str, float],
    measured: Dict[str, dict],
    tolerance: float,
    window: float,
) -> Dict[str, dict]:
    """Hold each sink's measured rate against its calculated one."""
    comparison: Dict[str, dict] = {}
    for sink_id, expected_rate in expected.items():
        actual = measured.get(sink_id, {}).get("rate", 0.0)

        if expected_rate <= 0:
            comparison[sink_id] = {
                "expected": expected_rate,
                "measured": actual,
                "deviation": None,
                "tolerance": None,
                "passed": actual <= 0,
            }
            continue

        # A count over a window is right to one item, which on a short window
        # can be looser than the caller's tolerance.
        quantization = 1.0 / (window * expected_rate)
        allowed = max(tolerance, quantization)
        deviation = (actual - expected_rate) / expected_rate
        comparison[sink_id] = {
            "expected": expected_rate,
            "measured": actual,
            "deviation": deviation,
            "tolerance": allowed,
            "passed": abs(deviation) <= allowed,
        }
    return comparison


def _upper_bound(
    replication_means: Dict[str, float],
    deterministic_counts: Dict[str, float],
    tolerance: float,
) -> Dict[str, dict]:
    """Check no replication mean sits above the deterministic count.

    Keys are accepted either as `run_replications` reports them
    (`snk.num_item_received`) or as a bare sink id.
    """
    findings: Dict[str, dict] = {}
    for key, mean in replication_means.items():
        sink_id = key.split(".", 1)[0]
        if sink_id not in deterministic_counts:
            continue
        ceiling = deterministic_counts[sink_id]
        findings[sink_id] = {
            "replication_mean": float(mean),
            "deterministic": ceiling,
            "holds": float(mean) <= ceiling * (1 + tolerance),
        }
    return findings


def _summary(report: dict) -> str:
    """The report as a short block of text."""
    if not report["applicable"]:
        lines = ["Fixed-value test: no expected value can be stated."]
        lines += [f"  - {b['message']}" for b in report["blockers"]]
        return "\n".join(lines)

    settings = report["settings"]
    lines = [
        "Fixed-value test",
        f"  run to {settings['until']:g}, counted from {settings['warmup']:g}",
        "",
        "  sink            calculated     measured    deviation   verdict",
    ]
    for sink_id, entry in report["comparison"].items():
        deviation = entry["deviation"]
        shown = "n/a" if deviation is None else f"{deviation * 100:+.2f}%"
        verdict = "ok" if entry["passed"] else "MISMATCH"
        lines.append(
            f"  {sink_id:<14} {entry['expected']:>10.4f}   {entry['measured']:>10.4f}"
            f"   {shown:>10}   {verdict}"
        )

    bottlenecks = ", ".join(report["expected"]["bottlenecks"]) or "none"
    lines += ["", f"  bottleneck: {bottlenecks}"]

    binding = [row for row in report["expected"]["stations"] if row["binding"]]
    for row in binding:
        lines.append(f"    {row['component']}: {row['basis']} = {row['rate']:g}")

    if report["upper_bound"]:
        lines += ["", "  replication means against the deterministic ceiling:"]
        for sink_id, entry in report["upper_bound"].items():
            verdict = "ok" if entry["holds"] else "ABOVE THE CEILING"
            lines.append(
                f"    {sink_id}: {entry['replication_mean']:.2f} vs "
                f"{entry['deterministic']:.0f}   {verdict}"
            )

    for warning in report["warnings"]:
        lines.append(f"  ! {warning}")

    lines += ["", f"  verdict: {'passed' if report['passed'] else 'FAILED'}"]
    return "\n".join(lines)


def verify_fixed_value(
    until: float,
    warmup: Optional[float] = None,
    tolerance: float = DEFAULT_TOLERANCE,
    replication_means: Optional[Dict[str, float]] = None,
    *,
    model: FactoryModel | None = None,
) -> dict:
    """Compare the model's throughput against one calculated independently.

    Builds a deterministic twin of the model, works out what that twin must
    produce, runs it, and holds the two against each other.

    Args:
        until: end time for the twin's run; must be a positive number. Long
            enough that the line fills well before `warmup`.
        warmup: point from which departures are counted. Defaults to half of
            `until`.
        tolerance: deviation allowed between the calculated and the measured
            rate, as a share of the calculated one. Never applied tighter than
            one item's worth of the counting window.
        replication_means: means from a `run_replications` batch of the
            *stochastic* model, keyed either as that tool reports them
            (`snk.num_item_received`) or by bare sink id. Each is checked
            against the deterministic count, which it cannot exceed.
        model: the model to check; defaults to the session model. Only its
            build spec is read — its clock and its last run's stats are left
            alone.

    Returns:
        A dict with `summary` (read this first), `passed`, `applicable`,
        `blockers` (why no figure could be stated), `expected` (the calculated
        rates, the station table and the bottleneck), `measured` (what the twin
        produced), `comparison` (per sink: calculated, measured, deviation,
        tolerance, verdict), `upper_bound`, `substitutions` (every distribution
        that was replaced), `warnings` and `settings`.

    Raises:
        ValueError: if `until` is not a positive number, `warmup` is outside
            [0, until), `tolerance` is not between 0 and 1, or the model has no
            recorded build steps.
    """
    require_positive_number("until", until)

    if warmup is None:
        warmup = until / 2.0
    require_positive_number("tolerance", tolerance)
    if tolerance >= 1:
        raise ValueError(f"tolerance must be below 1 (got {tolerance}).")
    if isinstance(warmup, bool) or not isinstance(warmup, (int, float)):
        raise ValueError(f"warmup must be a number (got {warmup!r}).")
    if not 0 <= warmup < until:
        raise ValueError(
            f"warmup must be at least 0 and below until ({until}); got {warmup}."
        )

    source = model if model is not None else get_session_model()
    spec = list(source.spec)
    if not spec:
        raise ValueError(
            "The model is empty: create nodes and edges (and connect them) "
            "before running the fixed-value test."
        )

    twin_spec, substitutions, warnings = deterministic_spec(spec)
    twin = build_from_spec(twin_spec)
    expected = expected_throughput(twin)

    settings = {
        "until": until,
        "warmup": warmup,
        "window": until - warmup,
        "tolerance": tolerance,
    }

    if not expected["applicable"]:
        report = {
            "applicable": False,
            "passed": False,
            "blockers": expected["blockers"],
            "expected": expected,
            "measured": {},
            "comparison": {},
            "upper_bound": {},
            "substitutions": substitutions,
            "warnings": warnings,
            "settings": settings,
        }
        report["summary"] = _summary(report)
        return report

    try:
        result = run_simulation(until, seed=None, model=twin)
    except Exception as exc:
        # FactorySimPy raises from inside the run for things no static check
        # sees. Naming the twin keeps the error from reading as a fault in the
        # session model, which was never run.
        report = {
            "applicable": False,
            "passed": False,
            "blockers": [
                {
                    "reason": "twin_run_failed",
                    "component": None,
                    "message": (
                        "The deterministic twin raised while running: "
                        f"{type(exc).__name__}: {exc}. The same model run "
                        "directly would raise the same way. Run validate_model "
                        "and fix what it reports first."
                    ),
                }
            ],
            "expected": expected,
            "measured": {},
            "comparison": {},
            "upper_bound": {},
            "substitutions": substitutions,
            "warnings": warnings,
            "settings": settings,
        }
        report["summary"] = _summary(report)
        return report

    sink_ids = _sink_ids(twin)
    measured = _measure(twin, sink_ids, warmup, until)
    comparison = _compare(
        expected["per_sink"], measured, tolerance, settings["window"]
    )

    for sink_id, entry in measured.items():
        if entry["in_window"] < MIN_WINDOW_ITEMS:
            warnings.append(
                f"Only {entry['in_window']} item(s) reached '{sink_id}' after "
                f"the warm-up point. Raise `until` — the measured rate rests on "
                "too few departures to mean much."
            )

    counts = {
        sink_id: float(
            (result["nodes"].get(sink_id) or {}).get(SINK_THROUGHPUT_STAT, 0)
        )
        for sink_id in sink_ids
    }
    upper_bound = (
        _upper_bound(replication_means, counts, tolerance)
        if replication_means
        else {}
    )

    report = {
        "applicable": True,
        "passed": all(entry["passed"] for entry in comparison.values())
        and all(entry["holds"] for entry in upper_bound.values()),
        "blockers": [],
        "expected": expected,
        "measured": {
            "per_sink": measured,
            "counts": counts,
            "total_rate": sum(entry["rate"] for entry in measured.values()),
        },
        "comparison": comparison,
        "upper_bound": upper_bound,
        "substitutions": substitutions,
        "warnings": warnings,
        "settings": settings,
    }
    report["summary"] = _summary(report)
    return report
