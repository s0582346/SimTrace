"""Static pre-run check: is the graph wired the way it has to be?

`validate_model` inspects the assembled graph and the recorded build spec and
reports what is wrong with it *before* a run. It is the static counterpart to
the two dynamic checks in this package: `verify_conservation` and
`verify_item_flow` need a finished run to look at, this one needs nothing but
the model. See `architecture/model_validation.md`.

There are two kinds of findings.

An **error** means the run would crash or its numbers would mean nothing.
Examples: a Source with no edge, a branch that leads nowhere.

A **warning** means the run works, but the model may not be what was meant.
Examples:
  - a node that throws items away when its out-edge is full (WIP)
  - a conveyor set up for a different item length than the items on it
  - a splitter with no pallets coming from upstream
  - an unpacking splitter with several out-edges and no pallet_edge
  - an out-edge of a routing machine that no route uses
  - a loop, such as rework, that can get stuck when all its buffers are full

Nothing here advances the clock or mutates the model, so it can be called on a
model that has never run and on one that has.
"""

from __future__ import annotations

import math

from simtrace.model import FactoryModel
from simtrace.model import get_model as get_session_model
from simtrace.tools.builders.part_types import make_payload
from simtrace.tools.builders.routing import FALLBACK, pick_route, target_edge_ids
from simtrace.tools.distributions import spec_parts

_SOURCE_TYPE = "Source"
_SINK_TYPE = "Sink"
_COMBINER_TYPE = "Combiner"
_SPLITTER_TYPE = "Splitter"

# node type -> (min_in, max_in, min_out, max_out); None means "no upper bound".
_CARDINALITY: dict[str, tuple[int, int | None, int, int | None]] = {
    "Source": (0, 0, 1, 1),
    "Sink": (1, None, 0, 0),
    "Machine": (1, None, 1, None),
    "Splitter": (1, None, 1, None),
    "Combiner": (1, None, 1, None),
}

_ERROR = "error"
_WARNING = "warning"


def _finding(
    check: str,
    severity: str,
    component: str | None,
    message: str,
    details: dict | None = None,
) -> dict:
    """One entry in the report: what failed, where, and in plain words.

    `details` holds the numbers behind the message, for checks that compute any.
    """
    finding = {
        "check": check,
        "severity": severity,
        "component": component,
        "message": message,
    }
    if details is not None:
        finding["details"] = details
    return finding


def _edge_list(node: object, attr: str) -> list:
    """Return a node's in_edges/out_edges as a list.

    FactorySimPy leaves both as None until the first `connect`, so an unwired
    node reads as no edges rather than raising.
    """
    return list(getattr(node, attr, None) or [])


def _endpoints(edge: object) -> tuple[str | None, str | None]:
    """Return an edge's wired `(src_node_id, dest_node_id)`; None where unwired."""
    src = getattr(edge, "src_node", None)
    dest = getattr(edge, "dest_node", None)
    return (
        getattr(src, "id", None) if src is not None else None,
        getattr(dest, "id", None) if dest is not None else None,
    )


def _adjacency(model: FactoryModel) -> tuple[dict[str, set], dict[str, set]]:
    """Build the node-to-node graph in both directions from the wired edges.

    Only fully wired edges contribute: a half-connected edge names no pair of
    nodes and so joins nothing. `downstream[n]` is where a part can go from n,
    `upstream[n]` is where one can have come from.
    """
    downstream: dict[str, set] = {node_id: set() for node_id in model.nodes}
    upstream: dict[str, set] = {node_id: set() for node_id in model.nodes}

    for edge in model.edges.values():
        src, dest = _endpoints(edge)
        if src is None or dest is None:
            continue
        downstream.setdefault(src, set()).add(dest)
        upstream.setdefault(dest, set()).add(src)

    return downstream, upstream


def _walk(starts: list[str], adjacency: dict[str, set]) -> set:
    """Every node reachable from `starts` along `adjacency`, the starts included."""
    seen = set(starts)
    queue = list(starts)
    while queue:
        current = queue.pop()
        for neighbour in adjacency.get(current, ()):
            if neighbour not in seen:
                seen.add(neighbour)
                queue.append(neighbour)
    return seen


def _spec_values(model: FactoryModel, op: str, field: str) -> dict[str, object]:
    """Map component id -> one recorded argument, for every build step of `op`.

    Some constructor arguments do not survive onto the live component — a
    ConveyorBelt keeps no `item_length` attribute — so the build spec is the
    only place left to read them.
    """
    values: dict[str, object] = {}
    for step in model.spec:
        if step["op"] != op:
            continue
        kwargs = step["kwargs"]
        component_id = kwargs.get("id")
        if isinstance(component_id, str):
            values[component_id] = kwargs.get(field)
    return values


def _types(model: FactoryModel) -> dict[str, str]:
    """Map every node id to its FactorySimPy class name."""
    return {node_id: type(node).__name__ for node_id, node in model.nodes.items()}


def _pallet_origins(model: FactoryModel, node_types: dict[str, str]) -> set:
    """Nodes that can put a packed item into the flow.

    A pallet enters the line either from a Source declared `flow_item_type=
    "pallet"` or from a Combiner, which packs one. Anything downstream of one of
    these can be handed a packed item; anything downstream of neither cannot.
    """
    pallet_sources = {
        node_id
        for node_id, flow_item_type in _spec_values(
            model, "create_source", "flow_item_type"
        ).items()
        if flow_item_type == "pallet"
    }
    combiners = {
        node_id
        for node_id, node_type in node_types.items()
        if node_type == _COMBINER_TYPE
    }
    return pallet_sources | combiners


