"""The limit test: what does one part experience at every station?

**One part.** A copy of the model is built and every source is set to release a
single item at time 0 and nothing after it. With one part in the plant nothing
queues, so the time the part spends at a station is that station's own time. It
is held against the value the station was built with.

**Three settings.** The same part goes through three copies: every delay
distribution at its lower bound, the model unchanged, every delay distribution
at its upper bound.

    uniform(a, b)   a                      b
    normal(m, s)    m - 3s, floored at 0   m + 3s
    gauss(m, s)     m - 3s, floored at 0   m + 3s
    exp(x)          0                      x * ln(1/(1 - quantile))
    a constant      itself                 itself

`exp` has no upper bound; the quantile stands in for one and the report says so.

**The whole run.** When the model has a finished run, its parts are measured
too: per station the fastest, the mean and the slowest. The single part's total
at the lower bound is a floor for that run — waiting only adds to a part's
time. A part below the floor skipped a station, and its route is reported.

Every number here is read off a run. Nothing is derived from the wiring and no
expected value is calculated. The session model is only read: its build spec is
replayed into fresh copies, so its clock and its last run's stats stay intact.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from simtrace.model import FactoryModel
from simtrace.model import get_model as get_session_model
from simtrace.tools.distributions import spec_parts
from simtrace.tools.simulation.lifecycle import run_simulation
from simtrace.tools.simulation.rebuild import build_from_spec
from simtrace.tools.utils import require_positive_number

LOWER = "min"
ENTERED = "entered"
UPPER = "max"
SETTINGS = (LOWER, ENTERED, UPPER)

# Build-spec fields holding a time a part spends at a component.
_TIME_FIELDS: Dict[str, Tuple[str, ...]] = {
    "create_machine": ("processing_delay",),
    "create_splitter": ("processing_delay",),
    "create_combiner": ("processing_delay",),
    "create_buffer": ("delay",),
    "create_fleet": ("delay", "transit_delay"),
}

# normal(m, s) spans m +/- SIGMAS * s.
SIGMAS = 3.0

# exp(x) has no upper bound; this quantile stands in for one.
QUANTILE = 0.99

# Event times are read off FactorySimPy's narration, which prints them with
# two decimals, so a measured stay carries up to 0.01 of rounding.
TOLERANCE = 0.011

_SOURCE = "Source"
_SINK = "Sink"

_ERROR = "error"
_HINT = "hint"


def _number(value: Optional[float], width: int = 6) -> str:
    return "-".rjust(width) if value is None else f"{value:>{width}.2f}"


def bounds(
    value: Any, sigmas: float = SIGMAS, quantile: float = QUANTILE
) -> Tuple[float, float, bool]:
    """Lower and upper bound of a delay value, and whether the upper is real.

    Args:
        value: a constant int/float or a distribution string.
        sigmas: how many standard deviations a normal/gauss spans each way.
        quantile: the quantile standing in for an exp's missing upper bound.

    Returns:
        `(lower, upper, upper_is_real)`. `upper_is_real` is False for `exp`,
        whose upper value is the quantile rather than a bound it cannot pass.
    """
    parts = spec_parts(value)
    if parts is None:
        return float(value), float(value), True
    name, a, b = parts
    if name == "uniform":
        return a, b, True
    if name in ("normal", "gauss"):
        spread = sigmas * (b or 0.0)
        return max(0.0, a - spread), a + spread, True
    return 0.0, a * math.log(1.0 / (1.0 - quantile)), False


def spec_at(
    spec: Sequence[Dict[str, Any]],
    setting: str,
    sigmas: float = SIGMAS,
    quantile: float = QUANTILE,
) -> Tuple[List[Dict[str, Any]], List[dict]]:
    """Rewrite a build spec with every delay distribution at one bound.

    Args:
        spec: a model's recorded build log ({"op", "kwargs"} per step).
        setting: `LOWER`, `ENTERED` (the spec comes back unchanged) or `UPPER`.
        sigmas: passed to `bounds`.
        quantile: passed to `bounds`.

    Returns:
        `(spec, substitutions)`. Inter-arrival times are left alone: the parts
        are released by patching the sources, not through their parameters.
    """
    rewritten: List[Dict[str, Any]] = []
    substitutions: List[dict] = []

    for step in spec:
        kwargs = dict(step["kwargs"])
        if setting != ENTERED:
            for field in _TIME_FIELDS.get(step["op"], ()):
                value = kwargs.get(field)
                if value is None or spec_parts(value) is None:
                    continue
                low, high, _ = bounds(value, sigmas, quantile)
                kwargs[field] = low if setting == LOWER else high
                substitutions.append(
                    {
                        "component": kwargs.get("id"),
                        "parameter": field,
                        "from": value,
                        "to": kwargs[field],
                    }
                )
        rewritten.append({"op": step["op"], "kwargs": kwargs})

    return rewritten, substitutions


def _gate(parts: int, pause: float):
    """A source delay of 0 for the first `parts` items and `pause` after."""
    left = {"n": parts}

    def gap() -> float:
        if left["n"] > 0:
            left["n"] -= 1
            return 0.0
        return pause

    return gap


def _release(model: FactoryModel, parts: int, pause: float) -> List[str]:
    """Set every source to release `parts` items at time 0 and none after."""
    sources = []
    for node_id, node in model.nodes.items():
        if type(node).__name__ != _SOURCE:
            continue
        node.inter_arrival_time = _gate(parts, pause)
        sources.append(node_id)
    return sources


def _bare(item: Optional[str]) -> Optional[str]:
    """`Item(x)` and `Pallet(x, items=2)` down to `x`; anything else unchanged."""
    if item is None:
        return None
    if item.startswith(("Item(", "Pallet(")):
        return item[item.index("(") + 1 :].split(",")[0].rstrip(")")
    return item


def part_stays(events: Sequence[dict]) -> Dict[str, List[Tuple[str, float, float]]]:
    """Per item, where it was and from when to when.

    An item's stay at a place runs from the line that put it there to the line
    that took it away: a node's `put` names the node it left and the edge it
    entered, a `get` or `process_start` names the node that took it, and
    `received` closes the trail at a sink.

    Args:
        events: the events a run captured (`model.events`).

    Returns:
        `{item_id: [(place, entry, exit), ...]}` in travel order. Places are
        node and edge ids, so a stay in a buffer is a row like any other.
    """
    per_item: Dict[str, List[dict]] = {}
    for event in events:
        item = _bare(event.get("item"))
        if item is None or event.get("time") is None:
            continue
        per_item.setdefault(item, []).append(event)

    stays: Dict[str, List[Tuple[str, float, float]]] = {}
    for item, own_events in per_item.items():
        rows: List[Tuple[str, float, float]] = []
        place: Optional[str] = None
        since = 0.0
        for event in own_events:
            kind, time = event["kind"], event["time"]
            if kind == "edge_put":
                if place is None:
                    place, since = event.get("edge"), time
            elif kind in ("get", "process_start"):
                node = event.get("node")
                if place is not None and node is not None and place != node:
                    rows.append((place, since, time))
                    place, since = node, time
            elif kind == "put":
                node, edge = event.get("node"), event.get("edge")
                if place is not None and place == node:
                    rows.append((place, since, time))
                if edge is not None:
                    place, since = edge, time
            elif kind == "received":
                if place is not None:
                    rows.append((place, since, time))
                place = None
        stays[item] = rows
    return stays


def _delivered(
    stays: Dict[str, List[Tuple[str, float, float]]],
    events: Sequence[dict],
    sink_ids: Sequence[str],
) -> Dict[str, Tuple[List[Tuple[str, float, float]], float]]:
    """Only the items that reached a sink, each with its total time."""
    arrived = {
        _bare(event.get("item"))
        for event in events
        if event.get("kind") == "received" and event.get("node") in sink_ids
    }
    return {
        item: (rows, rows[-1][2] - rows[0][1])
        for item, rows in stays.items()
        if item in arrived and rows
    }


def _entered_value(spec: Sequence[Dict[str, Any]], component: str) -> Optional[Any]:
    """The delay a component was built with, as it was written."""
    for step in spec:
        kwargs = step["kwargs"]
        if kwargs.get("id") != component:
            continue
        for field in _TIME_FIELDS.get(step["op"], ()):
            if field in kwargs:
                return kwargs[field]
    return None


def _one_pass(
    spec: Sequence[Dict[str, Any]],
    setting: str,
    until: float,
    seed: Optional[int],
    parts: int,
    sigmas: float,
    quantile: float,
) -> dict:
    """Release `parts` items into a copy of the model at one setting."""
    rewritten, substitutions = spec_at(spec, setting, sigmas, quantile)
    model = build_from_spec(rewritten)
    _release(model, parts, until * 10.0)

    result = {
        "setting": setting,
        "failed": None,
        "stays": {},
        "delivered": {},
        "edges": set(model.edges),
        "substitutions": substitutions,
    }
    try:
        run_simulation(until, seed=seed, model=model)
    except Exception as exc:
        result["failed"] = f"{type(exc).__name__}: {exc}"
        return result

    sink_ids = [
        node_id for node_id, node in model.nodes.items()
        if type(node).__name__ == _SINK
    ]
    result["stays"] = part_stays(model.events)
    result["delivered"] = _delivered(result["stays"], model.events, sink_ids)
    return result


def _station_rows(
    spec: Sequence[Dict[str, Any]],
    passes: Dict[str, dict],
    run_stays: Dict[str, List[Tuple[str, float, float]]],
    sigmas: float,
    quantile: float,
) -> List[dict]:
    """One row per place a part was seen at: its three times and the run's."""
    measured: Dict[str, Dict[str, float]] = {}
    order: List[str] = []

    for setting in SETTINGS:
        for rows, _ in passes[setting]["delivered"].values():
            for place, entry, leave in rows:
                if place not in measured:
                    measured[place] = {}
                    order.append(place)
                measured[place][setting] = (
                    measured[place].get(setting, 0.0) + leave - entry
                )

    from_run: Dict[str, List[float]] = {}
    for rows in run_stays.values():
        for place, entry, leave in rows:
            from_run.setdefault(place, []).append(leave - entry)
    for place in from_run:
        if place not in measured:
            measured[place] = {}
            order.append(place)

    edges = passes[ENTERED]["edges"]
    rows_out: List[dict] = []
    for place in order:
        entered = _entered_value(spec, place)
        limits = None
        if entered is not None:
            low, high, capped = bounds(entered, sigmas, quantile)
            limits = {"low": low, "high": high, "capped": capped}
        seen = from_run.get(place, [])
        rows_out.append(
            {
                "id": place,
                "kind": "edge" if place in edges else "node",
                "entered": None if entered is None else str(entered),
                "bounds": limits,
                LOWER: measured[place].get(LOWER),
                "entered_run": measured[place].get(ENTERED),
                UPPER: measured[place].get(UPPER),
                "run": (
                    {
                        "n": len(seen),
                        "fastest": min(seen),
                        "mean": sum(seen) / len(seen),
                        "slowest": max(seen),
                    }
                    if seen
                    else None
                ),
            }
        )
    return rows_out


