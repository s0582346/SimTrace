"""Post-run item-flow check: did each delivered item follow the wired route?

`verify_item_flow` replays each item's trail through the plant. Trails are built
from the run's events by `telemetry.build_item_paths` and live on
`model.item_paths`. See `architecture/item_flow.md` for what the check means and why.

A trail alternates nodes and edges, so it names not just where the item went but
what carried it:

    ["src", "B1", "M1", "C1", "M2", "B2", "snk"]

That makes the check a direct comparison against the wiring — each edge in the
trail must have the node before it as its `src_node` and the node after it as its
`dest_node` — and a journey is only proper if it runs all the way from a Source to
a Sink.
"""

from __future__ import annotations

from collections import Counter

from simtrace.model import FactoryModel
from simtrace.model import get_model as get_session_model
from simtrace.tools.telemetry import _norm_item

_SOURCE_TYPE = "Source"
_SINK_TYPE = "Sink"
# Event kinds in which a node handles an item.
_ITEM_EVENTS = {"generated", "received", "get", "put", "process_start", "process_end"}


def _endpoints(edge: object) -> tuple[str | None, str | None]:
    """Return an edge's wired `(src_node_id, dest_node_id)`.

    An end that was never connected reads as None, which no node id can equal, so
    an unwired edge appearing in a trail fails the check rather than passing by
    accident.

    Example:
        `connect("B1", "src", "M1")` gives `("src", "M1")`; a buffer that was
        created but never connected gives `(None, None)`.
    """
    src = getattr(edge, "src_node", None)
    dest = getattr(edge, "dest_node", None)
    return (
        getattr(src, "id", None) if src is not None else None,
        getattr(dest, "id", None) if dest is not None else None,
    )


def _wired_adjacency(model: FactoryModel) -> dict[str, dict[str, list[str]]]:
    """Map each node to the nodes downstream of it and the edges that get there.

    Built from the wired edges only; an unconnected edge contributes nothing. This
    is the plant's own answer to "where can a part go from here, and on what?", so
    it both decides whether a node-to-node hop is allowed at all and supplies the
    intended next station when one isn't.

    Example:
        src --B1--> M1 --B2--> M2, plus a second buffer B2b also wired M1 -> M2:

            src  ->  M1 via B1
            M1   ->  M2 via B2, B2b
            M2   ->  (end)
    """
    adjacency: dict[str, dict[str, list[str]]] = {
        node_id: {} for node_id in model.nodes
    }
    for edge_id, edge in model.edges.items():
        src, dest = _endpoints(edge)
        if src is not None and dest is not None:
            adjacency.setdefault(src, {}).setdefault(dest, []).append(edge_id)
    return adjacency


def _bad_hop_reason(
    before: str,
    after: str,
    adjacency: dict[str, dict[str, list[str]]],
) -> str:
    """Explain an impossible node-to-node hop by naming where `before` really goes.

    Points at the intended next station instead of just restating the bad hop, and
    names the edge that would have carried it there.

    Example:
        Against src --B1--> M1 --B2--> M2 --B3--> snk,

            src -> M2  ->  "M2 is not reachable from src; src connects to
                            M1 (via B1)"
            snk -> M1  ->  "M1 is not reachable from snk; snk connects nowhere
                            downstream"
    """
    downstream = adjacency.get(before, {})
    if downstream:
        connects = ", ".join(
            f"{dest} (via {', '.join(sorted(edge_ids))})"
            for dest, edge_ids in sorted(downstream.items())
        )
        return f"{after} is not reachable from {before}; {before} connects to {connects}"
    return f"{after} is not reachable from {before}; {before} connects nowhere downstream"