def _check_cardinality(model: FactoryModel, node_types: dict[str, str]) -> list[dict]:
    """Every node must have the in/out edge counts its type allows."""
    findings: list[dict] = []

    for node_id, node in model.nodes.items():
        node_type = node_types[node_id]
        rule = _CARDINALITY.get(node_type)
        if rule is None:
            continue
        min_in, max_in, min_out, max_out = rule
        sides = (
            ("in_edge", len(_edge_list(node, "in_edges")), min_in, max_in),
            ("out_edge", len(_edge_list(node, "out_edges")), min_out, max_out),
        )
        for side, actual, minimum, maximum in sides:
            if actual < minimum:
                findings.append(
                    _finding(
                        "node_cardinality",
                        _ERROR,
                        node_id,
                        f"{node_type} '{node_id}' needs at least {minimum} "
                        f"{side}(s) but has {actual}. Wire one with connect.",
                    )
                )
            elif maximum is not None and actual > maximum:
                findings.append(
                    _finding(
                        "node_cardinality",
                        _ERROR,
                        node_id,
                        f"{node_type} '{node_id}' takes at most {maximum} "
                        f"{side}(s) but has {actual}.",
                    )
                )

    return findings


def _check_orphan_edges(model: FactoryModel) -> list[dict]:
    """An edge that was created but never wired to both of its nodes."""
    findings: list[dict] = []

    for edge_id, edge in model.edges.items():
        src, dest = _endpoints(edge)
        if src is not None and dest is not None:
            continue
        ends = []
        if src is None:
            ends.append("src")
        if dest is None:
            ends.append("dest")
        findings.append(
            _finding(
                "orphan_edge",
                _ERROR,
                edge_id,
                f"{type(edge).__name__} '{edge_id}' has no "
                f"{' and no '.join(ends)} node: it was created but never "
                "connected, so nothing travels through it.",
            )
        )

    return findings


def _check_combiner_quantities(
    model: FactoryModel, node_types: dict[str, str]
) -> list[dict]:
    """A Combiner's per-edge pull counts must line up with its in-edges.

    `target_quantity_of_each_item` is fixed when the Combiner is created and the
    in-edges arrive later, at connect time, so the two can only be compared once
    wiring is done — which is what this check is for.
    """
    findings: list[dict] = []
    quantities = _spec_values(model, "create_combiner", "target_quantity_of_each_item")

    for node_id, node in model.nodes.items():
        if node_types[node_id] != _COMBINER_TYPE:
            continue
        targets = quantities.get(node_id)
        if targets is None:
            # Left at its default, which the builder fills in as [1].
            targets = [1]
        in_edges = len(_edge_list(node, "in_edges"))
        if in_edges and len(targets) != in_edges:
            findings.append(
                _finding(
                    "combiner_quantities",
                    _ERROR,
                    node_id,
                    f"Combiner '{node_id}' has {in_edges} in_edge(s) but "
                    f"target_quantity_of_each_item lists {len(targets)} "
                    f"({targets}). One count per in_edge is required, the first "
                    "being the pallet edge.",
                )
            )

    return findings


def _check_reachability(
    model: FactoryModel,
    node_types: dict[str, str],
    downstream: dict[str, set],
    upstream: dict[str, set],
) -> list[dict]:
    """Every node must sit on some route that runs from a Source to a Sink."""
    findings: list[dict] = []

    sources = sorted(n for n, t in node_types.items() if t == _SOURCE_TYPE)
    sinks = {n for n, t in node_types.items() if t == _SINK_TYPE}

    if not sources:
        findings.append(
            _finding(
                "reachability",
                _ERROR,
                None,
                "The model has no Source, so no items are ever generated.",
            )
        )
    if not sinks:
        findings.append(
            _finding(
                "reachability",
                _ERROR,
                None,
                "The model has no Sink, so no item can ever leave the line.",
            )
        )
    if not sources or not sinks:
        return findings

    for source_id in sources:
        if not _walk([source_id], downstream) & sinks:
            findings.append(
                _finding(
                    "reachability",
                    _ERROR,
                    source_id,
                    f"Source '{source_id}' has no route to any Sink: everything "
                    "it generates piles up or is dropped.",
                )
            )

    from_sources = _walk(sources, downstream)
    to_sinks = _walk(sorted(sinks), upstream)

    for sink_id in sorted(sinks):
        if sink_id not in from_sources:
            findings.append(
                _finding(
                    "reachability",
                    _ERROR,
                    sink_id,
                    f"Sink '{sink_id}' is not reachable from any Source: nothing "
                    "ever arrives there.",
                )
            )

    on_a_route = from_sources & to_sinks
    for node_id in model.nodes:
        if node_id in sinks or node_types[node_id] == _SOURCE_TYPE:
            continue
        if node_id in on_a_route:
            continue
        findings.append(
            _finding(
                "reachability",
                _ERROR,
                node_id,
                f"{node_types[node_id]} '{node_id}' is on no route from a Source "
                "to a Sink, so it takes no part in the run.",
            )
        )

    return findings


