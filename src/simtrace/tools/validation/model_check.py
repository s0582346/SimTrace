"""Static pre-run check: is the graph wired the way it has to be?

`validate_model` inspects the assembled graph and the recorded build spec and
reports what is wrong with it *before* a run. It is the static counterpart to
the two dynamic checks in this package: `verify_conservation` and
`verify_item_flow` need a finished run to look at, this one needs nothing but
the model. See `architecture/model_validation.md`.

Findings come in two severities. An **error** means the run would crash or the
numbers it produced would be meaningless: a Source with nothing wired to it, a
Combiner expecting more in-edges than it has, a branch that leads nowhere. A
**warning** means the run works and returns a plausible answer that is not the
one the modeller meant: items dropped at a non-blocking node, a conveyor whose
item length disagrees with the items travelling on it, an UNPACK splitter with
no pallets upstream.

Nothing here advances the clock or mutates the model, so it can be called on a
model that has never run and on one that has.
"""

from __future__ import annotations

from simtrace.model import FactoryModel
from simtrace.model import get_model as get_session_model

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


def _finding(check: str, severity: str, component: str | None, message: str) -> dict:
    """One entry in the report: what failed, where, and in plain words."""
    return {
        "check": check,
        "severity": severity,
        "component": component,
        "message": message,
    }


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

    A Splitter in UNPACK mode takes a packed item apart, and a Combiner packs
    into a container it pulls from its first in-edge. Both need a pallet to have
    entered the flow somewhere above them — from a `flow_item_type="pallet"`
    Source or from another Combiner.
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
                continue
            if _walk(sorted(upstream.get(node_id, set())), upstream) & origins:
                continue
            findings.append(
                _finding(
                    "pallet_mismatch",
                    _WARNING,
                    node_id,
                    f"Splitter '{node_id}' is in UNPACK mode but nothing "
                    "upstream of it packs items. Use mode='SPLIT', or feed it "
                    "from a pallet Source or a Combiner.",
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

    Warnings:
        silent_discard        a non-blocking node drops items at a full out-edge
        conveyor_item_length  a belt's item length disagrees with its Source's
        pallet_mismatch       packed items are handled with none produced above

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

    errors = [f for f in findings if f["severity"] == _ERROR]
    warnings = [f for f in findings if f["severity"] == _WARNING]

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "checked": {"nodes": len(model.nodes), "edges": len(model.edges)},
    }