def _check_stations(rows: List[dict]) -> List[dict]:
    """Hold each measured time against the value the station was built with."""
    findings: List[dict] = []

    for row in rows:
        limits = row["bounds"]
        if limits is None:
            continue
        low, high, capped = limits["low"], limits["high"], limits["capped"]

        for setting, key, floor, ceiling in (
            (LOWER, LOWER, low, low),
            (ENTERED, "entered_run", low, high if capped else math.inf),
            (UPPER, UPPER, high, high),
        ):
            seen = row[key]
            if seen is None or floor - TOLERANCE <= seen <= ceiling + TOLERANCE:
                continue
            findings.append(
                {
                    "check": "time_mismatch",
                    "severity": _ERROR,
                    "component": row["id"],
                    "message": (
                        f"'{row['id']}' was built with {row['entered']}, but the "
                        f"part spent {seen:g} there at the {setting} setting "
                        f"(expected {floor:g} to {ceiling:g})."
                    ),
                }
            )
            break

        if low == 0 and high > 0:
            findings.append(
                {
                    "check": "zero_possible",
                    "severity": _HINT,
                    "component": row["id"],
                    "message": (
                        f"'{row['id']}' is {row['entered']}, so a part can pass "
                        "it in no time at all."
                    ),
                }
            )
        if not capped:
            findings.append(
                {
                    "check": "no_upper_bound",
                    "severity": _HINT,
                    "component": row["id"],
                    "message": (
                        f"'{row['id']}' is {row['entered']}, which has no upper "
                        f"bound. The {high:g} shown is the value it stays under "
                        "99 times in 100."
                    ),
                }
            )

        if row["run"] and row["run"]["fastest"] < low - TOLERANCE:
            findings.append(
                {
                    "check": "below_station_floor",
                    "severity": _ERROR,
                    "component": row["id"],
                    "message": (
                        f"In the run a part passed '{row['id']}' in "
                        f"{row['run']['fastest']:g}, below the {low:g} its own "
                        "value allows."
                    ),
                }
            )

    return findings