def _check_silent_discard(model: FactoryModel) -> list[dict]:
    """A non-blocking node drops items whenever its out-edge is full.

    Every edge has a finite capacity, so this is not a question of if but of how
    often. The run still finishes and still reports a throughput; the dropped
    items surface only afterwards, as `discarded` in `verify_conservation`.
    `create_source` defaults to `blocking=False`, which is where this usually
    comes from.
    """
    findings: list[dict] = []

    for node_id, node in model.nodes.items():
        if getattr(node, "blocking", None) is not False:
            continue
        out_edges = _edge_list(node, "out_edges")
        if not out_edges:
            continue
        described = ", ".join(
            f"'{getattr(edge, 'id', '?')}' (capacity "
            f"{getattr(edge, 'capacity', '?')})"
            for edge in out_edges
        )
        findings.append(
            _finding(
                "silent_discard",
                _WARNING,
                node_id,
                f"{type(node).__name__} '{node_id}' is non-blocking: while "
                f"{described} is full its items are dropped and counted as "
                "discarded, not delivered.",
            )
        )

    return findings


def _check_conveyor_item_length(
    model: FactoryModel, node_types: dict[str, str], upstream: dict[str, set]
) -> list[dict]:
    """A conveyor's item length must match the items that reach it.

    Travel time and belt capacity are both derived from `item_length`, so a belt
    set up for a different item than the one arriving on it reports a throughput
    for a line that does not exist. This is the dimensional check of the model:
    the Source's metres and the belt's metres have to be the same metres.
    """
    findings: list[dict] = []
    belt_lengths = _spec_values(model, "create_conveyor", "item_length")
    source_lengths = _spec_values(model, "create_source", "item_length")

    for edge_id, edge in model.edges.items():
        belt_length = belt_lengths.get(edge_id)
        if belt_length is None:
            continue
        src, _ = _endpoints(edge)
        if src is None:
            continue
        for source_id in sorted(_walk([src], upstream)):
            if node_types.get(source_id) != _SOURCE_TYPE:
                continue
            source_length = source_lengths.get(source_id)
            if source_length is None or source_length == belt_length:
                continue
            findings.append(
                _finding(
                    "conveyor_item_length",
                    _WARNING,
                    edge_id,
                    f"Conveyor '{edge_id}' is set up for item_length "
                    f"{belt_length} but Source '{source_id}' upstream of it "
                    f"emits items of length {source_length}. Travel time and "
                    "belt capacity are computed from the belt's figure.",
                )
            )

    return findings


def _check_pallets(
    model: FactoryModel, node_types: dict[str, str], upstream: dict[str, set]
) -> list[dict]:
    """Nodes that handle packed items need something upstream that packs them.

    A Splitter takes a packed item apart, and a Combiner packs into a container
    it pulls from its first in-edge. Both need a pallet to have entered the flow
    somewhere above them — from a `flow_item_type="pallet"` Source or from
    another Combiner.

    For a Splitter that is not the whole story: a Source emits an *empty*
    pallet, so only a Combiner upstream gives it anything to unpack.
    """
    findings: list[dict] = []
    origins = _pallet_origins(model, node_types)
    splitter_modes = _spec_values(model, "create_splitter", "mode")

    for node_id, node in model.nodes.items():
        node_type = node_types[node_id]
        in_edges = _edge_list(node, "in_edges")
        if not in_edges:
            # No in-edge at all is a cardinality error; nothing to trace back.
            continue

        if node_type == _SPLITTER_TYPE:
            if splitter_modes.get(node_id, "UNPACK") != "UNPACK":
                # FactorySimPy's splitter always unpacks: it reads the incoming
                # item's contents, which only a Pallet has. A SPLIT splitter
                # raises AttributeError on the first item it is handed.
                findings.append(
                    _finding(
                        "splitter_mode",
                        _ERROR,
                        node_id,
                        f"Splitter '{node_id}' is in SPLIT mode, which "
                        "FactorySimPy does not implement — the run raises on "
                        "the first item. Use mode='UNPACK' and feed it pallets "
                        "packed by a Combiner.",
                    )
                )
                continue

            reachable = _walk(sorted(upstream.get(node_id, set())), upstream)
            if not reachable & origins:
                findings.append(
                    _finding(
                        "pallet_mismatch",
                        _WARNING,
                        node_id,
                        f"Splitter '{node_id}' unpacks packed items but nothing "
                        "upstream of it packs any. Feed it from a Combiner.",
                    )
                )
            elif not reachable & {
                other_id
                for other_id, other_type in node_types.items()
                if other_type == _COMBINER_TYPE
            }:
                # A Source emits an empty Pallet; only a Combiner fills one.
                findings.append(
                    _finding(
                        "pallet_mismatch",
                        _WARNING,
                        node_id,
                        f"Splitter '{node_id}' is fed pallets straight from a "
                        "pallet Source, and those are empty. It will emit one "
                        "item per pallet — the container — and nothing else. "
                        "Pack the pallets with a Combiner first.",
                    )
                )

        elif node_type == _COMBINER_TYPE:
            pallet_edge = in_edges[0]
            pallet_src, _ = _endpoints(pallet_edge)
            if pallet_src is None:
                continue
            if _walk([pallet_src], upstream) & origins:
                continue
            findings.append(
                _finding(
                    "pallet_mismatch",
                    _WARNING,
                    node_id,
                    f"Combiner '{node_id}' pulls its container from "
                    f"'{getattr(pallet_edge, 'id', '?')}', its first in_edge, "
                    "but no pallet Source feeds that edge. Connect the pallet "
                    "edge first.",
                )
            )

    return findings


