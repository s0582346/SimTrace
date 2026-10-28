"""The time course of a run: every edge's level and every station's state over
the simulated time.

A copy of the model is built from its build spec and run with the given seed.
A sampler process reads, every `until / SAMPLES` time units, the number of items
in each edge, the state of each station and source (processing, blocked, idle,
off shift, broken down) and each station's count of finished parts. The sampler
draws no random numbers, so with the same seed the copy is the run
run_simulation makes. Times in the findings are sample times, accurate to one
sample step.

Findings, all of severity "hint":

    line_stop          no station finishes a part in [t, until]
    no_output_to_end   a station finishes no part in [t, until]
    late_start         a station finishes its first part at t >= until * TAIL_SHARE
    blocked_to_end     a station or source is blocked, off shift or broken down
                       in [t, until] and blocked in part of it
    full_to_end        a buffer's level equals its capacity in [t, until]
    circular_wait      stations that are blocked_to_end, each on a buffer that is
                       full_to_end and feeds the next, close a circle: none of
                       them can move again. The message adds each
                       FIRST_AVAILABLE station that takes from a full in-edge
                       before the circle's one
    mostly_full       a buffer's level equals its capacity in at least
                       FULL_SHARE of the samples in [until / 2, until]
    input_starved      a combiner's in-edge is empty while another in-edge is
                       at capacity in at least STARVED_SHARE of the samples in
                       [until / 2, until]
    daily_swing        with a shift calendar and a run of two or more of its
                       cycles: a buffer's mean level per 1/24 of the cycle
                       differs by at least SWING_SHARE of its capacity. The
                       message adds the time per cycle the node feeding the
                       buffer is blocked while the buffer is at capacity

The `*_to_end` findings need [t, until] to cover at least TAIL_SHARE of the
run. Conveyors and fleets are left out of the level findings: a conveyor holds
its items while they travel, a fleet holds them until it departs.

The session model is only read.
"""

from __future__ import annotations

import contextlib
import io
from typing import Any, Dict, List, Optional, Sequence, Tuple

from simtrace.model import FactoryModel
from simtrace.model import get_model as get_session_model
from simtrace.tools.simulation.lifecycle import run_simulation
from simtrace.tools.simulation.rebuild import build_from_spec
from simtrace.tools.utils import require_positive_number

SAMPLES = 2000
TAIL_SHARE = 0.25
FULL_SHARE = 0.5
STARVED_SHARE = 0.25
SWING_SHARE = 0.75

PROCESSING = "processing"
BLOCKED = "blocked"
IDLE = "idle"
OFF = "off_shift"
DOWN = "down"
STATES = (PROCESSING, BLOCKED, IDLE, OFF, DOWN)

_HINT = "hint"
_STATIONS = ("Machine", "Splitter", "Combiner")
_WORKING = ("Machine", "Splitter", "Combiner", "Source")


def _store(edge: object) -> object:
    store = getattr(edge, "inbuiltstore", None)
    return store if store is not None else getattr(edge, "belt", None)


def _level(edge: object) -> int:
    store = _store(edge)
    return len(store.items) + len(store.ready_items)


def _capacity(edge: object) -> int:
    return getattr(edge, "capacity", None) or getattr(_store(edge), "capacity", 1)


def _state(node: object, now: float) -> str:
    """The state of a station or source at `now`."""
    calendar = getattr(node, "calendar", None)
    if getattr(node, "off_shift", False) or (
        calendar is not None and type(node).__name__ == "Source" and not calendar.is_working(now)
    ):
        return OFF
    if getattr(node, "down", False):
        return DOWN
    workers = getattr(node, "worker_thread_list", None)
    if workers is not None:
        if any(getattr(p, "thread_state", None) == "PROCESSING_STATE" for p in workers):
            return PROCESSING
        if any(getattr(p, "thread_state", None) == "BLOCKED_STATE" for p in workers):
            return BLOCKED
        return IDLE
    return BLOCKED if "BLOCKED" in str(getattr(node, "state", "")) else IDLE


