"""The limit test: what does one part experience at every station?

**One part.** A copy of the model is built for each source, and that source is
set to release a single item at time 0 and nothing after it, the others
nothing. With one part in the plant nothing queues, so the time the part spends
at a station is that station's own time. It is held against the value the
station was built with. A combiner needs parts from several sources at once, so
in a model with one every source releases into the same copy, and a source
feeding a combiner releases as many items as the combiner takes from its edge.

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

**Every combination.** Each station whose time is a distribution is set to its
lower or its upper bound, in every combination, and the model runs
`REPLICATIONS` times per combination with its sources as entered, the same
seeds in every combination. With every station time fixed, the arrivals,
breakdowns, repairs and route shares still draw, all from one random stream, so
a combination that changes a station's time also changes which value goes to
which draw. One run per combination would then show differences that are only
noise. Past `MAX_COMBINATIONS` only the two corners and each station set
against all the others are run. Reported: a combination that raises, one that
delivers nothing, one whose items do not add up, a station whose upper bound
delivers more parts than its lower bound by more than the spread of the runs
explains, and a station that changes no count.

**Extreme inputs.** Three more copies, each with the outcome a real plant
would show written down before it runs (`INPUT_CASES`): no source releases
anything, every inter-arrival time divided by `ARRIVAL_FACTOR`, and every
buffer at `ENDLESS` places. The second runs once per seed next to the model as
built and is held against it like a combination.

**Targeted extremes.** One copy per setting, each pushed past its bounds:
every station stopped in turn, every branch of a route share given every part,
and, with two or more sources, each source alone (`TARGETED_CASES`). What the
plant does here depends on its specification, so these runs report what
reached which sink by which route, for the reader to hold against it.

Every number here is read off a run. Nothing is derived from the wiring and no
expected value is calculated. The session model is only read: its build spec is
replayed into fresh copies, so its clock and its last run's stats stay intact.
"""

from __future__ import annotations

import math
import statistics
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from scipy.stats import t as student_t

from simtrace.model import FactoryModel
from simtrace.model import get_model as get_session_model
from simtrace.tools.distributions import spec_parts
from simtrace.tools.simulation import parallel
from simtrace.tools.simulation.lifecycle import run_simulation
from simtrace.tools.simulation.rebuild import build_from_spec
from simtrace.tools.simulation.replications import SEED_STRIDE
from simtrace.tools.utils import require_positive_number
from simtrace.tools.validation.conservation import verify_conservation

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

# Each station doubles the combinations; 8 is three stations. From four
# stations on, the two corners and each station against the rest (2k + 2 runs)
# are fewer than every combination.
MAX_COMBINATIONS = 8

# Runs per combination. Replication r uses seed + r * SEED_STRIDE, the same
# seeds in every combination.
REPLICATIONS = 5

# Error rate for "a longer time delivers more parts", shared out over the
# stations (Bonferroni) and, for a single combination, over every pair.
ALPHA = 0.05

# A station whose bounds shift the parts delivered by no more than this in
# every pair is reported as never limiting the line. One part covers a part
# that crosses the end of the run a little earlier or later.
NO_INFLUENCE_TOLERANCE = 1

# Event times are read off FactorySimPy's narration, which prints them with
# two decimals, so a measured stay carries up to 0.01 of rounding.
TOLERANCE = 0.011

_SOURCE = "Source"
_SINK = "Sink"
_COMBINER = "Combiner"
_BUFFER = "Buffer"

# Extreme inputs, each with the outcome a real plant would show.
AS_BUILT = "as_built"
NO_ARRIVALS = "no_arrivals"
MORE_ARRIVALS = "arrivals_x10"
ENDLESS_BUFFERS = "endless_buffers"
MINIMAL_BUFFERS = "minimal_buffers"
NO_BREAKDOWNS = "no_breakdowns"
ZERO_TIMES = "zero_times"
ARRIVAL_FACTOR = 10
ENDLESS = 1_000_000
INPUT_CASES: Dict[str, str] = {
    NO_ARRIVALS: "no source releases anything: 0 items created, 0 parts delivered",
    MORE_ARRIVALS: (
        f"every inter-arrival time divided by {ARRIVAL_FACTOR}: the output does not "
        "fall below the model as built"
    ),
    ENDLESS_BUFFERS: (
        f"every buffer holds {ENDLESS} items: no station whose out-edges are all "
        "buffers is ever blocked"
    ),
    MINIMAL_BUFFERS: "every buffer holds 1 item: parts still reach a sink",
    NO_BREAKDOWNS: (
        "no machine breaks down: 0 breakdowns, and the output does not fall below "
        "the model as built"
    ),
    ZERO_TIMES: (
        "every station takes no time: the output does not fall below the model "
        "as built"
    ),
}
# Cases run once per seed and held against the model as built at the same seed.
PAIRED_CASES = (MORE_ARRIVALS, NO_BREAKDOWNS, ZERO_TIMES)
# Per paired case: the finding's check, the setting, and why less output is wrong.
_LESS_OUTPUT: Dict[str, Tuple[str, str, str]] = {
    MORE_ARRIVALS: (
        "input_more_arrivals_less_output",
        f"With every inter-arrival time divided by {ARRIVAL_FACTOR:g}",
        "More arrivals cannot lower the output.",
    ),
    NO_BREAKDOWNS: (
        "input_no_breakdowns_less_output",
        "With every breakdown taken out",
        "A machine that never breaks down cannot lower the output.",
    ),
    ZERO_TIMES: (
        "input_zero_times_less_output",
        "With every station time at 0",
        "Quicker stations cannot lower the output.",
    ),
}

# Targeted extremes, each run once with one setting pushed past its bounds.
# The expectation depends on the specification, so the result is for the
# reader to hold against it.
STOPPED = "station_stopped"
ONE_BRANCH = "one_branch"
ONE_SOURCE = "one_source"
TARGETED_CASES: Dict[str, str] = {
    STOPPED: (
        "one station never finishes a part: a part reaches a sink only on a "
        "route that leaves this station out"
    ),
    ONE_BRANCH: (
        "every part takes one branch of a route share: the parts end in the "
        "sink this branch leads to, counted the way that sink counts them"
    ),
    ONE_SOURCE: (
        "only one source releases parts: only the stations on that source's "
        "route work"
    ),
}
_STATION_OPS = ("create_machine", "create_splitter", "create_combiner")
_BREAKDOWN_FIELDS = ("time_to_failure", "repair_time")
_FIRST_AVAILABLE = "FIRST_AVAILABLE"

# How the combinations release parts.
SATURATED = "saturated"
AS_ENTERED = "as entered"

_ERROR = "error"
_HINT = "hint"
_INFO = "info"


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


def has_real_floor(value: Any) -> bool:
    """Whether a delay can never be drawn below its lower bound.

    True for a constant and for uniform. False for normal/gauss, whose lower
    bound is m - sigmas * s and can be passed by a rare draw, and for exp.
    """
    parts = spec_parts(value)
    return parts is None or parts[0] == "uniform"


def route_floor(
    spec: Sequence[Dict[str, Any]],
    route: Sequence[str],
    sigmas: float = SIGMAS,
    quantile: float = QUANTILE,
) -> Tuple[float, bool]:
    """Sum of the lower bounds along one route, and whether every one is real."""
    total = 0.0
    real = True
    for place in route:
        entered = _entered_value(spec, place)
        if entered is None:
            continue
        low, _, _ = bounds(entered, sigmas, quantile)
        total += low
        real = real and has_real_floor(entered)
    return total, real


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