def _check_pallet_exit(model: FactoryModel, node_types: dict[str, str]) -> list[dict]:
    """Where an unpacking Splitter sends the empty pallet.

    Without `pallet_edge` the empty pallet takes the first out-edge with
    space, like the parts. With two or more out-edges that is a warning. A
    `pallet_edge` that is not one of the splitter's out-edges is an error:
    the run raises.

    Example:
        unpack -> b_parts -> ship, unpack -> b_pallets -> returns, no
        pallet_edge: warning, the pallets go to ship through b_parts.
    """
    findings: list[dict] = []
    modes = _spec_values(model, "create_splitter", "mode")
    pallet_edges = _spec_values(model, "create_splitter", "pallet_edge")
    for node_id, node in model.nodes.items():
        if node_types[node_id] != _SPLITTER_TYPE or modes.get(node_id, "UNPACK") != "UNPACK":
            continue
        out_ids = [getattr(edge, "id", "?") for edge in _edge_list(node, "out_edges")]
        pallet_edge = pallet_edges.get(node_id)
        if pallet_edge is None:
            if len(out_ids) >= 2:
                findings.append(
                    _finding(
                        "pallet_exit",
                        _WARNING,
                        node_id,
                        f"Splitter '{node_id}' has {len(out_ids)} out-edges "
                        f"({', '.join(out_ids)}) but no pallet_edge. The empty "
                        f"pallet goes to the first one with space, mostly "
                        f"'{out_ids[0]}', together with the parts. Set "
                        "pallet_edge to the edge the empty pallet should leave by.",
                    )
                )
        elif pallet_edge not in out_ids:
            findings.append(
                _finding(
                    "pallet_exit",
                    _ERROR,
                    node_id,
                    f"Splitter '{node_id}' has pallet_edge '{pallet_edge}', "
                    "which is not one of its out-edges"
                    + (f" ({', '.join(out_ids)})" if out_ids else "")
                    + ". The run raises.",
                )
            )
        elif len(out_ids) < 2:
            findings.append(
                _finding(
                    "pallet_exit",
                    _ERROR,
                    node_id,
                    f"Splitter '{node_id}' sends the empty pallet to "
                    f"'{pallet_edge}', its only out-edge, so the parts have "
                    "none left. The run raises. Connect a second out-edge for "
                    "the parts.",
                )
            )
    return findings


def _routed_arrivals(
    model: FactoryModel, node_types: dict[str, str]
) -> dict[str, list[tuple[str, dict]]]:
    """For each routing machine, list the Sources whose parts can reach it.

    Each entry is the Source id plus what its parts carry (type and
    attributes). To find them, we follow each Source's parts through the
    model, edge by edge. At a routing machine, the parts only go on along the
    edge the machine would send them to. So a second routing machine sees only
    the parts the first one sends its way. Parts that a Combiner packs into a
    pallet are not followed any further.
    """
    arrivals: dict[str, list[tuple[str, dict]]] = {}

    for source_id, node_type in node_types.items():
        if node_type != _SOURCE_TYPE:
            continue
        payload = _payload(model.nodes[source_id])
        # Note at each routing machine that this Source's items arrive there.
        for node_id in _follow(model, node_types, source_id):
            if getattr(model.nodes[node_id], "routes", None) is not None:
                arrivals.setdefault(node_id, []).append((source_id, payload))

    return arrivals


def _payload(source: object) -> dict:
    """What every item of a Source carries; {} for untyped items."""
    if getattr(source, "part_type", None) is None:
        return {}
    return make_payload(source)


def _next_edges(node: object, payload: dict) -> list:
    """The out-edges a part with `payload` can leave `node` by.

    A routing machine keeps only the edges its rule picks for the part, none
    when nothing matches. A share machine keeps every edge with a share above 0.
    """
    out_edges = _edge_list(node, "out_edges")
    routes = getattr(node, "routes", None)
    if routes is not None:
        target = pick_route(routes, payload.get(node.route_by))
        edge_ids = target_edge_ids(target)
        out_edges = [e for e in out_edges if getattr(e, "id", None) in edge_ids]
    shares = getattr(node, "route_shares", None)
    if shares is not None:
        out_edges = [e for e in out_edges if shares.get(getattr(e, "id", None), 0) > 0]
    return out_edges


def _follow(model: FactoryModel, node_types: dict[str, str], source_id: str) -> set:
    """Every node a part of `source_id` can reach, the Source included.

    Parts that a Combiner packs into a pallet are not followed any further.
    """
    payload = _payload(model.nodes[source_id])
    seen = {source_id}
    queue = [source_id]
    while queue:
        node = model.nodes[queue.pop()]
        for edge in _next_edges(node, payload):
            # Skip half-wired edges and nodes already visited.
            _, dest_id = _endpoints(edge)
            if dest_id is None or dest_id in seen:
                continue
            # A Combiner packs items from its other in-edges into the pallet
            # from its first one; packed items are not followed.
            dest = model.nodes[dest_id]
            if (
                node_types.get(dest_id) == _COMBINER_TYPE
                and _edge_list(dest, "in_edges")[:1] != [edge]
            ):
                continue
            seen.add(dest_id)
            queue.append(dest_id)
    return seen