def _finished(node: object) -> int:
    stats = getattr(node, "stats", None) or {}
    return stats.get("num_item_processed", 0)


def flow_order(model: FactoryModel) -> List[str]:
    """Node and edge ids from the sources on, each edge after the node it
    leaves. Sinks are left out."""
    order: List[str] = []
    seen = set()
    queue = [n for n, node in model.nodes.items() if type(node).__name__ == "Source"]
    while queue:
        node_id = queue.pop(0)
        if node_id in seen:
            continue
        seen.add(node_id)
        node = model.nodes[node_id]
        if type(node).__name__ != "Sink":
            order.append(node_id)
        for edge in getattr(node, "out_edges", None) or []:
            if edge.id in seen:
                continue
            seen.add(edge.id)
            order.append(edge.id)
            dest = getattr(edge, "dest_node", None)
            if dest is not None:
                queue.append(dest.id)
    order += [n for n, node in model.nodes.items()
              if n not in seen and type(node).__name__ != "Sink"]
    order += [e for e in model.edges if e not in seen]
    return order


def _record(model: FactoryModel, until: float, seed: Optional[int]) -> Dict[str, Any]:
    step = until / SAMPLES
    working = [n for n, node in model.nodes.items() if type(node).__name__ in _WORKING]
    samples: Dict[str, Any] = {
        "t": [],
        "levels": {e: [] for e in model.edges},
        "states": {n: [] for n in working},
        "finished": {n: [] for n, node in model.nodes.items() if type(node).__name__ in _STATIONS},
    }

    def sampler():
        while True:
            yield model.env.timeout(step)
            now = model.env.now
            samples["t"].append(now)
            for edge_id, edge in model.edges.items():
                samples["levels"][edge_id].append(_level(edge))
            for node_id in working:
                samples["states"][node_id].append(_state(model.nodes[node_id], now))
            for node_id in samples["finished"]:
                samples["finished"][node_id].append(_finished(model.nodes[node_id]))

    model.env.process(sampler())
    with contextlib.redirect_stdout(io.StringIO()):
        run_simulation(until, seed=seed, model=model)
    return samples


def _tail(values: Sequence, test) -> Optional[int]:
    """Index from which `test` holds for every value to the end, or None."""
    i = len(values)
    while i > 0 and test(values[i - 1]):
        i -= 1
    return i if i < len(values) else None


def _t(value: float) -> str:
    return f"{round(value, 1):g}"


def _share(part: int, whole: int) -> str:
    if not whole or not part:
        return "0 %"
    percent = 100 * part / whole
    return "< 1 %" if percent < 1 else f"{round(percent)} %"


def _cycle(model: FactoryModel) -> Optional[float]:
    if model.default_calendar is not None:
        return model.calendars[model.default_calendar].cycle
    for calendar in model.calendars.values():
        return calendar.cycle
    return None


def _finding(check: str, component: Optional[str], message: str, details: dict) -> dict:
    return {"check": check, "severity": _HINT, "component": component, "message": message,
            "details": details}