def _resolve_head(trail: list[str], model: FactoryModel) -> list[str]:
    """Put the Source back in front of a trail that begins with an edge.

    A non-blocking Source (blocking=true) hands its first item over without naming it, so that
    item's trail starts at the edge instead: `["B1", "M1", …]`. The edge knows
    where it comes from, so the Source is read off `B1.src_node` — a lookup with
    one answer, not a guess. Below a merge this still resolves, because the trail
    names *which* edge carried the item.

    A trail that already starts at a node is returned unchanged.

    Example:
        `["B1", "M1", "C1", "M2", "B2", "snk"]` with B1 wired src -> M1 becomes
        `["src", "B1", "M1", "C1", "M2", "B2", "snk"]`.
    """
    if not trail or trail[0] not in model.edges:
        return trail
    src, _ = _endpoints(model.edges[trail[0]])
    return trail if src is None else [src, *trail]


def _first_bad_step(
    trail: list[str],
    model: FactoryModel,
    adjacency: dict[str, dict[str, list[str]]],
) -> tuple[int, str] | None:
    """Find the first place a trail disagrees with the wiring.

    Returns `(index, reason)` for the earliest fault, or None when the trail is a
    route the plant really has. Faults, in the order they are looked for:

      - the trail is empty,
      - alternation: even positions are nodes, odd ones edges (never both —
        `FactoryModel.has_id` keeps one namespace),
      - even length, so it ends on an edge the item never left,
      - starts at a Source, ends at a Sink,
      - each edge is wired between the nodes on either side of it.

    Category order, not trail order: a bad id at position 4 is reported ahead of a
    non-Source at position 0. Each hop is checked against `adjacency` (which must
    come from `model`) before the edge's endpoints. So an endpoint message means the hop is
    wired but the trail's edge id is wrong for it.

    Example:
        Against src --B1--> M1 --B2--> M2 --B3--> snk,

            [src, B1, M1, B2, M2, B3, snk]
                -> None
            [src, B1, M2, B3, snk]
                -> (1, "M2 is not reachable from src; src connects to M1 (via B1)")
    """
    if not trail:
        return (0, "the trail is empty")

    for index, element in enumerate(trail):
        expect_node = index % 2 == 0
        is_node = element in model.nodes
        is_edge = element in model.edges
        if expect_node and not is_node:
            what = "an edge" if is_edge else "unknown"
            return (index, f"expected a node at position {index}, but {element} is {what}")
        if not expect_node and not is_edge:
            what = "a node" if is_node else "unknown"
            return (index, f"expected an edge at position {index}, but {element} is {what}")

    if len(trail) % 2 == 0:
        # Alternation held, so an even length means it ends on an edge: the item is
        # still inside that edge. `verify_item_flow` filters those out before
        # asking, but a direct caller gets a reason rather than an IndexError.
        return (
            len(trail) - 1,
            f"the trail ends inside {trail[-1]}; the item never left it",
        )

    if type(model.nodes[trail[0]]).__name__ != _SOURCE_TYPE:
        return (0, f"the trail starts at {trail[0]}, which is not a Source")
    if type(model.nodes[trail[-1]]).__name__ != _SINK_TYPE:
        # `verify_item_flow` only asks about trails that already end at a sink, so
        # this answers a direct caller rather than the delivered-items loop.
        return (len(trail) - 1, f"the trail ends at {trail[-1]}, which is not a Sink")

    # Every odd position is an edge with a node on each side: one hop.
    for index in range(1, len(trail), 2):
        before, edge_id, after = trail[index - 1], trail[index], trail[index + 1]
        if after not in adjacency.get(before, {}):
            return (index, _bad_hop_reason(before, after, adjacency))
        src, dest = _endpoints(model.edges[edge_id])
        if src != before or dest != after:
            # The hop itself is wired, just not by this edge — only reachable when
            # the trail's edge id disagrees with the nodes around it.
            wiring = (
                f"{src} -> {dest}" if src and dest else "not wired at both ends"
            )
            return (
                index,
                f"{edge_id} carried {before} -> {after}, but {edge_id} is {wiring}",
            )
    return None