def _check_routes(model: FactoryModel, node_types: dict[str, str]) -> list[dict]:
    """Check that each routing machine's rule fits its edges and its parts.

    Three things are checked. Every edge in `routes`, also inside a list,
    must leave this machine. Every part that can arrive must have a route, or
    there must be a "*" fallback. If either is wrong, the run stops at the
    first part it affects. And every edge that leaves the machine should
    appear in `routes`, otherwise no part ever uses it.
    """
    findings: list[dict] = []
    arrivals = _routed_arrivals(model, node_types)

    for node_id, node in model.nodes.items():
        # Only machines with a routing rule are checked.
        routes = getattr(node, "routes", None)
        if routes is None:
            continue
        route_by = node.route_by
        out_ids = [getattr(e, "id", None) for e in _edge_list(node, "out_edges")]

        # 1. Every edge in the rule must leave this machine.
        named: set[str] = set()
        for value, target in routes.items():
            for edge_id in target_edge_ids(target):
                named.add(edge_id)
                if edge_id in out_ids:
                    continue
                findings.append(
                    _finding(
                        "route_target",
                        _ERROR,
                        node_id,
                        f"Machine '{node_id}' sends {route_by}={value!r} to "
                        f"'{edge_id}', which is not one of its out-edges ("
                        + (", ".join(f"'{e}'" for e in out_ids) or "none")
                        + "). The run stops at the first item sent there.",
                    )
                )

        # 2. Every edge that leaves this machine should be in the rule.
        for edge_id in out_ids:
            if edge_id in named:
                continue
            findings.append(
                _finding(
                    "route_unused_edge",
                    _WARNING,
                    node_id,
                    f"Machine '{node_id}' has out-edge '{edge_id}', but no entry "
                    "in its routes sends items there, so nothing travels "
                    "through it.",
                )
            )

        # 3. Every Source whose parts arrive here needs a route or "*".
        for source_id, payload in arrivals.get(node_id, []):
            value = payload.get(route_by)
            if pick_route(routes, value) is not None:
                continue
            # Say why the parts don't match, for the message.
            if not payload:
                described = "carry no part type"
            elif route_by not in payload:
                described = f"have no attribute '{route_by}'"
            elif not isinstance(value, str):
                described = (
                    f"have {route_by}={value!r}, and only string values match "
                    "a route"
                )
            else:
                described = f"have {route_by}={value!r}"
            findings.append(
                _finding(
                    "route_unmatched",
                    _ERROR,
                    node_id,
                    f"Machine '{node_id}' routes by '{route_by}', but items "
                    f"from Source '{source_id}' {described}. Its routes have no "
                    f'entry for that and no "{FALLBACK}" fallback, so the run '
                    "stops at the first such item.",
                )
            )

    return findings


def _check_shares(model: FactoryModel) -> list[dict]:
    """Check that each share machine's rule fits its edges.

    Every edge in `route_shares` must leave this machine, otherwise the run
    stops at the first part sent there. And every edge that leaves the
    machine should have a share above 0, otherwise no part ever uses it.
    """
    findings: list[dict] = []

    for node_id, node in model.nodes.items():
        # Only machines with a share rule are checked.
        shares = getattr(node, "route_shares", None)
        if shares is None:
            continue
        out_ids = [getattr(e, "id", None) for e in _edge_list(node, "out_edges")]

        # 1. Every edge in the rule must leave this machine.
        for edge_id, share in shares.items():
            if edge_id in out_ids:
                continue
            findings.append(
                _finding(
                    "route_target",
                    _ERROR,
                    node_id,
                    f"Machine '{node_id}' sends a share of {share} to "
                    f"'{edge_id}', which is not one of its out-edges ("
                    + (", ".join(f"'{e}'" for e in out_ids) or "none")
                    + "). The run stops at the first item sent there.",
                )
            )

        # 2. Every edge that leaves this machine should get a share above 0.
        for edge_id in out_ids:
            if shares.get(edge_id, 0) > 0:
                continue
            why = "has a share of 0" if edge_id in shares else "is not in route_shares"
            findings.append(
                _finding(
                    "route_unused_edge",
                    _WARNING,
                    node_id,
                    f"Machine '{node_id}' has out-edge '{edge_id}', but it "
                    f"{why}, so nothing travels through it.",
                )
            )

    return findings


def _check_loops(model: FactoryModel, downstream: dict[str, set]) -> list[dict]:
    """Warn about loops in the graph, such as a rework loop.

    In a loop each station waits for space in the buffer of the next one. When
    every buffer in the loop is full at the same time, no station can hand on
    its part and the loop stops for good. Larger buffers in the loop make this
    less likely; they cannot rule it out.
    """
    findings: list[dict] = []

    # A node is in a loop when it can reach itself again.
    in_loop = {
        node_id
        for node_id in model.nodes
        if node_id in _walk(list(downstream.get(node_id, ())), downstream)
    }

    # Nodes that can reach each other belong to the same loop; report each
    # loop once.
    reported: set = set()
    for node_id in sorted(in_loop):
        if node_id in reported:
            continue
        reach = _walk([node_id], downstream)
        members = sorted(
            other for other in in_loop
            if other in reach and node_id in _walk([other], downstream)
        )
        reported.update(members)
        findings.append(
            _finding(
                "loop_jam",
                _WARNING,
                members[0],
                "Stations "
                + ", ".join(f"'{m}'" for m in members)
                + " form a loop. When every buffer in the loop is full, each "
                "station waits for the next one and the loop stops for good. "
                "Give the buffers in the loop enough capacity.",
            )
        )

    return findings


_MACHINE_TYPE = "Machine"
_BUFFER_TYPES = ("Buffer", "Fleet")
_NORMAL_NAMES = ("normal", "gauss")
# A normal draw below 0 is clamped to 0. Above this chance it shifts the mean.
_NEGATIVE_SHARE = 0.01
_LOW_AVAILABILITY = 0.5


def _mean(value: object) -> float | None:
    """Mean of a constant or a distribution string; None if it is neither."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    parts = spec_parts(value)
    if parts is None:
        return None
    name, a, b = parts
    if name == "uniform":
        return (a + b) / 2
    return a


def _working_share(calendar: object | None) -> float:
    """Share of each cycle inside the calendar's windows; 1 without a calendar."""
    if calendar is None:
        return 1.0
    return calendar.working_time_between(0, calendar.cycle) / calendar.cycle