def _circular_waits(model: FactoryModel, blocked_from: Dict[str, float],
                    full_from: Dict[str, float], until: float) -> List[dict]:
    """One circular_wait per circle of stuck stations and full buffers.

    A blocked station waits for a place in one of its out-edges, so in one
    that is full. A full buffer waits for its destination to take a part. A
    circle of these never moves again. Each circle is reported once, starting
    at the station that comes first in the flow.
    """
    waits: Dict[str, List[Tuple[str, str]]] = {}
    for node_id in blocked_from:
        for edge in getattr(model.nodes[node_id], "out_edges", None) or []:
            dest = getattr(edge, "dest_node", None)
            if edge.id in full_from and dest is not None and dest.id in blocked_from:
                waits.setdefault(node_id, []).append((edge.id, dest.id))

    rank = {cid: k for k, cid in enumerate(flow_order(model))}
    circles: List[List[str]] = []

    def walk(first: str, node_id: str, path: List[str]) -> None:
        for edge_id, dest in waits.get(node_id, []):
            if dest == first:
                circles.append(path + [edge_id])
            elif dest not in path and rank[dest] > rank[first]:
                walk(first, dest, path + [edge_id, dest])

    for node_id in sorted(waits, key=rank.get):
        walk(node_id, node_id, [node_id])

    found = []
    for circle in circles:
        frm = max([blocked_from[c] for c in circle[::2]] + [full_from[c] for c in circle[1::2]])
        steps = "; ".join(
            f"{circle[k]} blocked on full {circle[k + 1]}, which feeds "
            f"{circle[(k + 2) % len(circle)]}"
            for k in range(0, len(circle), 2)
        )
        # A FIRST_AVAILABLE station takes from its in-edges in connect order,
        # so a full in-edge before the circle's one keeps the circle full.
        priority = []
        for k in range(0, len(circle), 2):
            node = model.nodes[circle[k]]
            if getattr(node, "in_edge_selection", None) != "FIRST_AVAILABLE":
                continue
            ins = [e.id for e in getattr(node, "in_edges", None) or []]
            inbound = circle[k - 1]
            earlier = [e for e in ins[:ins.index(inbound)] if e in full_from] if inbound in ins else []
            if earlier:
                priority.append({"station": circle[k], "first": earlier, "then": inbound})
        notes = "".join(
            f" {p['station']} takes from {', '.join(p['first'])} before {p['then']} "
            f"(FIRST_AVAILABLE), and {', '.join(p['first'])} "
            f"{'is' if len(p['first']) == 1 else 'are'} full."
            for p in priority
        )
        found.append(_finding(
            "circular_wait", circle[0],
            f"Circular wait in [{_t(frm)}, {_t(until)}]: {steps}.{notes}",
            {"from": frm, "to": until, "circle": circle, "priority": priority}))
    return found