def _summary(report: dict) -> str:
    """The report as a short block of text."""
    settings = report["settings"]
    seed = settings["seed"]
    lines = [
        "Limit test",
        f"  {settings['parts_per_source']} part(s) per source, run to "
        f"{settings['until']:g}" + (f", seed {seed}" if seed is not None else ""),
        "",
        "  station          entered           min    as built        max"
        "   in the run",
    ]

    for row in report["stations"]:
        run = row["run"]
        from_run = (
            "-"
            if run is None
            else f"{run['fastest']:.2f} to {run['slowest']:.2f}  n={run['n']}"
        )
        lines.append(
            f"  {row['id']:<16} {str(row['entered'] or ''):<14}"
            f"{_number(row[LOWER])}  {_number(row['entered_run'])}"
            f"  {_number(row[UPPER])}   {from_run}"
        )

    single = report["single"]
    lines += [
        "",
        f"  one part, total: {_number(single[LOWER]).strip()} at the lower bound, "
        f"{_number(single[ENTERED]).strip()} as built, "
        f"{_number(single[UPPER]).strip()} at the upper bound",
    ]

    run = report["run"]
    if run:
        lines.append(
            f"  the run's {run['n']} parts: {run['fastest']:.2f} fastest, "
            f"{run['mean']:.2f} mean, {run['slowest']:.2f} slowest"
        )
    for part in report["below_floor"]:
        lines.append(
            f"    below the floor: {part['item']} in {part['total']:.2f} via "
            + " > ".join(part["route"])
        )

    errors = [f for f in report["findings"] if f["severity"] == _ERROR]
    hints = [f for f in report["findings"] if f["severity"] == _HINT]
    if errors:
        lines += ["", "  what does not add up:"]
        lines += [f"    {finding['message']}" for finding in errors]
    if hints:
        lines += ["", "  worth a look:"]
        lines += [f"    {finding['message']}" for finding in hints]

    lines += ["", f"  verdict: {'passed' if report['passed'] else 'FAILED'}"]
    return "\n".join(lines)