def _availability(model: FactoryModel, node_id: str) -> float:
    """Mean time to failure / (that + mean repair time); 1 without breakdowns."""
    to_failure = _mean(_spec_values(model, "create_machine", "time_to_failure").get(node_id))
    repair = _mean(_spec_values(model, "create_machine", "repair_time").get(node_id))
    if not to_failure or repair is None:
        return 1.0
    return to_failure / (to_failure + repair)


def _finish_rate(model: FactoryModel, node_id: str) -> float:
    """Parts a machine can finish per time unit, counting its workers, its
    breakdowns and its working hours. Infinite for a machine that takes no time."""
    delay = _mean(_spec_values(model, "create_machine", "processing_delay").get(node_id))
    if not delay:
        return math.inf
    workers = _spec_values(model, "create_machine", "work_capacity").get(node_id) or 1
    node = model.nodes[node_id]
    return (
        workers / delay
        * _availability(model, node_id)
        * _working_share(getattr(node, "calendar", None))
    )


def _flows(model: FactoryModel, node_types: dict[str, str]) -> tuple[dict, dict]:
    """Mean parts per time unit into every node and through every edge.

    Each Source's parts are carried down the line separately, so a routing
    machine can send them by their own attributes. A share machine splits by
    its shares; any other node splits evenly over its out-edges. A machine
    passes on at most what it can finish. Splitters and Combiners change the
    part count, so nothing is carried past them, and neither into a loop.

    Returns (node id -> rate in, edge id -> rate through).
    """
    inflow: dict[str, dict[str, float]] = {node_id: {} for node_id in model.nodes}
    through: dict[str, float] = {}

    # Visit the nodes in flow order; nodes in a loop are never ready.
    waiting = {
        node_id: len({_endpoints(e)[0] for e in _edge_list(node, "in_edges")} - {None})
        for node_id, node in model.nodes.items()
    }
    ready = [node_id for node_id, count in waiting.items() if count == 0]
    while ready:
        node_id = ready.pop()
        node = model.nodes[node_id]
        node_type = node_types.get(node_id)

        if node_type == _SOURCE_TYPE:
            gap = _mean(_spec_values(model, "create_source", "inter_arrival_time").get(node_id))
            share = _working_share(getattr(node, "calendar", None))
            out = {node_id: share / gap} if gap else {}
        elif node_type == _MACHINE_TYPE:
            total = sum(inflow[node_id].values())
            limit = _finish_rate(model, node_id)
            factor = min(1.0, limit / total) if total else 1.0
            out = {source: rate * factor for source, rate in inflow[node_id].items()}
        else:
            out = {}

        shares = getattr(node, "route_shares", None)
        for source_id, rate in out.items():
            edges = _next_edges(node, _payload(model.nodes[source_id]))
            for edge in edges:
                edge_id = getattr(edge, "id", None)
                part = rate * shares[edge_id] if shares is not None else rate / len(edges)
                through[edge_id] = through.get(edge_id, 0.0) + part
                _, dest_id = _endpoints(edge)
                if dest_id is not None:
                    inflow[dest_id][source_id] = inflow[dest_id].get(source_id, 0.0) + part

        for dest_id in {_endpoints(e)[1] for e in _edge_list(node, "out_edges")} - {None}:
            waiting[dest_id] -= 1
            if waiting[dest_id] == 0:
                ready.append(dest_id)

    return {node_id: sum(rates.values()) for node_id, rates in inflow.items()}, through


def _every(rate: float) -> str:
    """A rate as 'one part every N time units'."""
    return f"{1 / rate:.3g}"


def _check_overload(
    model: FactoryModel, node_types: dict[str, str], inflow: dict[str, float]
) -> list[dict]:
    """Warn when a machine gets more parts on average than it can finish.

    The estimate uses mean times only. Effective process time is the process
    time divided by the number of workers, the availability and the working
    share. Utilization is effective process time / interarrival time. At 100%
    or more parts pile up in front of the machine and the stations before it
    are held back, or a non-blocking node throws parts away.
    """
    findings: list[dict] = []
    for node_id, node_type in node_types.items():
        if node_type != _MACHINE_TYPE or not inflow.get(node_id):
            continue
        limit = _finish_rate(model, node_id)
        load = inflow[node_id] / limit
        if load < 1:
            continue
        findings.append(
            _finding(
                "station_overload",
                _WARNING,
                node_id,
                f"Machine '{node_id}' gets more parts than it can finish "
                f"(utilization {load:.0%}).",
                {
                    "effective_process_time": 1 / limit,
                    "interarrival_time": 1 / inflow[node_id],
                    "utilization": load,
                },
            )
        )
    return findings


def _check_buffer_delay(model: FactoryModel, through: dict[str, float]) -> list[dict]:
    """Warn when a buffer with a delay has too few places for its flow.

    Each part stays for the delay, so on average flow × delay places are taken.
    The flow is taken over the working hours of the node in front of it.
    """
    findings: list[dict] = []
    for edge_id, edge in model.edges.items():
        if type(edge).__name__ not in _BUFFER_TYPES or not through.get(edge_id):
            continue
        op = "create_buffer" if type(edge).__name__ == "Buffer" else "create_fleet"
        delay = _mean(_spec_values(model, op, "delay").get(edge_id))
        if not delay:
            continue
        src_id, _ = _endpoints(edge)
        share = _working_share(getattr(model.nodes.get(src_id), "calendar", None))
        needed = through[edge_id] / share * delay
        capacity = getattr(edge, "capacity", None)
        if capacity is None or needed < capacity:
            continue
        findings.append(
            _finding(
                "buffer_delay_capacity",
                _WARNING,
                edge_id,
                f"'{edge_id}' keeps each part about {delay:g} time units and gets "
                f"one about every {_every(through[edge_id] / share)}, so it needs "
                f"about {needed:.1f} places on average but has {capacity}. It "
                f"fills up and holds back '{src_id}'.",
            )
        )
    return findings