def _part_type(model: FactoryModel, item_id: str) -> str | None:
    return model.item_attributes.get(item_id, {}).get("part_type")


def _routes(
    model: FactoryModel, delivered: dict[str, list[str]]
) -> list[dict]:
    """Delivered items grouped by part type and the stations they passed.

    `share` is the row's part of its part type, or of its Source for items
    without a part type.

    Example:
        100 items of type "alu", 95 via src -> M1 -> ship, 5 via src -> M1 ->
        scrap, give two rows with share 0.95 and 0.05.
    """
    counts: Counter = Counter()
    for item_id, trail in delivered.items():
        stations = tuple(e for e in trail if e in model.nodes)
        counts[(_part_type(model, item_id), stations)] += 1
    def group(part_type: str | None, stations: tuple) -> str:
        return part_type if part_type is not None else stations[0]

    per_group: Counter = Counter()
    for (part_type, stations), n in counts.items():
        per_group[group(part_type, stations)] += n
    rows = [
        {
            "part_type": part_type,
            "route": list(stations),
            "end": stations[-1],
            "scrap": bool(getattr(model.nodes[stations[-1]], "scrap", False)),
            "items": n,
            "share": n / per_group[group(part_type, stations)],
        }
        for (part_type, stations), n in counts.items()
    ]
    rows.sort(key=lambda r: (group(r["part_type"], r["route"]), -r["items"], r["route"]))
    return rows


def _repeat_visits(
    model: FactoryModel, delivered: dict[str, list[str]]
) -> list[dict]:
    """Per station, the delivered items that passed it more than once.

    Example:
        3 items went through M1 twice and one three times:
        {"node": "M1", "items": 4, "max_visits": 3}.
    """
    items: Counter = Counter()
    most: dict[str, int] = {}
    for trail in delivered.values():
        for node_id, n in Counter(e for e in trail if e in model.nodes).items():
            if n > 1:
                items[node_id] += 1
                most[node_id] = max(most.get(node_id, 0), n)
    return [
        {"node": node_id, "items": items[node_id], "max_visits": most[node_id]}
        for node_id in model.nodes
        if node_id in items
    ]


def _flow_warnings(
    model: FactoryModel,
    trails: dict[str, list[str]],
    delivered: dict[str, list[str]],
) -> list[dict]:
    """Stations no item touched and part types with no good part at a sink."""
    touched = {
        event.get("node")
        for event in model.events
        if event.get("kind") in _ITEM_EVENTS and event.get("item")
    }
    for trail in trails.values():
        touched.update(trail)
    warnings = [
        {
            "check": "unvisited_station",
            "component": node_id,
            "message": f"No item passed through {node_id} in the run.",
        }
        for node_id in model.nodes
        if node_id not in touched
    ]
    started: Counter = Counter(_part_type(model, i) for i in trails)
    good: Counter = Counter(
        _part_type(model, i)
        for i, trail in delivered.items()
        if not getattr(model.nodes[trail[-1]], "scrap", False)
    )
    for part_type, n in started.items():
        if part_type is not None and good[part_type] == 0:
            warnings.append({
                "check": "type_not_delivered",
                "component": part_type,
                "message": (
                    f"{n} items of part type {part_type} entered the line, "
                    f"none reached a sink that is not a scrap sink."
                ),
            })
    return warnings