def verify_extreme_conditions(
    until: float,
    seed: Optional[int] = None,
    parts_per_source: int = 1,
    sigmas: float = SIGMAS,
    quantile: float = QUANTILE,
    *,
    model: FactoryModel | None = None,
) -> dict:
    """Send one part through the model at its lower bound, as built, and at its upper bound.

    With a single part in the plant nothing queues, so the time it spends at a
    station is that station's own time. Every number returned is read off a run.

    Args:
        until: end time for each of the three runs; must be a positive number.
            Long enough for a part to reach a sink at the upper bound.
        seed: RNG seed for the run at the built values. At the bounds there is
            nothing left to draw.
        parts_per_source: items each source releases at time 0. One means
            nothing queues anywhere, which is what makes a measured time
            comparable to the value entered. Raise it for a model whose
            combiner needs several items per cycle, and read the times as
            including waiting.
        sigmas: how far a normal/gauss reaches each way, in standard
            deviations.
        quantile: the quantile standing in for an exp's missing upper bound.
        model: the model to check; defaults to the session model. Only its
            build spec and its last run's events are read.

    Returns:
        A dict with `summary` (read this first), `passed`, `stations` (per
        place: the value entered, the three measured times, and the run's
        fastest/mean/slowest), `single` (the part's total per setting), `run`
        (the last run's parts, or None when nothing has run), `below_floor`
        (parts quicker than the single part at the lower bound, with their
        route), `findings`, `substitutions` and `settings`.

    Raises:
        ValueError: if `until` is not a positive number, `parts_per_source` is
            not a positive int, `quantile` is not between 0 and 1, or the model
            has no recorded build steps.
    """
    require_positive_number("until", until)
    if isinstance(parts_per_source, bool) or not isinstance(parts_per_source, int):
        raise ValueError(f"parts_per_source must be an int (got {parts_per_source!r}).")
    if parts_per_source < 1:
        raise ValueError("parts_per_source must be a positive int (>= 1).")
    if not 0 < quantile < 1:
        raise ValueError(f"quantile must be between 0 and 1 (got {quantile}).")

    source = model if model is not None else get_session_model()
    spec = list(source.spec)
    if not spec:
        raise ValueError(
            "The model is empty: create nodes and edges (and connect them) "
            "before running the limit test."
        )

    passes = {
        setting: _one_pass(
            spec, setting, until, seed, parts_per_source, sigmas, quantile
        )
        for setting in SETTINGS
    }

    findings: List[dict] = []
    single: Dict[str, Optional[float]] = {}
    for setting in SETTINGS:
        result = passes[setting]
        single[setting] = None
        if result["failed"]:
            findings.append(
                {
                    "check": "run_failed",
                    "severity": _ERROR,
                    "component": None,
                    "message": (
                        f"The copy at the {setting} setting raised while "
                        f"running: {result['failed']}."
                    ),
                }
            )
            continue
        if not result["delivered"]:
            stuck = ""
            for rows in result["stays"].values():
                if rows:
                    stuck = f" The part is still at '{rows[-1][0]}'."
                    break
            findings.append(
                {
                    "check": "part_stuck",
                    "severity": _ERROR,
                    "component": None,
                    "message": (
                        f"At the {setting} setting no part reached a sink within "
                        f"{until:g}.{stuck} Raise `until`, or `parts_per_source` "
                        "when a combiner needs several items per cycle."
                    ),
                }
            )
            continue
        # The quickest part is the floor for the run: another route cannot be
        # slower than its own stations and still beat this.
        single[setting] = min(total for _, total in result["delivered"].values())

    sink_ids = [
        node_id for node_id, node in source.nodes.items()
        if type(node).__name__ == _SINK
    ]
    run_stays = part_stays(source.events) if source.events else {}
    run_delivered = _delivered(run_stays, source.events, sink_ids) if run_stays else {}

    stations = _station_rows(
        spec,
        passes,
        {item: rows for item, (rows, _) in run_delivered.items()},
        sigmas,
        quantile,
    )
    findings += _check_stations(stations)

    totals = sorted(total for _, total in run_delivered.values())
    run = (
        {
            "n": len(totals),
            "fastest": totals[0],
            "mean": sum(totals) / len(totals),
            "slowest": totals[-1],
            "totals": totals,
        }
        if totals
        else None
    )

    below_floor: List[dict] = []
    floor = single[LOWER]
    if run and floor is not None:
        for item, (rows, total) in sorted(
            run_delivered.items(), key=lambda pair: pair[1][1]
        ):
            if total >= floor - TOLERANCE:
                continue
            below_floor.append(
                {"item": item, "total": total, "route": [p for p, _, _ in rows]}
            )
        if below_floor:
            findings.append(
                {
                    "check": "below_floor",
                    "severity": _ERROR,
                    "component": None,
                    "message": (
                        f"{len(below_floor)} of {len(totals)} parts in the run "
                        f"were quicker than {floor:g}, the total of one part "
                        "with every station at its shortest: "
                        + "; ".join(
                            f"{part['item']} ({part['total']:g}) via "
                            + " > ".join(part["route"])
                            for part in below_floor[:3]
                        )
                    ),
                }
            )

    report = {
        "passed": not any(f["severity"] == _ERROR for f in findings),
        "stations": stations,
        "single": single,
        "run": run,
        "below_floor": below_floor,
        "findings": findings,
        "substitutions": {
            LOWER: passes[LOWER]["substitutions"],
            UPPER: passes[UPPER]["substitutions"],
        },
        "settings": {
            "until": until,
            "seed": seed,
            "parts_per_source": parts_per_source,
            "sigmas": sigmas,
            "quantile": quantile,
        },
    }
    report["summary"] = _summary(report)
    return report