def _windows_in(calendar: object, period: float) -> list[tuple[float, float]]:
    """The calendar's windows repeated over [0, period)."""
    repeats = round(period / calendar.cycle)
    return [
        (start + k * calendar.cycle, end + k * calendar.cycle)
        for k in range(repeats)
        for start, end in calendar.windows
    ]


def _working(windows: list[tuple[float, float]] | None, moment: float) -> bool:
    return windows is None or any(start <= moment < end for start, end in windows)


def _time_off_while_on(on: object | None, off: object) -> float:
    """Time per common period in which `on` works and `off` does not.

    `on` is None for no calendar, so it always works. Returns 0 when the two
    cycles have no common period of whole numbers.
    """
    cycles = [off.cycle] + ([on.cycle] if on is not None else [])
    if not all(float(c).is_integer() for c in cycles):
        return 0.0
    period = math.lcm(*(int(c) for c in cycles))
    on_windows = _windows_in(on, period) if on is not None else None
    off_windows = _windows_in(off, period)
    points = sorted(
        {0.0, float(period)}
        | {p for window in (on_windows or []) + off_windows for p in window}
    )
    return sum(
        end - start
        for start, end in zip(points, points[1:])
        if _working(on_windows, (start + end) / 2)
        and not _working(off_windows, (start + end) / 2)
    )


def _check_calendar_mismatch(model: FactoryModel, node_types: dict[str, str]) -> list[dict]:
    """Warn when a Source delivers while the machine right after it is off.

    The buffer between them fills up. Then a blocking Source waits and a
    non-blocking one throws its parts away.
    """
    findings: list[dict] = []
    for source_id, node_type in node_types.items():
        if node_type != _SOURCE_TYPE:
            continue
        source = model.nodes[source_id]
        for edge in _edge_list(source, "out_edges"):
            _, dest_id = _endpoints(edge)
            dest = model.nodes.get(dest_id)
            station_calendar = getattr(dest, "calendar", None)
            if station_calendar is None:
                continue
            gap = _time_off_while_on(getattr(source, "calendar", None), station_calendar)
            if gap <= 0:
                continue
            after = (
                f"'{source_id}' waits"
                if getattr(source, "blocking", None) is not False
                else f"'{source_id}' throws its parts away"
            )
            findings.append(
                _finding(
                    "calendar_mismatch",
                    _WARNING,
                    source_id,
                    f"Source '{source_id}' delivers for about {gap:g} time units "
                    f"per cycle while '{dest_id}' is off shift. "
                    f"'{getattr(edge, 'id', '?')}' fills up and then {after}.",
                )
            )
    return findings


def _check_unused_calendars(model: FactoryModel) -> list[dict]:
    """Warn about a calendar that no Source or machine follows."""
    used = {id(getattr(node, "calendar", None)) for node in model.nodes.values()}
    return [
        _finding(
            "unused_calendar",
            _WARNING,
            calendar_id,
            f"Calendar '{calendar_id}' is not used by any Source or machine.",
        )
        for calendar_id, calendar in model.calendars.items()
        if id(calendar) not in used
    ]


def _check_availability(model: FactoryModel) -> list[dict]:
    """Warn when a machine is broken down more often than it works."""
    findings: list[dict] = []
    for node_id in _spec_values(model, "create_machine", "time_to_failure"):
        availability = _availability(model, node_id)
        if availability >= _LOW_AVAILABILITY:
            continue
        findings.append(
            _finding(
                "low_availability",
                _WARNING,
                node_id,
                f"Machine '{node_id}' is in repair {1 - availability:.0%} of its "
                "working time: the mean repair time is longer than the mean "
                "time to failure. Check whether the two were swapped or are "
                "in different units.",
            )
        )
    return findings


def _check_normal_clamping(model: FactoryModel) -> list[dict]:
    """Warn about a normal distribution that often draws below 0.

    Such draws are set to 0, which raises the real mean above the given one.
    """
    findings: list[dict] = []
    for step in model.spec:
        component_id = step["kwargs"].get("id")
        for field, value in step["kwargs"].items():
            parts = spec_parts(value)
            if parts is None or parts[0] not in _NORMAL_NAMES or not parts[2]:
                continue
            _, mean, deviation = parts
            negative = 0.5 * (1 + math.erf(-mean / (deviation * math.sqrt(2))))
            if negative <= _NEGATIVE_SHARE:
                continue
            findings.append(
                _finding(
                    "normal_clamping",
                    _WARNING,
                    component_id,
                    f"'{component_id}' has {field}={value!r}: about "
                    f"{negative:.0%} of the draws are below 0 and are set to 0, "
                    "so the real mean is higher than the given one.",
                )
            )
    return findings