def _waiting_places(model: FactoryModel, items: list[str], trails: dict[str, list[str]]) -> Counter:
    """Where each undelivered item is at run end.

    A trail ends at the edge an item was last put into, but a machine may
    already have taken it from there. The item's last event then names that
    machine. A part a Combiner took is wherever its pallet is.

    Example:
        A trail ending in b_mill1 whose last event is "mill1 gets item" is
        counted at mill1, not at b_mill1.
    """
    combiners = {
        node_id for node_id, node in model.nodes.items()
        if type(node).__name__ == "Combiner"
    }
    last: dict[str, tuple[str, str]] = {}
    held: dict[str, list[str]] = {}
    carrier: dict[str, str] = {}
    for event in model.events:
        item = _norm_item(event.get("item"))
        kind, node = event.get("kind"), event.get("node")
        if item is None or kind not in _ITEM_EVENTS | {"edge_put"}:
            continue
        last[item] = (kind, node)
        if kind == "get" and node in combiners:
            held.setdefault(node, []).append(item)
        elif kind == "put" and node in combiners:
            for part in held.pop(node, []):
                if part != item:
                    carrier[part] = item

    def place(item: str, depth: int = 0) -> str:
        kind, node = last.get(item, (None, None))
        if kind == "get" and node in combiners and item in carrier and depth < 10:
            return place(carrier[item], depth + 1)
        if kind in ("get", "process_start", "process_end") and node is not None:
            return node
        trail = trails.get(item)
        return trail[-1] if trail else str(node)

    return Counter(place(item) for item in items)


def verify_item_flow(*, model: FactoryModel | None = None) -> dict:
    """Check that every delivered item travelled a complete wired route, and
    summarize where the items went.

    For each item that reached a sink in the last run, replays its captured
    trail from `model.item_paths` and checks it against the wiring with `_first_bad_step`.
    A clean trail counts toward `passed`; one that disagrees with the wiring is
    reported in `improper` with the position and reason of its first fault.

    Requires a prior `run_simulation`.

    Returns a summary dict:
        {
          "now": <sim clock at end of last run>,
          "delivered": int,     # items that reached a sink
          "passed": int,        # delivered items whose trail is a wired route
          "all_proper": bool,   # passed == delivered
          "improper": [         # only the failures carry detail
            {"item": ..., "trail": [...], "at": int, "reason": str}, ...
          ],
          "routes": [           # delivered items per part type and route
            {"part_type": str | None, "route": [node ids], "end": str,
             "scrap": bool, "items": int, "share": float}, ...
          ],
          "repeat_visits": [{"node": str, "items": int, "max_visits": int}, ...],
          "not_delivered": [{"place": str, "items": int}, ...],  # node or edge holding them
          "warnings": [         # unvisited_station, type_not_delivered
            {"check": str, "component": str, "message": str}, ...
          ],
        }
    """
    model = model if model is not None else get_session_model()

    if model.env.now == 0:
        raise ValueError(
            "No simulation has run yet. "
            "Call run_simulation before verify_item_flow."
        )

    adjacency = _wired_adjacency(model)
    sink_ids = {
        node_id
        for node_id, node in model.nodes.items()
        if type(node).__name__ == _SINK_TYPE
    }

    trails: dict[str, list[str]] = {}
    arrived: dict[str, list[str]] = {}
    passed = 0
    improper: list[dict] = []

    for item_id, raw_trail in model.item_paths.items():
        trail = _resolve_head(list(raw_trail), model)
        if not trail:
            continue
        trails[item_id] = trail

        # An item that never reached a sink is still in flight or discarded —
        # verify_conservation's concern, not a path fault here.
        if trail[-1] not in sink_ids:
            continue
        arrived[item_id] = trail

        fault = _first_bad_step(trail, model, adjacency)
        if fault is None:
            passed += 1
        else:
            index, reason = fault
            improper.append({
                "item": item_id,
                "trail": trail,
                "at": index,
                "reason": reason,
            })

    stuck = _waiting_places(
        model, [item_id for item_id in trails if item_id not in arrived], trails
    )
    return {
        "now": model.env.now,
        "delivered": len(arrived),
        "passed": passed,
        "all_proper": passed == len(arrived),
        "improper": improper,
        "routes": _routes(model, arrived),
        "repeat_visits": _repeat_visits(model, arrived),
        "not_delivered": [
            {"place": place, "items": n} for place, n in stuck.most_common()
        ],
        "warnings": _flow_warnings(model, trails, arrived),
    }