def without_breakdowns(spec: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rewrite a build spec with no machine breaking down."""
    rewritten: List[Dict[str, Any]] = []
    for step in spec:
        kwargs = dict(step["kwargs"])
        for field in _BREAKDOWN_FIELDS:
            if field in kwargs:
                kwargs[field] = None
        rewritten.append({"op": step["op"], "kwargs": kwargs})
    return rewritten


def mean_of(value: Any) -> Any:
    """The mean of a distribution string; a constant comes back as it is."""
    parts = spec_parts(value)
    if parts is None:
        return value
    name, a, b = parts
    return (a + b) / 2 if name == "uniform" else a


def breakdowns_at_means(spec: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rewrite a build spec with every time to failure and repair time at its mean."""
    rewritten: List[Dict[str, Any]] = []
    for step in spec:
        kwargs = dict(step["kwargs"])
        for field in _BREAKDOWN_FIELDS:
            if kwargs.get(field) is not None:
                kwargs[field] = mean_of(kwargs[field])
        rewritten.append({"op": step["op"], "kwargs": kwargs})
    return rewritten


def without_shifts(spec: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rewrite a build spec with no shift calendar: every station works all the time."""
    return [
        {
            "op": step["op"],
            "kwargs": (
                {**step["kwargs"], "calendar": None}
                if "calendar" in step["kwargs"]
                else dict(step["kwargs"])
            ),
        }
        for step in spec
        if step["op"] != "create_shift_calendar"
    ]


def _connections(spec: Sequence[Dict[str, Any]]) -> List[Tuple[str, str, str]]:
    """Every connect step as `(edge, from node, to node)`, in build order."""
    return [
        (step["kwargs"]["edge_id"], step["kwargs"]["src_id"], step["kwargs"]["dest_id"])
        for step in spec
        if step["op"] == "connect"
    ]


def _ops(spec: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """Component id to the op that built it."""
    return {
        step["kwargs"]["id"]: step["op"]
        for step in spec
        if step["op"] != "connect" and "id" in step["kwargs"]
    }


def setups_ahead(spec: Sequence[Dict[str, Any]]) -> Dict[str, Tuple[str, float]]:
    """Edge id to `(node, setup time)` for every edge into a node with a setup time."""
    setups = {
        step["kwargs"]["id"]: step["kwargs"].get("node_setup_time") or 0
        for step in spec
        if step["op"] != "connect" and "id" in step["kwargs"]
    }
    return {
        edge: (dest, setups[dest])
        for edge, _, dest in _connections(spec)
        if setups.get(dest, 0) > 0
    }


def _gate(parts: int, pause: float):
    """A source delay of 0 for the first `parts` items and `pause` after."""
    left = {"n": parts}

    def gap() -> float:
        if left["n"] > 0:
            left["n"] -= 1
            return 0.0
        return pause

    return gap


def combiner_release(spec: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """Source id to the items it must release for a combiner to finish one cycle.

    A source feeds a combiner in-edge, directly or through other stations, and
    the combiner takes `target_quantity_of_each_item` items from that in-edge.
    Sources that feed no combiner are not listed.
    """
    targets = {
        step["kwargs"]["id"]: step["kwargs"].get("target_quantity_of_each_item") or [1]
        for step in spec
        if step["op"] == "create_combiner"
    }
    if not targets:
        return {}
    into: Dict[str, List[str]] = {}
    for edge, _, dest in _connections(spec):
        if dest in targets:
            into.setdefault(dest, []).append(edge)
    out = _downstream(spec)
    need: Dict[str, int] = {}
    for source, op in _ops(spec).items():
        if op != "create_source":
            continue
        stack, seen = [source], {source}
        while stack:
            for edge, dest in out.get(stack.pop(), []):
                if dest in targets:
                    index = into[dest].index(edge)
                    if index < len(targets[dest]):
                        need[source] = max(need.get(source, 1), targets[dest][index])
                elif dest not in seen:
                    seen.add(dest)
                    stack.append(dest)
    return need


def _release(
    model: FactoryModel,
    parts: int,
    pause: float,
    only: Optional[Sequence[str]] = None,
    at_least: Optional[Dict[str, int]] = None,
) -> List[str]:
    """Set every source to release `parts` items at time 0 and none after.

    With `only`, the sources not named release nothing. With `at_least`, a
    source named there releases at least that many.
    """
    sources = []
    for node_id, node in model.nodes.items():
        if type(node).__name__ != _SOURCE:
            continue
        own = max(parts, (at_least or {}).get(node_id, 0)) if (
            only is None or node_id in only
        ) else 0
        node.inter_arrival_time = _gate(own, pause)
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


def _fleet_stay(kwargs: Dict[str, Any], sigmas: float, quantile: float) -> Tuple[str, dict]:
    """How long one part stays in a fleet, as text and as low and high bounds.

    FactorySimPy's fleet takes transit_delay twice per trip: the empty drive to
    the source and the loaded drive to the destination. A fleet of capacity 1
    leaves as soon as the part is in. A larger fleet with one part waits its
    full delay first.
    """
    transit = kwargs.get("transit_delay") or 0
    delay = kwargs.get("delay", 1)
    capacity = kwargs.get("capacity", 1)
    t_low, t_high, t_capped = bounds(transit, sigmas, quantile)
    low, high, capped = 2 * t_low, 2 * t_high, t_capped
    text = f"2 x transit_delay {transit}"
    if capacity != 1:
        d_low, d_high, d_capped = bounds(delay, sigmas, quantile)
        low, high, capped = low + d_low, high + d_high, capped or d_capped
        text = f"delay {delay} + {text}"
    return text, {"low": low, "high": high, "capped": capped}


def _one_pass(
    spec: Sequence[Dict[str, Any]],
    setting: str,
    until: float,
    seed: Optional[int],
    parts: int,
    sigmas: float,
    quantile: float,
) -> dict:
    """Release `parts` items into a copy of the model at one setting.

    Each source gets a copy of its own, so the parts of two sources never meet
    at a station. A model with a combiner needs parts from several sources at
    once, so there every source releases into the same copy. The copies have
    no breakdowns and no shift calendars, which would add time to a stay.
    Setup times stay in.
    """
    rewritten, substitutions = spec_at(
        without_shifts(without_breakdowns(spec)), setting, sigmas, quantile
    )
    model = build_from_spec(rewritten)
    sources = [
        node_id for node_id, node in model.nodes.items()
        if type(node).__name__ == _SOURCE
    ]
    together = any(type(node).__name__ == _COMBINER for node in model.nodes.values())
    groups = [sources] if together or len(sources) < 2 else [[s] for s in sources]

    result = {
        "setting": setting,
        "failed": None,
        "stays": {},
        "delivered": {},
        "edges": set(model.edges),
        "substitutions": substitutions,
    }
    for n, group in enumerate(groups):
        if n:
            model = build_from_spec(rewritten)
        _release(model, parts, until * 10.0, only=group, at_least=combiner_release(spec))
        try:
            run_simulation(until, seed=seed, model=model)
        except Exception as exc:
            result["failed"] = f"{type(exc).__name__}: {exc}"
            return result

        sink_ids = [
            node_id for node_id, node in model.nodes.items()
            if type(node).__name__ == _SINK
        ]
        stays = part_stays(model.events)
        result["stays"].update(stays)
        result["delivered"].update(_delivered(stays, model.events, sink_ids))
    return result


def ranged_stations(spec: Sequence[Dict[str, Any]]) -> List[str]:
    """Ids of the components whose time is a distribution, in build order."""
    ids: List[str] = []
    for step in spec:
        kwargs = step["kwargs"]
        fields = _TIME_FIELDS.get(step["op"], ())
        if any(spec_parts(kwargs.get(field)) is not None for field in fields):
            ids.append(kwargs["id"])
    return ids


def spec_with(
    spec: Sequence[Dict[str, Any]],
    levels: Dict[str, str],
    sigmas: float = SIGMAS,
    quantile: float = QUANTILE,
) -> List[Dict[str, Any]]:
    """Rewrite a build spec with each named component at its own bound.

    Args:
        spec: a model's recorded build log.
        levels: component id to `LOWER` or `UPPER`. Components not named keep
            their value as entered.
        sigmas: passed to `bounds`.
        quantile: passed to `bounds`.
    """
    rewritten: List[Dict[str, Any]] = []
    for step in spec:
        kwargs = dict(step["kwargs"])
        level = levels.get(kwargs.get("id"))
        if level is not None:
            for field in _TIME_FIELDS.get(step["op"], ()):
                value = kwargs.get(field)
                if spec_parts(value) is None:
                    continue
                low, high, _ = bounds(value, sigmas, quantile)
                kwargs[field] = low if level == LOWER else high
        rewritten.append({"op": step["op"], "kwargs": kwargs})
    return rewritten


def combination_masks(count: int) -> Tuple[List[int], bool]:
    """Which combinations to run, as bit masks, and whether that is all of them.

    Bit i set means station i sits at its upper bound. Up to `MAX_COMBINATIONS`
    every mask is returned. Past it: all at the lower bound, all at the upper
    bound, and each station alone at one bound with all others at the other.
    """
    every = (1 << count) - 1
    if 1 << count <= MAX_COMBINATIONS:
        return list(range(1 << count)), True
    picked = {0, every}
    for i in range(count):
        picked.add(1 << i)
        picked.add(every ^ (1 << i))
    return sorted(picked), False


def _downstream(spec: Sequence[Dict[str, Any]]) -> Dict[str, List[Tuple[str, str]]]:
    """Node id to its out-edges as `(edge, next node)`."""
    out: Dict[str, List[Tuple[str, str]]] = {}
    for edge, src, dest in _connections(spec):
        out.setdefault(src, []).append((edge, dest))
    return out


def mix_is_fixed(spec: Sequence[Dict[str, Any]]) -> bool:
    """Whether the product mix stays as entered when every source is kept busy.

    A combiner takes its parts in a fixed ratio, so its output is a stream of
    its own, like a source's. True when no node gets parts from two streams:
    one source, separate lines, or sources that meet only at a combiner. A
    source that also passes the combiner and joins its output later makes it
    False.
    """
    ops = _ops(spec)
    out = _downstream(spec)
    starts = [
        node for node, op in ops.items() if op in ("create_source", "create_combiner")
    ]
    seen_by: Dict[str, str] = {}
    for start in starts:
        stack, reached = [start], {start}
        while stack:
            for _, dest in out.get(stack.pop(), []):
                if dest in reached or ops.get(dest) == "create_combiner":
                    continue
                reached.add(dest)
                stack.append(dest)
        for node in reached - {start}:
            if seen_by.setdefault(node, start) != start:
                return False
    return True


def zero_time_route(
    spec: Sequence[Dict[str, Any]], sigmas: float = SIGMAS, quantile: float = QUANTILE
) -> bool:
    """Whether a part can go from a source to a sink in no time at the lower bounds.

    A source kept busy would then release parts without end at one point in
    time. A conveyor always takes time.
    """
    ops = _ops(spec)
    out = _downstream(spec)
    lowest: Dict[str, float] = {}
    for step in spec:
        fields = _TIME_FIELDS.get(step["op"], ())
        if step["op"] == "create_conveyor":
            lowest[step["kwargs"]["id"]] = math.inf
        elif fields:
            lowest[step["kwargs"]["id"]] = sum(
                bounds(step["kwargs"].get(field) or 0, sigmas, quantile)[0]
                for field in fields
            )
    stack = [node for node, op in ops.items() if op == "create_source"]
    reached = set(stack)
    while stack:
        for edge, dest in out.get(stack.pop(), []):
            if lowest.get(edge, 0.0) > 0 or dest in reached:
                continue
            if ops.get(dest) == "create_sink":
                return True
            if lowest.get(dest, 0.0) > 0:
                continue
            reached.add(dest)
            stack.append(dest)
    return False


def saturated_arrivals(spec: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rewrite a build spec so every source releases its next part as soon as there is room."""
    return [
        {
            "op": step["op"],
            "kwargs": (
                {**step["kwargs"], "inter_arrival_time": 0, "blocking": True}
                if step["op"] == "create_source"
                else dict(step["kwargs"])
            ),
        }
        for step in spec
    ]


def random_elements(spec: Sequence[Dict[str, Any]]) -> List[str]:
    """What in a combination copy still draws from the random stream.

    The station times are fixed at their bounds in every combination. Left are
    inter-arrival times, breakdowns, route shares with two or more branches
    and RANDOM edge selection.
    """
    found: List[str] = []
    for step in spec:
        kwargs = step["kwargs"]
        name = kwargs.get("id")
        if step["op"] == "create_source" and spec_parts(kwargs.get("inter_arrival_time")):
            found.append(f"arrivals at '{name}' ({kwargs['inter_arrival_time']})")
        if any(spec_parts(kwargs.get(field)) for field in _BREAKDOWN_FIELDS):
            found.append(f"breakdowns at '{name}'")
        shares = kwargs.get("route_shares") or {}
        if sum(1 for share in shares.values() if share > 0) > 1:
            found.append(f"route shares at '{name}'")
        if "RANDOM" in (kwargs.get("in_edge_selection"), kwargs.get("out_edge_selection")):
            found.append(f"RANDOM selection at '{name}'")
    return found


def soft_reasons(spec: Sequence[Dict[str, Any]]) -> List[str]:
    """What in a model can let a longer station time deliver more parts.

    A priority between in-edges, routing by attribute, a shift calendar or a
    combiner changes which part goes where and when. A longer time can then
    shift the work to a quicker route or into a shift.
    """
    ops = _ops(spec)
    in_count: Dict[str, int] = {}
    for _, _, dest in _connections(spec):
        in_count[dest] = in_count.get(dest, 0) + 1
    found: List[str] = []
    for step in spec:
        kwargs = step["kwargs"]
        name = kwargs.get("id")
        if (
            step["op"] in ("create_machine", "create_splitter")
            and in_count.get(name, 0) > 1
            and kwargs.get("in_edge_selection", _FIRST_AVAILABLE) == _FIRST_AVAILABLE
        ):
            found.append(f"a priority between the in-edges of '{name}'")
        if kwargs.get("route_by"):
            found.append(f"routing by '{kwargs['route_by']}' at '{name}'")
    if "create_shift_calendar" in (step["op"] for step in spec):
        found.append("shift calendars")
    found += [f"the combiner '{node}'" for node, op in ops.items() if op == "create_combiner"]
    return found


# State names per node class: a machine has several work slots, a combiner
# and a splitter one.
_WORKING_STATES = ("ATLEAST_ONE_PROCESSING_STATE", "PROCESSING_STATE")
_BLOCKED_STATES = ("ALL_ACTIVE_BLOCKED_STATE", "BLOCKED_STATE")


def _station_states(node: Any) -> Optional[Dict[str, float]]:
    """A node's time per state, or None when it does not process items."""
    states = (getattr(node, "stats", None) or {}).get("total_time_spent_in_states", {})
    if not any(name in states for name in _WORKING_STATES):
        return None
    return states


def time_shares(model: FactoryModel, until: float) -> Dict[str, Dict[str, float]]:
    """Per station, the share of the run spent working, waiting, blocked and down.

    Read off the node stats FactorySimPy keeps. Only nodes that process items
    have these states; sources, sinks and edges are left out. The shares are
    parts of `until`. FactorySimPy counts a state only up to the node's last
    change of state, so the four can add up to a little less than 1.
    """
    shares: Dict[str, Dict[str, float]] = {}
    for node_id, node in model.nodes.items():
        states = _station_states(node)
        if states is None:
            continue
        shares[node_id] = {
            "working": sum(states.get(name, 0.0) for name in _WORKING_STATES) / until,
            "waiting": states.get("IDLE_STATE", 0.0) / until,
            "blocked": sum(states.get(name, 0.0) for name in _BLOCKED_STATES) / until,
            "down": states.get("BREAKDOWN_STATE", 0.0) / until,
        }
    return shares


def processed_counts(model: FactoryModel) -> Dict[str, int]:
    """Per station, the items it finished in the run.

    Read off the same nodes as `time_shares`: those that process items.
    """
    return {
        node_id: node.stats.get("num_item_processed", 0)
        for node_id, node in model.nodes.items()
        if _station_states(node) is not None
    }


def _share_rows(runs: Sequence[dict]) -> List[dict]:
    """Per station, how its shares ranged over the runs, every replication of every combination."""
    per_station: Dict[str, List[Dict[str, float]]] = {}
    for run in runs:
        for station, shares in (run.get("shares") or {}).items():
            per_station.setdefault(station, []).append(shares)
    rows: List[dict] = []
    for station, seen in per_station.items():
        row: Dict[str, Any] = {"component": station, "runs": len(seen)}
        for key in ("working", "waiting", "blocked"):
            values = [shares[key] for shares in seen]
            row[key] = {
                "lowest": min(values),
                "mean": sum(values) / len(values),
                "highest": max(values),
            }
        rows.append(row)
    return rows


def _type_rows(runs: Sequence[dict]) -> List[dict]:
    """Per part type, how many were completed over the runs: lowest, mean, highest.

    A type that drops to 0 in some combination shows a mix that shifted.
    """
    per_type: Dict[str, List[int]] = {}
    for run in runs:
        for name, counts in (run.get("types") or {}).items():
            per_type.setdefault(name, []).append(counts["completed"])
    return [
        {
            "type": name,
            "runs": len(seen),
            "lowest": min(seen),
            "mean": sum(seen) / len(seen),
            "highest": max(seen),
        }
        for name, seen in per_type.items()
    ]


def _combination_run(
    spec: Sequence[Dict[str, Any]],
    levels: Dict[str, str],
    until: float,
    seed: int,
    sigmas: float,
    quantile: float,
) -> dict:
    """Run a copy of the model once with the stations at the given bounds.

    Shipped to worker processes, so every argument pickles and failures come
    back as values.
    """
    parallel.silence_stdout()
    record, _ = _run_copy(spec_with(spec, levels, sigmas, quantile), until, seed)
    record["at"] = levels
    return record


def _run_copy(
    spec: Sequence[Dict[str, Any]],
    until: float,
    seed: int,
    no_arrivals: bool = False,
    silence: Sequence[str] = (),
) -> Tuple[Dict[str, Any], Optional[FactoryModel]]:
    """Build a copy from `spec`, run it, and count its items.

    With `no_arrivals` every source releases nothing; `silence` names single
    sources that release nothing. Returns the record and the model it ran, or
    None when the run raised.
    """
    record: Dict[str, Any] = {
        "seed": seed,
        "failed": None,
        "generated": None,
        "received": None,
        "discarded": None,
        "in_line": None,
        "missing": None,
        "shares": None,
        "processed": None,
        "sinks": None,
        "types": None,
        "sources_blocked": None,
        "breakdowns": None,
    }
    model = build_from_spec(spec)
    if no_arrivals:
        _release(model, 0, until * 10.0)
    for source_id in silence:
        model.nodes[source_id].inter_arrival_time = _gate(0, until * 10.0)
    try:
        summary = run_simulation(until, seed=seed, model=model)
    except Exception as exc:
        record["failed"] = f"{type(exc).__name__}: {exc}"
        return record, None

    balance = verify_conservation(model=model)
    in_machines = balance["in_machines"]
    record.update(
        generated=balance["generated"],
        received=balance["received"],
        discarded=balance["discarded"],
        in_line=None if in_machines is None else balance["in_edges"] + in_machines,
        missing=balance["missing"],
        shares=time_shares(model, until),
        processed=processed_counts(model),
        sinks={
            node_id: node.stats["num_item_received"]
            for node_id, node in model.nodes.items()
            if type(node).__name__ == _SINK
        },
        types={
            name: {"completed": counts["completed"], "scrapped": counts["scrapped"]}
            for name, counts in (summary.get("part_types") or {}).items()
        },
        sources_blocked={
            node_id: node.stats["total_time_spent_in_states"].get("BLOCKED_STATE", 0.0)
            for node_id, node in model.nodes.items()
            if type(node).__name__ == _SOURCE
        },
        breakdowns=sum(
            (getattr(node, "stats", None) or {}).get("num_breakdowns", 0)
            for node in model.nodes.values()
        ),
    )
    return record, model


def scaled_arrivals(spec: Sequence[Dict[str, Any]], factor: float) -> List[Dict[str, Any]]:
    """Rewrite a build spec with every inter-arrival time divided by `factor`.

    A distribution has each of its arguments divided, so its mean and its
    spread both shrink by `factor`.
    """
    rewritten: List[Dict[str, Any]] = []
    for step in spec:
        kwargs = dict(step["kwargs"])
        if step["op"] == "create_source":
            value = kwargs.get("inter_arrival_time", 1.0)
            parts = spec_parts(value)
            if parts is not None:
                name, a, b = parts
                args = f"{a / factor:g}" if b is None else f"{a / factor:g}, {b / factor:g}"
                kwargs["inter_arrival_time"] = f"{name}({args})"
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                kwargs["inter_arrival_time"] = value / factor
        rewritten.append({"op": step["op"], "kwargs": kwargs})
    return rewritten


def buffers_at(spec: Sequence[Dict[str, Any]], capacity: int) -> List[Dict[str, Any]]:
    """Rewrite a build spec with every buffer's capacity at `capacity`."""
    return [
        {
            "op": step["op"],
            "kwargs": (
                {**step["kwargs"], "capacity": capacity}
                if step["op"] == "create_buffer"
                else dict(step["kwargs"])
            ),
        }
        for step in spec
    ]


def endless_buffers(spec: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rewrite a build spec with every buffer's capacity at `ENDLESS`."""
    return buffers_at(spec, ENDLESS)


def zero_station_times(spec: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rewrite a build spec with every station's processing time at 0.

    Buffer and fleet delays stay: they are holding and travel times, not work
    at a station.
    """
    return [
        {
            "op": step["op"],
            "kwargs": (
                {**step["kwargs"], "processing_delay": 0}
                if step["op"] in _STATION_OPS
                else dict(step["kwargs"])
            ),
        }
        for step in spec
    ]


def input_plan(spec: Sequence[Dict[str, Any]]) -> List[str]:
    """The extreme inputs a model gets.

    `NO_BREAKDOWNS` only when a machine can break down. `ZERO_TIMES` not when a
    source releases a part every 0 time units, since that copy would never
    leave time 0.
    """
    cases = [NO_ARRIVALS, MORE_ARRIVALS, ENDLESS_BUFFERS, MINIMAL_BUFFERS]
    kwargs = [step["kwargs"] for step in spec]
    if any(kw.get("time_to_failure") is not None for kw in kwargs):
        cases.append(NO_BREAKDOWNS)
    if not any(
        step["op"] == "create_source" and step["kwargs"].get("inter_arrival_time") == 0
        for step in spec
    ):
        cases.append(ZERO_TIMES)
    return cases


def _input_run(spec: Sequence[Dict[str, Any]], case: str, until: float, seed: int) -> dict:
    """Run a copy of the model once with one extreme input.

    Shipped to worker processes like `_combination_run`. For `ENDLESS_BUFFERS`
    the record also names every station that was blocked although each of its
    out-edges is a buffer.
    """
    parallel.silence_stdout()
    if case == NO_ARRIVALS:
        record, model = _run_copy(spec, until, seed, no_arrivals=True)
    elif case == MORE_ARRIVALS:
        record, model = _run_copy(scaled_arrivals(spec, ARRIVAL_FACTOR), until, seed)
    elif case == ENDLESS_BUFFERS:
        record, model = _run_copy(endless_buffers(spec), until, seed)
    elif case == MINIMAL_BUFFERS:
        record, model = _run_copy(buffers_at(spec, 1), until, seed)
    elif case == NO_BREAKDOWNS:
        record, model = _run_copy(without_breakdowns(spec), until, seed)
    elif case == ZERO_TIMES:
        record, model = _run_copy(zero_station_times(spec), until, seed)
    else:
        record, model = _run_copy(spec, until, seed)
    record["case"] = case
    record["blocked"] = {}
    if model is not None and case == ENDLESS_BUFFERS:
        for node_id, node in model.nodes.items():
            outs = getattr(node, "out_edges", None) or []
            share = (record["shares"].get(node_id) or {}).get("blocked", 0.0)
            if outs and all(type(edge).__name__ == _BUFFER for edge in outs) and share > 0:
                record["blocked"][node_id] = share
    return record


def _check_inputs(runs: Sequence[dict]) -> Tuple[List[dict], List[dict]]:
    """Findings over the extreme-input runs, and one row per case that ran.

    Each case has its expectation stated in `INPUT_CASES`. The runs of a case
    in `PAIRED_CASES` are paired with the as-built runs of the same seed; their
    output is held to be lower only when the mean difference stays below 0 at
    its one-sided `ALPHA` bound.
    """
    findings: List[dict] = []
    by_case: Dict[str, List[dict]] = {}
    for run in runs:
        by_case.setdefault(run["case"], []).append(run)

    failed = [run for run in runs if run["failed"]]
    if failed:
        findings.append({
            "check": "input_failed",
            "severity": _ERROR,
            "component": None,
            "message": (
                f"{len(failed)} of {len(runs)} runs with an extreme input raised, "
                f"e.g. {failed[0]['case']}: {failed[0]['failed']}."
            ),
        })
    lost = [run for run in runs if run["missing"] not in (None, 0)]
    if lost:
        findings.append({
            "check": "input_items_lost",
            "severity": _ERROR,
            "component": None,
            "message": (
                f"In {len(lost)} of {len(runs)} runs with an extreme input the items "
                f"do not add up, e.g. {lost[0]['missing']} missing with "
                f"{lost[0]['case']}."
            ),
        })

    rows: List[dict] = []
    for case, expected in INPUT_CASES.items():
        if case not in by_case:
            continue
        own = [run for run in by_case[case] if not run["failed"]]
        row = {"case": case, "expected": expected, "runs": len(own), "ok": None}
        rows.append(row)
        if not own:
            continue

        if case == NO_ARRIVALS:
            run = own[0]
            row.update(received=run["received"], generated=run["generated"])
            row["ok"] = not run["received"] and not run["generated"]
            if not row["ok"]:
                findings.append({
                    "check": "input_no_arrivals_output",
                    "severity": _ERROR,
                    "component": None,
                    "message": (
                        f"With every source releasing nothing, {run['generated']} "
                        f"items were created and {run['received']} reached a sink."
                    ),
                })

        elif case in PAIRED_CASES:
            built = {run["seed"]: run for run in by_case.get(AS_BUILT, []) if not run["failed"]}
            pairs = [(built[run["seed"]], run) for run in own if run["seed"] in built]
            if not pairs:
                continue
            diffs = [more["received"] - base["received"] for base, more in pairs]
            mean = statistics.mean(diffs)
            half = _half_width(diffs, ALPHA)
            row.update(
                received=statistics.mean(more["received"] for _, more in pairs),
                as_built=statistics.mean(base["received"] for base, _ in pairs),
                half_width=half,
            )
            row["ok"] = bool(mean + half >= 0)
            if not row["ok"]:
                check, setting, reason = _LESS_OUTPUT[case]
                findings.append({
                    "check": check,
                    "severity": _ERROR,
                    "component": None,
                    "message": (
                        f"{setting}, the model delivered {row['received']:.1f} parts "
                        f"on average instead of {row['as_built']:.1f} as built, over "
                        f"{len(pairs)} runs. {reason}"
                    ),
                })
            if case == NO_BREAKDOWNS:
                row["breakdowns"] = sum(more.get("breakdowns") or 0 for _, more in pairs)
                if row["breakdowns"]:
                    row["ok"] = False
                    findings.append({
                        "check": "input_breakdowns_left",
                        "severity": _ERROR,
                        "component": None,
                        "message": (
                            f"With every breakdown taken out, {row['breakdowns']} "
                            f"breakdowns still happened over {len(pairs)} runs."
                        ),
                    })

        elif case == MINIMAL_BUFFERS:
            run = own[0]
            row.update(received=run["received"], generated=run["generated"])
            row["ok"] = bool(run["received"])
            if not row["ok"]:
                findings.append({
                    "check": "input_minimal_buffers_no_output",
                    "severity": _ERROR,
                    "component": None,
                    "message": (
                        "With every buffer holding 1 item, no part reached a sink "
                        f"({run['generated']} items created). The line locks up."
                    ),
                })

        elif case == ENDLESS_BUFFERS:
            run = own[0]
            row.update(received=run["received"], blocked=run["blocked"])
            row["ok"] = not run["blocked"]
            for station, share in run["blocked"].items():
                findings.append({
                    "check": "input_blocked_with_endless_buffers",
                    "severity": _ERROR,
                    "component": station,
                    "message": (
                        f"With every buffer at {ENDLESS} places, '{station}' was still "
                        f"blocked for {share:.1%} of the run, although each of its "
                        "out-edges is a buffer."
                    ),
                })
    return findings, rows


def stopped_station(
    spec: Sequence[Dict[str, Any]], station: str, until: float
) -> List[Dict[str, Any]]:
    """Rewrite a build spec with one station's processing time at 10 * `until`.

    The station takes its first part and holds it to the end of the run.
    """
    return [
        {
            "op": step["op"],
            "kwargs": (
                {**step["kwargs"], "processing_delay": until * 10.0}
                if step["kwargs"].get("id") == station
                else dict(step["kwargs"])
            ),
        }
        for step in spec
    ]


def one_branch(
    spec: Sequence[Dict[str, Any]], station: str, edge: str
) -> List[Dict[str, Any]]:
    """Rewrite a build spec so `station` sends every part to `edge`.

    The station's route shares keep their edges, with 1 for `edge` and 0 for
    the rest.
    """
    rewritten: List[Dict[str, Any]] = []
    for step in spec:
        kwargs = dict(step["kwargs"])
        if kwargs.get("id") == station and kwargs.get("route_shares"):
            kwargs["route_shares"] = {
                out: 1.0 if out == edge else 0.0 for out in kwargs["route_shares"]
            }
        rewritten.append({"op": step["op"], "kwargs": kwargs})
    return rewritten


def targeted_plan(spec: Sequence[Dict[str, Any]]) -> List[Tuple[str, str, Optional[str]]]:
    """The targeted extremes a model gets, as `(case, target, edge)`.

    Every station is stopped once. Every out-edge of a route share gets every
    part once. With two or more sources, each releases parts alone once.
    """
    plan: List[Tuple[str, str, Optional[str]]] = []
    sources: List[str] = []
    for step in spec:
        kwargs = step["kwargs"]
        if step["op"] in _STATION_OPS:
            plan.append((STOPPED, kwargs["id"], None))
        if step["op"] == "create_source":
            sources.append(kwargs["id"])
    for step in spec:
        shares = step["kwargs"].get("route_shares")
        if step["op"] == "create_machine" and shares:
            plan += [(ONE_BRANCH, step["kwargs"]["id"], edge) for edge in shares]
    if len(sources) > 1:
        plan += [(ONE_SOURCE, source, None) for source in sources]
    return plan


def _routes(model: FactoryModel) -> List[dict]:
    """The routes the parts that reached a sink took, stations only, most used first."""
    counted: Dict[Tuple[str, ...], int] = {}
    for trail in model.item_paths.values():
        if not trail or type(model.nodes.get(trail[-1])).__name__ != _SINK:
            continue
        route = tuple(place for place in trail if place in model.nodes)
        counted[route] = counted.get(route, 0) + 1
    return [
        {"route": list(route), "parts": parts}
        for route, parts in sorted(counted.items(), key=lambda pair: -pair[1])
    ]


def _targeted_run(
    spec: Sequence[Dict[str, Any]],
    case: str,
    target: str,
    edge: Optional[str],
    until: float,
    seed: int,
) -> dict:
    """Run a copy of the model once with one targeted extreme.

    Shipped to worker processes like `_combination_run`.
    """
    parallel.silence_stdout()
    if case == STOPPED:
        record, model = _run_copy(stopped_station(spec, target, until), until, seed)
    elif case == ONE_BRANCH:
        record, model = _run_copy(one_branch(spec, target, edge), until, seed)
    else:
        others = [
            step["kwargs"]["id"]
            for step in spec
            if step["op"] == "create_source" and step["kwargs"]["id"] != target
        ]
        record, model = _run_copy(spec, until, seed, silence=others)
    record.update(case=case, target=target, edge=edge, routes=[])
    if model is not None:
        record["routes"] = _routes(model)
    return record


def _check_targeted(runs: Sequence[dict]) -> List[dict]:
    """Errors over the targeted runs: a run that raised, items that do not add up.

    Everything else in these runs depends on the specification and is left to
    the reader.
    """
    findings: List[dict] = []

    def label(run: dict) -> str:
        return f"{run['case']} at '{run['target']}'" + (
            f" to '{run['edge']}'" if run["edge"] else ""
        )

    failed = [run for run in runs if run["failed"]]
    if failed:
        findings.append({
            "check": "targeted_failed",
            "severity": _ERROR,
            "component": failed[0]["target"],
            "message": (
                f"{len(failed)} of {len(runs)} targeted runs raised, e.g. "
                f"{label(failed[0])}: {failed[0]['failed']}."
            ),
        })
    lost = [run for run in runs if run["missing"] not in (None, 0)]
    if lost:
        findings.append({
            "check": "targeted_items_lost",
            "severity": _ERROR,
            "component": lost[0]["target"],
            "message": (
                f"In {len(lost)} of {len(runs)} targeted runs the items do not add "
                f"up, e.g. {lost[0]['missing']} missing with {label(lost[0])}."
            ),
        })
    return findings


def _run_tasks(fn, tasks: List[tuple]) -> List[dict]:
    """Run `fn(*task)` for every task: the first here, the rest on worker
    processes when the first run's time says that pays."""
    started = time.perf_counter()
    done = [fn(*tasks[0])]
    seconds_each = time.perf_counter() - started
    workers = parallel.choose_workers(len(tasks) - 1, seconds_each, parallel.worker_budget())
    if workers > 1:
        done += parallel.run_on_workers(fn, tasks[1:], workers)
    else:
        done += [fn(*task) for task in tasks[1:]]
    return done


def _levels_text(levels: Dict[str, str], leave_out: Optional[str] = None) -> str:
    return ", ".join(
        f"'{station}' at {level}"
        for station, level in levels.items()
        if station != leave_out
    )


def _half_width(values: Sequence[float], alpha: float) -> float:
    """One-sided Student t bound of the mean: mean + this is the upper limit.

    0 for fewer than two values, so a single run is taken as it is.
    """
    if len(values) < 2:
        return 0.0
    spread = statistics.stdev(values)
    return student_t.ppf(1 - alpha, len(values) - 1) * spread / math.sqrt(len(values))


def _more_output_text(
    station: str,
    pairs: Sequence[Tuple[dict, dict, List[int]]],
    pair_means: Sequence[Tuple[dict, dict, float, float]],
    clear: Sequence[Tuple[dict, dict, float, float]],
    mean: float,
    half: float,
    several: bool,
) -> str:
    """The message for a station whose upper bound delivered more parts."""
    worst = min(clear or pair_means, key=lambda row: row[2])
    low, high, worst_mean, _ = worst
    if not several:
        return (
            f"'{station}' at its upper bound delivered more parts than at its "
            f"lower bound in {len(clear)} of {len(pairs)} combinations, at most "
            f"{high['received']} instead of {low['received']}, with "
            f"{_levels_text(low['at'], station)}. A longer time at a station "
            "cannot add output."
        )
    runs = len(pairs[0][2])
    if mean + half < 0:
        text = (
            f"'{station}' at its upper bound delivered more parts than at its "
            f"lower bound: {-mean:.1f} more on average over {len(pairs)} "
            f"combinations with {runs} runs each, and at least "
            f"{-(mean + half):.1f} more at {1 - ALPHA:.0%} confidence over all "
            "stations."
        )
    else:
        text = (
            f"'{station}' at its upper bound delivered more parts than at its "
            f"lower bound in {len(clear)} of {len(pairs)} combinations, by more "
            f"than the spread of {runs} runs each explains."
        )
    return (
        text
        + f" The largest: {-worst_mean:.1f} more on average with "
        f"{_levels_text(low['at'], station)}. A longer time at a station cannot "
        "add output."
    )


def _check_combinations(
    stations: List[str],
    masks: List[int],
    runs: List[dict],
    until: float,
    soft: Sequence[str] = (),
    saturated: bool = False,
) -> Tuple[List[dict], List[dict]]:
    """Findings over all combinations, and each station's influence on output.

    A station's effect in one combination is the parts delivered with it at the
    lower bound minus those with it at the upper bound, every other station
    unchanged, taken per replication. `masks[i]` is the combination of
    `runs[i]`; a run's `replication` (0 when missing) pairs it with the run of
    the same seed. A negative effect breaks the rule that a longer time cannot
    add output. With one replication every negative effect is reported. With
    several, a station is reported when its mean effect stays below 0 at its
    one-sided confidence bound, or the mean effect of one combination does,
    at `ALPHA` shared out over the stations and over every pair.

    `soft` names what in the model can let a longer time add output
    (`soft_reasons`); with any, the rule gives a hint instead of an error.
    `saturated` says the sources were kept busy. Otherwise a source that was
    never held up in any run means the arrivals limit the line, and the hint
    for a station that changes nothing says so.
    """
    findings: List[dict] = []
    total = len(runs)

    failed = [run for run in runs if run["failed"]]
    if failed:
        findings.append(
            {
                "check": "combination_failed",
                "severity": _ERROR,
                "component": None,
                "message": (
                    f"{len(failed)} of {total} combinations raised while running, "
                    f"e.g. with {_levels_text(failed[0]['at'])}: "
                    f"{failed[0]['failed']}."
                ),
            }
        )

    empty = [run for run in runs if run["received"] == 0]
    if empty:
        findings.append(
            {
                "check": "combination_no_output",
                "severity": _ERROR,
                "component": None,
                "message": (
                    f"In {len(empty)} of {total} combinations no part reached a "
                    f"sink within {until:g}, e.g. with "
                    f"{_levels_text(empty[0]['at'])}."
                ),
            }
        )

    lost = [run for run in runs if run["missing"] not in (None, 0)]
    if lost:
        findings.append(
            {
                "check": "combination_items_lost",
                "severity": _ERROR,
                "component": None,
                "message": (
                    f"In {len(lost)} of {total} combinations the items do not add "
                    f"up, e.g. {lost[0]['missing']} missing with "
                    f"{_levels_text(lost[0]['at'])}."
                ),
            }
        )

    by_key = {
        (mask, run.get("replication", 0)): run for mask, run in zip(masks, runs)
    }
    replications = sorted({rep for _, rep in by_key})
    combos = sorted({mask for mask, _ in by_key})
    several = len(replications) > 1

    # Per station: (low run, high run, effect per replication) for each pair.
    per_station: Dict[str, List[Tuple[dict, dict, List[int]]]] = {}
    for i, station in enumerate(stations):
        bit = 1 << i
        pairs = []
        for mask in combos:
            if mask & bit or (mask | bit, replications[0]) not in by_key:
                continue
            effects = []
            for rep in replications:
                low, high = by_key.get((mask, rep)), by_key.get((mask | bit, rep))
                if low is None or high is None:
                    continue
                if low["received"] is None or high["received"] is None:
                    continue
                effects.append(low["received"] - high["received"])
            if effects:
                pairs.append(
                    (by_key[(mask, replications[0])], by_key[(mask | bit, replications[0])],
                     effects)
                )
        if pairs:
            per_station[station] = pairs
    all_pairs = sum(len(pairs) for pairs in per_station.values())
    arrivals_limit = not saturated and all(
        run.get("sources_blocked") is not None
        and not any(run["sources_blocked"].values())
        for run in runs
        if not run["failed"]
    )

    influence: List[dict] = []
    for station, pairs in per_station.items():
        effects = [effect for _, _, own in pairs for effect in own]
        per_rep = [
            statistics.mean(own[r] for _, _, own in pairs if r < len(own))
            for r in range(max(len(own) for _, _, own in pairs))
        ]
        mean = sum(effects) / len(effects)
        half = _half_width(per_rep, ALPHA / len(per_station))
        influence.append(
            {
                "component": station,
                "combinations": len(pairs),
                "replications": len(per_rep),
                "mean": mean,
                "half_width": half,
                "smallest": min(effects),
                "largest": max(effects),
            }
        )

        pair_means = [
            (low, high, statistics.mean(own), _half_width(own, ALPHA / all_pairs))
            for low, high, own in pairs
        ]
        clear = [row for row in pair_means if row[2] + row[3] < 0]
        if mean + half < 0 or clear:
            message = _more_output_text(
                station, pairs, pair_means, clear, mean, half, several
            )
            if soft:
                message += (
                    " This model has " + ", ".join(soft) + ", where a longer time "
                    "can move work to a quicker route or into a shift. Check "
                    "against the specification whether that is what happens here."
                )
            findings.append(
                {
                    "check": "longer_time_more_output",
                    "severity": _HINT if soft else _ERROR,
                    "component": station,
                    "message": message,
                }
            )
        elif all(abs(m) <= NO_INFLUENCE_TOLERANCE for _, _, m, _ in pair_means):
            changes = (
                f"'{station}' changes the number of parts delivered by "
                f"at most {NO_INFLUENCE_TOLERANCE}"
                + (" on average" if several else "")
                + f" in every one of the {len(pairs)} combinations"
            )
            findings.append(
                {
                    "check": "no_influence",
                    "severity": _HINT,
                    "component": station,
                    "message": changes + (
                        ". No source was ever held up by a full buffer, so the "
                        "arrivals limit the line and the station times barely "
                        "show in the output."
                        if arrivals_limit
                        else ": between its bounds it never limits the line. Check "
                        "against the specification whether this station is meant "
                        "to be this far from limiting the line."
                    ),
                }
            )

    influence.sort(key=lambda row: row["mean"], reverse=True)
    return findings, influence


def _fastest(totals: Optional[List[float]]) -> Optional[float]:
    """The quickest part's time at a place, or None when none passed."""
    return min(totals) if totals else None


def _station_rows(
    spec: Sequence[Dict[str, Any]],
    passes: Dict[str, dict],
    run_stays: Dict[str, List[Tuple[str, float, float]]],
    sigmas: float,
    quantile: float,
) -> List[dict]:
    """One row per place a part was seen at: its three times and the run's.

    The times are per visit. A part sent back over a rework loop visits a
    place again, and each visit is one time. `parts` counts the parts that
    visited the place.

    In front of a node with a setup time, a part that arrives before the setup
    is over waits for the rest of it. A row records that rest per setting for
    the quickest visit as `setup_wait`.
    """
    setups = setups_ahead(spec)
    measured: Dict[str, Dict[str, List[float]]] = {}
    waits: Dict[str, Dict[str, List[float]]] = {}
    visitors: Dict[str, Dict[str, int]] = {}
    order: List[str] = []

    for setting in SETTINGS:
        for rows, _ in passes[setting]["delivered"].values():
            for place in dict.fromkeys(place for place, _, _ in rows):
                counts = visitors.setdefault(place, {})
                counts[setting] = counts.get(setting, 0) + 1
            for place, entry, leave in rows:
                if place not in measured:
                    measured[place] = {}
                    order.append(place)
                measured[place].setdefault(setting, []).append(leave - entry)
                setup = setups[place][1] if place in setups else 0.0
                waits.setdefault(place, {}).setdefault(setting, []).append(
                    max(0.0, setup - entry)
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
    fleets = {
        step["kwargs"]["id"]: step["kwargs"]
        for step in spec
        if step["op"] == "create_fleet" and "id" in step["kwargs"]
    }
    rows_out: List[dict] = []
    for place in order:
        entered = _entered_value(spec, place)
        limits = None
        if place in fleets:
            entered, limits = _fleet_stay(fleets[place], sigmas, quantile)
        elif entered is not None:
            low, high, capped = bounds(entered, sigmas, quantile)
            limits = {"low": low, "high": high, "capped": capped}
        seen = from_run.get(place, [])
        setup_wait: Dict[str, float] = {}
        for setting, own in measured[place].items():
            quickest = min(range(len(own)), key=own.__getitem__)
            setup_wait[setting] = waits[place][setting][quickest]
        rows_out.append(
            {
                "id": place,
                "kind": "edge" if place in edges else "node",
                "entered": None if entered is None else str(entered),
                "bounds": limits,
                "setup": setups.get(place),
                "setup_wait": setup_wait,
                LOWER: _fastest(measured[place].get(LOWER)),
                "entered_run": _fastest(measured[place].get(ENTERED)),
                UPPER: _fastest(measured[place].get(UPPER)),
                "parts": {
                    setting: visitors.get(place, {}).get(setting, 0)
                    for setting in SETTINGS
                },
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


def _check_stations(rows: List[dict], several: bool = False) -> List[dict]:
    """Hold each measured time against the value the station was built with.

    With one part per source nothing queues, so a measured time must land on
    the value entered. With several parts a part can wait for the station in
    front of it, which only ever adds time, so then the times are held against
    their lower end alone. In front of a node with a setup time, a part that
    arrives before the setup is over waits for the rest of it: the stay is the
    larger of the value entered and that rest.
    """
    findings: List[dict] = []

    for row in rows:
        limits = row["bounds"]
        if limits is None:
            continue
        low, high, capped = limits["low"], limits["high"], limits["capped"]
        entered_top = high if capped else math.inf
        setup_wait = row.get("setup_wait") or {}

        for setting, key, floor, ceiling in (
            (LOWER, LOWER, low, math.inf if several else low),
            (ENTERED, "entered_run", low, math.inf if several else entered_top),
            (UPPER, UPPER, high, math.inf if several else high),
        ):
            wait = setup_wait.get(setting, 0.0)
            floor, ceiling = max(floor, wait), max(ceiling, wait)
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

        held = max(setup_wait.values(), default=0.0)
        if row.get("setup") and held > low + TOLERANCE:
            node, setup = row["setup"]
            findings.append(
                {
                    "check": "setup_wait",
                    "severity": _INFO,
                    "component": row["id"],
                    "message": (
                        f"'{row['id']}' held the part up to {held:g} instead of its "
                        f"own {low:g}: '{node}' was still in its setup of {setup:g} "
                        "when the part arrived."
                    ),
                }
            )

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
            fastest = row["run"]["fastest"]
            if has_real_floor(row["entered"]):
                findings.append(
                    {
                        "check": "below_station_floor",
                        "severity": _ERROR,
                        "component": row["id"],
                        "message": (
                            f"In the run a part passed '{row['id']}' in "
                            f"{fastest:g}, below the {low:g} its own value allows."
                        ),
                    }
                )
            else:
                findings.append(
                    {
                        "check": "below_station_floor",
                        "severity": _HINT,
                        "component": row["id"],
                        "message": (
                            f"In the run a part passed '{row['id']}' in "
                            f"{fastest:g}, below the {low:g} taken as its lower "
                            f"bound. {row['entered']} can draw this; check "
                            "whether the real process allows it."
                        ),
                    }
                )

    return findings


def _check_setups(
    spec: Sequence[Dict[str, Any]], sigmas: float = SIGMAS, quantile: float = QUANTILE
) -> List[dict]:
    """A hint for every station whose setup time is longer than one processing time.

    A setup runs once, at the start of the run. A processing time entered as a
    setup time shows up here, even when its station is never measured.
    """
    findings: List[dict] = []
    for step in spec:
        kwargs = step["kwargs"]
        setup = kwargs.get("node_setup_time") or 0
        if step["op"] not in _STATION_OPS or setup <= 0:
            continue
        _, high, _ = bounds(kwargs.get("processing_delay", 0), sigmas, quantile)
        if setup <= high:
            continue
        per_part = (
            "no processing time"
            if high == 0
            else f"a processing time of at most {high:g}"
        )
        findings.append(
            {
                "check": "setup_longer_than_processing",
                "severity": _HINT,
                "component": kwargs["id"],
                "message": (
                    f"'{kwargs['id']}' has a setup time of {setup:g} and {per_part}. "
                    "The setup runs once, at the start of the run. Check whether "
                    f"{setup:g} is meant as the time per part."
                ),
            }
        )
    return findings


def _targeted_lines(targeted: dict) -> List[str]:
    """The targeted extremes as summary lines, one block per case."""
    runs = targeted["runs"]
    if not runs:
        return []

    def sinks_text(sinks: Optional[Dict[str, int]]) -> str:
        reached = {sink: n for sink, n in (sinks or {}).items() if n}
        return ", ".join(f"{sink} {n}" for sink, n in reached.items()) or "no sink"

    def types_text(types: Optional[Dict[str, Dict[str, int]]]) -> str:
        if not types:
            return ""
        done = sum(counts["completed"] for counts in types.values())
        scrapped = sum(counts["scrapped"] for counts in types.values())
        return f"; counted as completed {done}, scrapped {scrapped}"

    lines = [
        "",
        f"  targeted extremes, once each with seed {targeted['seed']}, for the "
        "reader to hold against the specification:",
    ]
    built = targeted["as_built"]
    if built is not None:
        lines.append(
            f"  as built: {built['received']} parts delivered "
            f"({sinks_text(built['sinks'])}){types_text(built['types'])}"
        )
    for case, expected in targeted["expected"].items():
        own = [run for run in runs if run["case"] == case]
        if not own:
            continue
        lines += ["", f"    {case}. Expected: {expected}."]
        for run in own:
            name = run["target"] + (f" > {run['edge']}" if run["edge"] else "")
            if run["failed"]:
                lines.append(f"      {name:<24} failed: {run['failed']}")
                continue
            if case == STOPPED:
                routes = "; ".join(
                    f"{' > '.join(r['route'])} ({r['parts']})" for r in run["routes"][:2]
                )
                lines.append(
                    f"      {name:<24} {run['received']:>5} parts"
                    + (f"   via {routes}" if routes else "")
                )
            elif case == ONE_BRANCH:
                lines.append(
                    f"      {name:<24} {run['received']:>5} parts: "
                    f"{sinks_text(run['sinks'])}{types_text(run['types'])}"
                )
            else:
                processed = run["processed"] or {}
                working = [s for s, n in processed.items() if n]
                idle = [s for s, n in processed.items() if not n]
                lines.append(
                    f"      {name:<24} {run['received']:>5} parts; working: "
                    f"{', '.join(working) or 'none'}; idle: {', '.join(idle) or 'none'}"
                )
    return lines


def _summary(report: dict) -> str:
    """The report as a short block of text."""
    settings = report["settings"]
    seed = settings["seed"]
    released = settings.get("released") or {}
    per_source = (
        "items released per source: "
        + ", ".join(f"{name} {count}" for name, count in released.items())
        if any(count > 1 for count in released.values())
        else f"{settings['parts_per_source']} part(s) per source"
    )
    lines = [
        "Limit test",
        f"  {per_source}, run to "
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

    if settings.get("queued", settings["parts_per_source"] > 1):
        lines.append(
            "  with more than one part per source the columns show the "
            "quickest part, and only the lower end is held against the value "
            "entered: waiting for the station in front can only add time"
        )

    run = report["run"]
    if run:
        lines.append(
            f"  the run's {run['n']} parts: {run['fastest']:.2f} fastest, "
            f"{run['mean']:.2f} mean, {run['slowest']:.2f} slowest"
        )
        lines.append(
            "  the run's times include waiting, so they are held against the "
            "floor alone"
        )
    for part in report["below_floor"]:
        lines.append(
            f"    below the floor: {part['item']} in {part['total']:.2f} via "
            + " > ".join(part["route"])
        )

    combos = report["combinations"]
    if combos["runs"]:
        count = len(combos["stations"])
        scope = (
            "every station at min or max"
            if combos["complete"]
            else f"of {1 << count}: both corners and each station against the rest"
        )
        reps = combos["replications"]
        runs_text = (
            f"{len(combos['runs'])} runs, {scope}, seed {combos['seed']}"
            if reps == 1
            else f"{len(combos['runs'])} runs, {scope}, {reps} runs each with "
            f"seeds {combos['seed']} to {combos['seed'] + (reps - 1) * SEED_STRIDE} "
            f"in steps of {SEED_STRIDE}"
        )
        if combos["arrivals"] == SATURATED:
            arrivals_text = (
                "  every source releases its next part as soon as there is room "
                f"({combos['arrivals_why']}), so the counts are the capacity of "
                "the line"
            )
        else:
            arrivals_text = (
                f"  arrivals as entered ({combos['arrivals_why']})"
            )
        random_text = (
            "  still random: " + ", ".join(combos["random"])
            if combos["random"]
            else "  nothing random is left, so one run per combination, and a "
            "second seed for the first combination as a check"
        )
        lines += [
            "",
            f"  combinations: {runs_text}, run to {settings['until']:g}",
            arrivals_text,
            random_text,
            "  sensitivity (Sargent): parts delivered at the lower bound minus "
            "at the upper bound, per station:",
            "    station            mean"
            + ("      +/-" if reps > 1 else "")
            + "    smallest  largest",
        ]
        for row in combos["influence"]:
            spread = f"  {row['half_width']:>6.1f} " if reps > 1 else ""
            lines.append(
                f"    {row['component']:<16} {row['mean']:>6.1f}{spread}"
                f"    {row['smallest']:>6d}  {row['largest']:>6d}"
            )
        if reps > 1:
            lines.append(
                f"    +/- is the one-sided {1 - ALPHA:.0%} bound of the mean over "
                "the runs, shared out over the stations"
            )
        if combos["time_shares"]:
            lines += [
                "",
                "  share of the run per station, mean over the combinations "
                "(lowest to highest):",
                "    station            working           waiting           blocked",
            ]
            for row in combos["time_shares"]:
                cells = [
                    f"{row[key]['mean']:>4.0%} ({row[key]['lowest']:>4.0%}-"
                    f"{row[key]['highest']:>4.0%})"
                    for key in ("working", "waiting", "blocked")
                ]
                lines.append(f"    {row['component']:<16} " + "  ".join(cells))
        if combos["types"]:
            lines += ["", "  parts completed per type over the combinations "
                      "(lowest, mean, highest):"]
            lines += [
                f"    {row['type']:<16} {row['lowest']:>6d} {row['mean']:>8.1f} "
                f"{row['highest']:>6d}"
                for row in combos["types"]
            ]

    cases = report["inputs"]["cases"]
    if cases:
        lines += ["", "  extreme inputs, each with what a real plant would do:"]
        for row in cases:
            verdict = {True: "as expected", False: "NOT as expected", None: "not run"}[row["ok"]]
            if row["case"] in PAIRED_CASES and row["ok"] is not None:
                got = (f"{row['received']:.1f} parts against {row['as_built']:.1f} as built, "
                       f"mean of {row['runs']} runs")
                if "breakdowns" in row:
                    got += f", {row['breakdowns']} breakdowns"
            elif row["case"] == ENDLESS_BUFFERS and row["ok"] is not None:
                got = f"{len(row['blocked'])} station(s) blocked, {row['received']} parts"
            elif row["ok"] is not None:
                got = f"{row['generated']} items created, {row['received']} parts"
            else:
                got = "-"
            lines += [f"    {row['case']:<16} {row['expected']}", f"{'':21}{got}: {verdict}"]

    lines += _targeted_lines(report["targeted"])

    errors = [f for f in report["findings"] if f["severity"] == _ERROR]
    hints = [f for f in report["findings"] if f["severity"] == _HINT]
    notes = [f for f in report["findings"] if f["severity"] == _INFO]
    if errors:
        lines += ["", "  what does not add up:"]
        lines += [f"    {finding['message']}" for finding in errors]
    if hints:
        lines += ["", "  worth a look:"]
        lines += [f"    {finding['message']}" for finding in hints]
    if notes:
        lines += ["", "  for information:"]
        lines += [f"    {finding['message']}" for finding in notes]

    lines += ["", f"  verdict: {'passed' if report['passed'] else 'FAILED'}"]
    return "\n".join(lines)


def verify_extreme_conditions(
    until: float,
    seed: Optional[int] = None,
    parts_per_source: int = 1,
    sigmas: float = SIGMAS,
    quantile: float = QUANTILE,
    replications: int = REPLICATIONS,
    *,
    model: FactoryModel | None = None,
) -> dict:
    """Send one part through the model at its lower bound, as built, and at its upper bound.

    With a single part in the plant nothing queues, so the time it spends at a
    station is that station's own time. Every number returned is read off a run.

    Args:
        until: end time for each of the three runs; must be a positive number.
            Long enough for a part to reach a sink at the upper bound.
        seed: RNG seed for the run at the built values and for every
            combination run; the combinations use 0 when it is None. At the
            bounds only the arrivals draw.
        parts_per_source: items each source releases at time 0. One means
            nothing queues anywhere, which is what makes a measured time
            comparable to the value entered. A source that feeds a combiner
            releases at least the items the combiner takes from its edge,
            whatever this is. Where parts queue, each place reports its
            quickest part and is held against its lower end alone.
        sigmas: how far a normal/gauss reaches each way, in standard
            deviations.
        quantile: the quantile standing in for an exp's missing upper bound.
        replications: runs per combination. Run r uses seed + r * 1000 in
            every combination. Breakdowns, route shares and several random
            sources still draw at the bounds, so one run per combination can
            show a difference that is only noise.
        model: the model to check; defaults to the session model. Only its
            build spec and its last run's events are read.

    Returns:
        A dict with `summary` (read this first), `passed`, `stations` (per
        place: the value entered, the three measured times, and the run's
        fastest/mean/slowest), `single` (the part's total per setting), `run`
        (the last run's parts, or None when nothing has run), `below_floor`
        (parts quicker than the single part at the lower bound, with their
        route), `combinations` (the stations varied, whether every combination
        ran, the seed, one record per run with each station's share of the run
        spent working, waiting and blocked and the items it processed, each
        station's influence on the
        parts delivered, and the shares' range over all runs), `findings`,
        `substitutions` and `settings`. `inputs` holds the extreme-input
        cases (`cases`: each with its expectation, result and `ok`) and their
        runs. `targeted` holds the targeted extremes: their expectations, the
        model as built at the same seed, and one record per run with the parts
        per sink, the per-type counts, each station's processed items and the
        routes the delivered parts took.

    Raises:
        ValueError: if `until` is not a positive number, `parts_per_source` or
            `replications` is not a positive int, `quantile` is not between 0
            and 1, or the model has no recorded build steps.
    """
    require_positive_number("until", until)
    for name, value in (("parts_per_source", parts_per_source), ("replications", replications)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an int (got {value!r}).")
        if value < 1:
            raise ValueError(f"{name} must be a positive int (>= 1).")
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
                        f"{until:g}.{stuck} Raise `until`, or `parts_per_source`."
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
    released = {
        name: max(parts_per_source, count)
        for name, count in combiner_release(spec).items()
    }
    queued = parts_per_source > 1 or any(count > 1 for count in released.values())
    findings += _check_stations(stations, queued)

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
            route = [p for p, _, _ in rows]
            own_floor, real = route_floor(spec, route, sigmas, quantile)
            # Quicker than its own route allows: a draw below a chosen bound,
            # unless every bound on the route is real. Otherwise the route
            # itself is shorter than the single part's.
            drawn = total < own_floor - TOLERANCE and not real
            below_floor.append(
                {
                    "item": item,
                    "total": total,
                    "route": route,
                    "route_floor": own_floor,
                    "cause": "drawn below bound" if drawn else "shorter route",
                }
            )

        def listed(parts: List[dict]) -> str:
            return "; ".join(
                f"{part['item']} ({part['total']:g}) via " + " > ".join(part["route"])
                for part in parts[:3]
            )

        shorter = [p for p in below_floor if p["cause"] == "shorter route"]
        drawn_low = [p for p in below_floor if p["cause"] == "drawn below bound"]
        if shorter:
            findings.append(
                {
                    "check": "below_floor",
                    "severity": _ERROR,
                    "component": None,
                    "message": (
                        f"{len(shorter)} of {len(totals)} parts in the run "
                        f"were quicker than {floor:g}, the total of one part "
                        "with every station at its shortest, on a route that "
                        "leaves out a station: " + listed(shorter)
                    ),
                }
            )
        if drawn_low:
            findings.append(
                {
                    "check": "below_floor",
                    "severity": _HINT,
                    "component": None,
                    "message": (
                        f"{len(drawn_low)} of {len(totals)} parts in the run "
                        f"were quicker than {floor:g}, on a full route; a "
                        "normal or exp time drew below the bound taken for it: "
                        + listed(drawn_low)
                    ),
                }
            )

    findings += _check_setups(spec, sigmas, quantile)

    stations_ranged = ranged_stations(spec)
    masks, complete = combination_masks(len(stations_ranged))
    combo_seed = seed if seed is not None else 0

    # Keep the sources busy when the mix cannot shift, so the output is what
    # the stations allow. A route that takes no time at the lower bounds would
    # let a busy source release parts without end at one point in time.
    if not mix_is_fixed(spec):
        arrivals, arrivals_why = AS_ENTERED, "two sources share a station before any combiner"
    elif zero_time_route(spec, sigmas, quantile):
        arrivals, arrivals_why = (
            AS_ENTERED, "a part can reach a sink in no time at the lower bounds"
        )
    else:
        arrivals, arrivals_why = SATURATED, "the product mix cannot shift"
    # Breakdowns at their means: the combinations ask how the capacity moves
    # with the station times, and a random breakdown only adds noise to that.
    combo_spec = breakdowns_at_means(
        saturated_arrivals(spec) if arrivals == SATURATED else spec
    )
    still_random = random_elements(combo_spec)
    combo_reps = replications if still_random else 1

    combo_runs: List[dict] = []
    run_masks: List[int] = []
    influence: List[dict] = []
    repeat: Optional[dict] = None
    if stations_ranged:
        tasks = []
        for mask in masks:
            levels = {
                station: UPPER if mask >> i & 1 else LOWER
                for i, station in enumerate(stations_ranged)
            }
            for rep in range(combo_reps):
                tasks.append(
                    (combo_spec, levels, until, combo_seed + rep * SEED_STRIDE,
                     sigmas, quantile)
                )
                run_masks.append(mask)
        if not still_random:
            # Nothing should draw: the first combination again with another
            # seed must deliver the same.
            first = tasks[0]
            tasks.append((first[0], first[1], until, combo_seed + SEED_STRIDE, sigmas, quantile))
        combo_runs = _run_tasks(_combination_run, tasks)
        if not still_random:
            repeat = combo_runs.pop()
            if not repeat["failed"] and not combo_runs[0]["failed"] and (
                repeat["received"], repeat["generated"]
            ) != (combo_runs[0]["received"], combo_runs[0]["generated"]):
                findings.append(
                    {
                        "check": "combination_not_repeatable",
                        "severity": _HINT,
                        "component": None,
                        "message": (
                            "Nothing in the combination copies should draw from the "
                            f"random stream, yet seed {repeat['seed']} delivered "
                            f"{repeat['received']} parts and seed {combo_seed} "
                            f"{combo_runs[0]['received']}, with "
                            f"{_levels_text(repeat['at'])}. Something random is "
                            "left in the model, so one run per combination may "
                            "show noise."
                        ),
                    }
                )
        for n, record in enumerate(combo_runs):
            record["replication"] = n % combo_reps
        combo_findings, influence = _check_combinations(
            stations_ranged, run_masks, combo_runs, until,
            soft=soft_reasons(spec), saturated=arrivals == SATURATED,
        )
        findings += combo_findings

    # Extreme inputs: as built and each paired case once per seed, the rest
    # once.
    cases = input_plan(spec)
    seeds = [combo_seed + rep * SEED_STRIDE for rep in range(replications)]
    input_tasks = [
        (spec, case, until, s)
        for case in [AS_BUILT] + [c for c in cases if c in PAIRED_CASES]
        for s in seeds
    ]
    input_tasks += [
        (spec, case, until, combo_seed) for case in cases if case not in PAIRED_CASES
    ]
    input_runs = _run_tasks(_input_run, input_tasks)
    input_findings, input_rows = _check_inputs(input_runs)
    findings += input_findings

    # Targeted extremes, once each with the first seed.
    plan = targeted_plan(spec)
    targeted_runs = (
        _run_tasks(
            _targeted_run,
            [(spec, case, target, edge, until, combo_seed) for case, target, edge in plan],
        )
        if plan
        else []
    )
    findings += _check_targeted(targeted_runs)
    as_built = next(
        (run for run in input_runs
         if run["case"] == AS_BUILT and run["seed"] == combo_seed and not run["failed"]),
        None,
    )

    report = {
        "passed": not any(f["severity"] == _ERROR for f in findings),
        "stations": stations,
        "single": single,
        "run": run,
        "below_floor": below_floor,
        "combinations": {
            "stations": stations_ranged,
            "bounds": {
                station: dict(
                    zip(("low", "high", "capped"),
                        bounds(_entered_value(spec, station), sigmas, quantile)),
                    entered=str(_entered_value(spec, station)),
                )
                for station in stations_ranged
            },
            "breakdowns_at_means": any(
                spec_parts(step["kwargs"].get(field))
                for step in spec for field in _BREAKDOWN_FIELDS
            ),
            "complete": complete,
            "seed": combo_seed,
            "replications": combo_reps,
            "arrivals": arrivals,
            "arrivals_why": arrivals_why,
            "random": still_random,
            "repeat": repeat,
            "runs": combo_runs,
            "influence": influence,
            "time_shares": _share_rows(combo_runs),
            "types": _type_rows(combo_runs),
        },
        "inputs": {"cases": input_rows, "runs": input_runs},
        "targeted": {
            "expected": TARGETED_CASES,
            "seed": combo_seed,
            "as_built": None if as_built is None else {
                "received": as_built["received"],
                "sinks": as_built["sinks"],
                "types": as_built["types"],
            },
            "runs": targeted_runs,
        },
        "findings": findings,
        "substitutions": {
            LOWER: passes[LOWER]["substitutions"],
            UPPER: passes[UPPER]["substitutions"],
        },
        "settings": {
            "until": until,
            "seed": seed,
            "parts_per_source": parts_per_source,
            "released": released,
            "queued": queued,
            "sigmas": sigmas,
            "quantile": quantile,
            "replications": replications,
        },
    }
    report["summary"] = _summary(report)
    return report