def _check_no_effect(model: FactoryModel) -> list[dict]:
    """Warn about an edge selection that has nothing to choose from.

    ROUND_ROBIN or RANDOM over a single edge acts like no selection at all.
    This usually means an edge is missing.
    """
    findings: list[dict] = []
    for op in ("create_source", "create_machine", "create_splitter", "create_combiner"):
        for attr, side in (("out_edge_selection", "out"), ("in_edge_selection", "in")):
            for node_id, selection in _spec_values(model, op, attr).items():
                if selection in (None, "FIRST_AVAILABLE") or node_id not in model.nodes:
                    continue
                edges = _edge_list(model.nodes[node_id], f"{side}_edges")
                if len(edges) > 1:
                    continue
                findings.append(
                    _finding(
                        "no_effect_setting",
                        _WARNING,
                        node_id,
                        f"'{node_id}' has {attr}={selection!r} but only "
                        f"{len(edges)} {side}-edge, so there is nothing to "
                        "choose between. Is an edge missing?",
                    )
                )
    return findings


def _check_scrap_only(model: FactoryModel, node_types: dict[str, str]) -> list[dict]:
    """Warn when every Sink a Source's parts can reach is a scrap Sink."""
    findings: list[dict] = []
    for source_id, node_type in node_types.items():
        if node_type != _SOURCE_TYPE:
            continue
        sinks = [
            node_id
            for node_id in _follow(model, node_types, source_id)
            if node_types.get(node_id) == _SINK_TYPE
        ]
        if not sinks or not all(getattr(model.nodes[s], "scrap", False) for s in sinks):
            continue
        part_type = getattr(model.nodes[source_id], "part_type", None)
        parts = f"'{part_type}' parts" if part_type else "Parts"
        findings.append(
            _finding(
                "part_type_dead_end",
                _WARNING,
                source_id,
                f"{parts} from Source '{source_id}' can only reach the scrap "
                "Sink" + ("s " if len(sinks) > 1 else " ")
                + ", ".join(f"'{s}'" for s in sorted(sinks))
                + ", so none of them is ever completed.",
            )
        )
    return findings


def validate_model(*, model: FactoryModel | None = None) -> dict:
    """Check the assembled graph before it is run.

    It reads the wired graph and the recorded build spec, so it is equally valid 
    on a model that has never run and on one that has. Errors and warnings are 
    separated because they call for different responses.

    Errors:
        node_cardinality    a node has too few or too many in/out edges
        orphan_edge         an edge was created but never connected
        combiner_quantities target_quantity_of_each_item doesn't match in_edges
        reachability        a node is on no route from a Source to a Sink
        splitter_mode       a splitter is in SPLIT mode, which cannot run
        route_target        a routing machine names an edge it is not wired to,
                            in its routes or its route_shares
        route_unmatched     items reach a routing machine that no route matches

    Warnings:
        silent_discard        a non-blocking node drops items at a full out-edge
        conveyor_item_length  a belt's item length disagrees with its Source's
        pallet_mismatch       packed items are handled with none produced above,
                              or a splitter is fed empty pallets from a Source
        route_unused_edge     a routing machine's out-edge that no route names,
                              or that has a share of 0
        loop_jam              stations form a loop that stops when it is full
        station_overload      a machine gets more parts on average than it can
                              finish, from the mean times. Its details give
                              effective_process_time, interarrival_time and
                              utilization.
        buffer_delay_capacity a buffer with a delay has fewer places than its
                              flow × delay
        calendar_mismatch     a Source delivers while the machine after it is
                              off shift
        unused_calendar       a calendar no Source or machine follows
        low_availability      a machine is in repair more than half its time
        normal_clamping       a normal distribution draws below 0 often
        no_effect_setting     ROUND_ROBIN or RANDOM over a single edge
        part_type_dead_end    a Source's parts can only reach scrap Sinks

    Returns a report dict:
        {
          "valid": bool,          # False if there is at least one error
          "errors": [finding],    # each {check, severity, component, message}
          "warnings": [finding],
          "checked": {"nodes": int, "edges": int},
        }
    """
    model = model if model is not None else get_session_model()

    if not model.nodes and not model.edges:
        return {
            "valid": False,
            "errors": [
                _finding(
                    "empty_model",
                    _ERROR,
                    None,
                    "The model is empty. Build it with the create_* tools and "
                    "wire it with connect first.",
                )
            ],
            "warnings": [],
            "checked": {"nodes": 0, "edges": 0},
        }

    node_types = _types(model)
    downstream, upstream = _adjacency(model)

    findings: list[dict] = []
    findings += _check_cardinality(model, node_types)
    findings += _check_orphan_edges(model)
    findings += _check_combiner_quantities(model, node_types)
    findings += _check_reachability(model, node_types, downstream, upstream)
    findings += _check_silent_discard(model)
    findings += _check_conveyor_item_length(model, node_types, upstream)
    findings += _check_pallets(model, node_types, upstream)
    findings += _check_pallet_exit(model, node_types)
    findings += _check_routes(model, node_types)
    findings += _check_shares(model)
    findings += _check_loops(model, downstream)
    inflow, through = _flows(model, node_types)
    findings += _check_overload(model, node_types, inflow)
    findings += _check_buffer_delay(model, through)
    findings += _check_calendar_mismatch(model, node_types)
    findings += _check_unused_calendars(model)
    findings += _check_availability(model)
    findings += _check_normal_clamping(model)
    findings += _check_no_effect(model)
    findings += _check_scrap_only(model, node_types)

    errors = [f for f in findings if f["severity"] == _ERROR]
    warnings = [f for f in findings if f["severity"] == _WARNING]

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "checked": {"nodes": len(model.nodes), "edges": len(model.edges)},
    }