def _findings(model: FactoryModel, samples: Dict[str, Any], until: float) -> List[dict]:
    t = samples["t"]
    n = len(t)
    if not n:
        return []
    tail_min = TAIL_SHARE * n
    half = n // 2
    step = until / SAMPLES
    found: List[dict] = []

    def start(i: int) -> float:
        # The last sample before the tail; the tail began after it.
        return t[i - 1] if i > 0 else 0.0

    stops: Dict[str, float] = {}
    for node_id, counts in samples["finished"].items():
        i = _tail(counts, lambda v, last=counts[-1]: v == last)
        if i is not None and n - i >= tail_min:
            frm = start(i)
            stops[node_id] = frm
            states = samples["states"][node_id][i:]
            parts = ", ".join(
                f"{s} {_share(states.count(s), len(states))}"
                for s in sorted(STATES, key=states.count, reverse=True) if states.count(s))
            found.append(_finding(
                "no_output_to_end", node_id,
                f"0 parts finished in [{_t(frm)}, {_t(until)}]. States in it: {parts}.",
                {"from": frm, "to": until,
                 "states": {s: states.count(s) / len(states) for s in STATES if states.count(s)}}))
        if counts[-1] > 0:
            first = next(k for k, v in enumerate(counts) if v > 0)
            if first >= tail_min:
                found.append(_finding(
                    "late_start", node_id, f"First part finished at {_t(t[first])}.",
                    {"first": t[first]}))
    if samples["finished"] and len(stops) == len(samples["finished"]):
        frm = min(stops.values())
        found.insert(0, _finding(
            "line_stop", None,
            f"No station finishes a part in [{_t(max(stops.values()))}, {_t(until)}]. "
            f"The first stops at {_t(frm)}.",
            {"from": max(stops.values()), "to": until, "first_stop": frm,
             "stops": dict(sorted(stops.items(), key=lambda kv: kv[1]))}))

    blocked_from: Dict[str, float] = {}
    for node_id, states in samples["states"].items():
        i = _tail(states, lambda s: s in (BLOCKED, OFF, DOWN))
        if i is not None and n - i >= tail_min and BLOCKED in states[i:]:
            tail = states[i:]
            frm = start(i)
            blocked_from[node_id] = frm
            found.append(_finding(
                "blocked_to_end", node_id,
                f"State blocked, off shift or down in [{_t(frm)}, {_t(until)}], blocked in "
                f"{_share(tail.count(BLOCKED), len(tail))} of it.",
                {"from": frm, "to": until, "blocked": tail.count(BLOCKED) / len(tail)}))

    buffers = [e for e, edge in model.edges.items() if type(edge).__name__ == "Buffer"]
    full_from: Dict[str, float] = {}
    for edge_id in buffers:
        levels = samples["levels"][edge_id]
        cap = _capacity(model.edges[edge_id])
        i = _tail(levels, lambda v: v >= cap)
        if i is not None and n - i >= tail_min:
            frm = start(i)
            full_from[edge_id] = frm
            found.append(_finding(
                "full_to_end", edge_id,
                f"Level = capacity ({cap}) in [{_t(frm)}, {_t(until)}].",
                {"capacity": cap, "from": frm, "to": until}))
        else:
            second = levels[half:]
            full = sum(v >= cap for v in second)
            if full >= FULL_SHARE * len(second):
                found.append(_finding(
                    "mostly_full", edge_id,
                    f"Level = capacity ({cap}) in {_share(full, len(second))} of "
                    f"[{_t(t[half] - step)}, {_t(until)}].",
                    {"capacity": cap, "share": full / len(second), "from": t[half] - step,
                     "to": until}))

    found += _circular_waits(model, blocked_from, full_from, until)

    for node_id, node in model.nodes.items():
        if type(node).__name__ != "Combiner":
            continue
        ins = [e.id for e in getattr(node, "in_edges", None) or [] if e.id in samples["levels"]]
        pairs: Dict[Tuple[str, str], int] = {}
        for k in range(half, n):
            empty = [e for e in ins if samples["levels"][e][k] == 0]
            full = [e for e in ins if samples["levels"][e][k] >= _capacity(model.edges[e])]
            for a in empty:
                for b in full:
                    pairs[(a, b)] = pairs.get((a, b), 0) + 1
        for (a, b), count in pairs.items():
            if count >= STARVED_SHARE * (n - half):
                found.append(_finding(
                    "input_starved", node_id,
                    f"{a} level 0 while {b} level = capacity ({_capacity(model.edges[b])}) in "
                    f"{_share(count, n - half)} of [{_t(t[half] - step)}, {_t(until)}].",
                    {"empty": a, "full": b, "share": count / (n - half),
                     "from": t[half] - step, "to": until}))

    cycle = _cycle(model)
    if cycle and until >= 2 * cycle:
        width = cycle / 24
        for edge_id in buffers:
            cap = _capacity(model.edges[edge_id])
            if cap < 2:
                continue
            bins: Dict[int, List[int]] = {}
            for time, v in zip(t, samples["levels"][edge_id]):
                bins.setdefault(int(time % cycle // width), []).append(v)
            means = {b: sum(vs) / len(vs) for b, vs in bins.items()}
            high = max(means, key=means.get)
            low = min(means, key=means.get)
            if means[high] - means[low] >= SWING_SHARE * cap:
                message = (
                    f"Mean level {means[low]:.1f} in [{_t(low * width)}, {_t((low + 1) * width)}) "
                    f"and {means[high]:.1f} in [{_t(high * width)}, {_t((high + 1) * width)}) "
                    f"of the cycle ({_t(cycle)}), capacity {cap}.")
                details = {"capacity": cap, "cycle": cycle, "low": means[low],
                           "low_from": low * width, "high": means[high],
                           "high_from": high * width}
                src = getattr(getattr(model.edges[edge_id], "src_node", None), "id", None)
                if src in samples["states"]:
                    held = sum(s == BLOCKED and v >= cap for s, v in
                               zip(samples["states"][src], samples["levels"][edge_id]))
                    per_cycle = held * step * cycle / until
                    message += f" {src} blocked while it is at capacity: {_t(per_cycle)} per cycle."
                    details.update(src=src, src_blocked_per_cycle=per_cycle)
                found.append(_finding("daily_swing", edge_id, message, details))
    return found


def _changes(times: Sequence[float], values: Sequence) -> List[list]:
    """[time, value] at every sample where the value differs from the one before."""
    out: List[list] = []
    for time, value in zip(times, values):
        if not out or out[-1][1] != value:
            out.append([time, value])
    return out


def verify_time_course(
    until: float,
    seed: Optional[int] = None,
    *,
    series: bool = False,
    model: Optional[FactoryModel] = None,
) -> dict:
    """Record every edge's level and every station's state over one run.

    Args:
        until: end time of the run; must be a positive number.
        seed: RNG seed. With the seed of a run_simulation call the copy is
            that run. None draws unseeded.
        series: also return the sampled values as change points, for a chart.
        model: the model to record; defaults to the session model. Only its
            build spec is read.

    Returns:
        {
          "summary": str,
          "until": float, "seed": int | None, "step": float,
          "findings": [{check, severity, component, message, details}, ...],
          "stations": {id: {type, shares: {state: share of the run}, finished}},
          "edges": {id: {type, capacity, mean_level, max_level, share_full,
                         share_empty}},
          "order": [id, ...],          # flow order, sources first, no sinks
          "cycle": float | None,       # cycle of the shift calendar, if any
          "series": {"stations": {id: [[t, state], ...]},
                     "edges": {id: [[t, level], ...]},
                     "finished": {id: [[t, count], ...]}}   # only with series
        }

    Raises:
        ValueError: if `until` is not a positive number, `seed` is not an int
            or None, or the model has no recorded build steps.
    """
    require_positive_number("until", until)
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        raise ValueError(f"seed must be an int or None (got {seed!r}).")
    source = model if model is not None else get_session_model()
    spec = list(source.spec)
    if not spec:
        raise ValueError(
            "The model is empty: create nodes and edges (and connect them) "
            "before recording its time course."
        )

    copy = build_from_spec(spec)
    samples = _record(copy, until, seed)
    findings = _findings(copy, samples, until)
    t = samples["t"]

    stations = {}
    for node_id, states in samples["states"].items():
        node = copy.nodes[node_id]
        stations[node_id] = {
            "type": type(node).__name__,
            "shares": {s: states.count(s) / len(states) for s in STATES if states.count(s)},
            "finished": samples["finished"].get(node_id, [None])[-1],
        }
    edges = {}
    for edge_id, levels in samples["levels"].items():
        edge = copy.edges[edge_id]
        cap = _capacity(edge)
        edges[edge_id] = {
            "type": type(edge).__name__,
            "capacity": cap,
            "mean_level": sum(levels) / len(levels),
            "max_level": max(levels),
            "share_full": sum(v >= cap for v in levels) / len(levels),
            "share_empty": sum(v == 0 for v in levels) / len(levels),
        }

    checks = sorted({f["check"] for f in findings})
    summary = (
        f"{len(findings)} hints ({', '.join(checks)}) over [0, {_t(until)}], one sample every "
        f"{_t(until / SAMPLES)}." if findings else
        f"No hint over [0, {_t(until)}], one sample every {_t(until / SAMPLES)}."
    )
    result = {
        "summary": summary,
        "until": until,
        "seed": seed,
        "step": until / SAMPLES,
        "findings": findings,
        "stations": stations,
        "edges": edges,
        "order": flow_order(copy),
        "cycle": _cycle(copy),
    }
    if series:
        result["series"] = {
            "stations": {k: _changes(t, v) for k, v in samples["states"].items()},
            "edges": {k: _changes(t, v) for k, v in samples["levels"].items()},
            "finished": {k: _changes(t, v) for k, v in samples["finished"].items()},
        }
    return result
