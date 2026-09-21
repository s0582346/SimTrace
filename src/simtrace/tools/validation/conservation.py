"""Post-run material balance: report where every generated item ended up.

`verify_conservation` reads the nodes' and edges' item counters, counts what sits
inside the nodes directly, and checks that the four places add up to what the
sources generated. Alongside the check it reports the split itself — delivered,
discarded, still in edges, still in stations — with plain findings on what those
numbers show. See `architecture/conservation.md`.
"""

from __future__ import annotations

from simtrace.model import FactoryModel
from simtrace.model import get_model as get_session_model

_PACKING_NODES = frozenset({"Splitter", "Combiner"})

_EMITTED_COUNTER: dict[str, str] = {
    "Source": "num_item_generated",
    "Machine": "num_item_processed",
    "Splitter": "num_item_processed",
    "Combiner": "num_item_processed",
}

_DISCARDED_COUNTER = "num_item_discarded"


def _counter(node: object, field: str) -> int:
    """Read an integer stats counter off a node, defaulting to 0.

    A missing field or a non-dict `stats` yields 0 rather than raising: a node
    type that simply doesn't track this counter contributes nothing to the sum,
    which is the correct behaviour for a Sink's emitted-count or a Source's
    processed-count.
    """
    stats = getattr(node, "stats", None)
    if not isinstance(stats, dict):
        return 0
    value = stats.get(field, 0)
    return value if isinstance(value, int) else 0


def _items_in_nodes(model: FactoryModel) -> int:
    """Count the items held inside nodes right now, directly.

    A machine keeps one worker process per item it is working on or waiting to
    hand over, and lists them in `worker_thread_list`. A source that generated an
    item it could not hand over yet is in BLOCKED_STATE and holds that one item.
    Sinks hold nothing. Splitters and combiners are left out because the caller
    declines to count on lines that contain them.
    """
    total = 0
    for node in model.nodes.values():
        node_type = type(node).__name__
        if node_type == "Machine":
            total += len(node.worker_thread_list)
        elif node_type == "Source" and getattr(node, "state", None) == "BLOCKED_STATE":
            total += 1
    return total


def _share(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "0%"


def _findings(
    *,
    generated: int,
    in_edges: int,
    in_machines: int | None,
    discarded: int,
    missing: int | None,
    packing_nodes: list[str],
    by_node: dict[str, dict],
) -> list[dict[str, str]]:
    """Describe what the run's numbers show, without judging the model.

    Each entry states an observation the model's author can act on. A clean run
    still yields entries (what is left in the line), so an empty list never
    doubles as an all-clear.
    """
    findings: list[dict[str, str]] = []

    if missing:
        if missing > 0:
            findings.append({
                "kind": "missing",
                "text": (
                    f"{missing} of {generated} generated items are missing. They "
                    "did not reach a sink, are in no edge, in no station and were "
                    "not discarded, so they were lost inside the simulation."
                ),
            })
        else:
            findings.append({
                "kind": "impossible",
                "text": (
                    f"{-missing} more items were accounted for than were "
                    "generated. Items cannot appear from nowhere, so this points "
                    "at a fault in the simulation rather than in the model."
                ),
            })

    if discarded:
        dropping = [i for i, node in by_node.items() if node["discarded"]]
        where = ", ".join(dropping) if dropping else "non-blocking nodes"
        findings.append({
            "kind": "discarded",
            "text": (
                f"{discarded} of {generated} generated items "
                f"({_share(discarded, generated)}) were dropped at {where}. A node "
                "drops items when it is set to non-blocking and its out edge is "
                "full."
            ),
        })

    unfinished = in_edges + (in_machines or 0)
    if unfinished:
        inside = (
            f"{in_edges} in edges"
            if in_machines is None
            else f"{in_edges} in edges and {in_machines} in stations"
        )
        findings.append({
            "kind": "unfinished",
            "text": (
                f"{unfinished} of {generated} items "
                f"({_share(unfinished, generated)}) had not left the line when the "
                f"run stopped, {inside}. A large share means the run was too short "
                "or the line is congested."
            ),
        })

    if packing_nodes:
        findings.append({
            "kind": "not_countable",
            "text": (
                f"{', '.join(packing_nodes)} pack or unpack items, so the number "
                "of physical items changes there. Items inside stations are not "
                "counted, and counts taken before and after these nodes are not "
                "comparable."
            ),
        })

    return findings


def _summary(
    *,
    generated: int,
    received: int,
    in_edges: int,
    in_machines: int | None,
    discarded: int,
    missing: int | None,
) -> str:
    """State where every generated item ended up, in one sentence."""
    parts = [f"{received} reached a sink"]
    if discarded:
        parts.append(f"{discarded} were discarded")
    parts.append(f"{in_edges} are in edges")
    parts.append(
        f"{in_machines} are in stations"
        if in_machines is not None
        else "the number inside stations is unknown"
    )
    if missing:
        parts.append(f"{missing} are missing" if missing > 0 else "the sum is too large")
    return f"Of {generated} items generated, " + ", ".join(parts) + "."


def _edge_level(edge: object) -> int:
    """Return how many items are currently sitting inside an edge.

    Buffers and fleets keep their items in `inbuiltstore`; conveyors keep them
    in `belt`. Both split the contents into `items` (not yet available to get)
    and `ready_items` (available), and the live level is the sum of the two —
    the same expression FactorySimPy uses internally for can_put/is_empty. An
    edge type we don't recognise contributes 0 and is reported so it can't
    silently unbalance the total.
    """
    store = getattr(edge, "inbuiltstore", None)
    if store is None:
        store = getattr(edge, "belt", None)
    if store is None:
        return 0
    items = getattr(store, "items", None) or []
    ready = getattr(store, "ready_items", None) or []
    return len(items) + len(ready)


def verify_conservation(*, model: FactoryModel | None = None) -> dict:
    """Reconcile every generated item against where it ended up.

    Splits the items all Sources generated into four places and checks that they
    add up:

        generated == received + in_edges + in_machines + discarded

    `generated`, `received` and `discarded` are ground-truth counters, `in_edges`
    is the live level of every edge, and `in_machines` is counted directly from
    the nodes (each machine's running workers plus a source holding an item it
    could not hand over). Because none of the four is derived from the others,
    the identity can fail:

        missing = generated - (received + in_edges + in_machines + discarded)

    A positive `missing` means items were lost inside the simulation, a negative
    one that items appeared from nowhere. A model built through the tools cannot
    produce either, since every node counts what it drops, so a non-zero value
    points at a fault in the simulation rather than at the model. What a model
    can show is the split itself, which `findings` describes: items dropped at
    non-blocking nodes, and how much never left the line.

    Requires a prior `run_simulation` (a model whose clock is still at 0 raises).

    A Splitter or Combiner emits a different number of physical items than it
    consumed (it un/re-packs pallets) and its consumed-count is not recorded, so
    the items inside nodes cannot be compared with the counters. When the model
    contains any such node, `in_machines` and `missing` are reported as None,
    `exact` is False, and the offending node ids are listed in `packing_nodes`.

    Returns a summary dict:
        {
          "now": <sim clock at end of last run>,
          "generated": int,          # items all Sources produced
          "received": int,           # items all Sinks collected
          "in_edges": int,           # flow-item objects still inside buffers/
                                     # conveyors/fleets (a Pallet = 1, its packed
                                     # contents are not counted)
          "in_machines": int,        # items held inside nodes, counted directly
                                     # (None if not exact)
          "discarded": int,          # items dropped at non-blocking nodes
          "accounted": int,          # received + in_edges + in_machines + discarded
          "missing": int,            # generated - accounted (None if not exact)
          "exact": bool,             # False when packing nodes make it inexact
          "packing_nodes": [id,...], # splitter/combiner ids that break exactness
          "summary": str,            # where every generated item ended up
          "findings": [{"kind": str, "text": str}, ...],  # what the numbers show
          "by_edge": {edge_id: level, ...},
          "by_node": {node_id: {type, generated, emitted, discarded, received}},
        }
    """
    model = model if model is not None else get_session_model()

    if model.env.now == 0:
        raise ValueError(
            "No simulation has run yet. "
            "Call run_simulation before verify_conservation."
        )

    generated = 0
    received = 0
    discarded = 0
    emitted_total = 0
    packing_nodes: list[str] = []
    by_node: dict[str, dict] = {}

    for node_id, node in model.nodes.items():
        node_type = type(node).__name__
        node_generated = _counter(node, "num_item_generated")
        node_received = _counter(node, "num_item_received")
        node_discarded = _counter(node, _DISCARDED_COUNTER)
        emitted_field = _EMITTED_COUNTER.get(node_type)
        node_emitted = _counter(node, emitted_field) if emitted_field else 0

        generated += node_generated
        received += node_received
        discarded += node_discarded
        emitted_total += node_emitted

        if node_type in _PACKING_NODES:
            packing_nodes.append(node_id)

        by_node[node_id] = {
            "type": node_type,
            "generated": node_generated,
            "emitted": node_emitted,
            "discarded": node_discarded,
            "received": node_received,
        }

    by_edge = {edge_id: _edge_level(edge) for edge_id, edge in model.edges.items()}
    in_edges = sum(by_edge.values())

    exact = not packing_nodes
    in_machines: int | None
    missing: int | None
    if exact:
        in_machines = _items_in_nodes(model)
        missing = generated - (received + in_edges + in_machines + discarded)
    else:
        # A splitter/combiner changed the physical item count by an amount we
        # can't read from counters, so what sits inside nodes can't be compared.
        in_machines = None
        missing = None

    accounted = received + in_edges + discarded
    if in_machines is not None:
        accounted += in_machines

    return {
        "now": model.env.now,
        "generated": generated,
        "received": received,
        "in_edges": in_edges,
        "in_machines": in_machines,
        "discarded": discarded,
        "accounted": accounted,
        "missing": missing,
        "exact": exact,
        "packing_nodes": packing_nodes,
        "summary": _summary(
            generated=generated,
            received=received,
            in_edges=in_edges,
            in_machines=in_machines,
            discarded=discarded,
            missing=missing,
        ),
        "findings": _findings(
            generated=generated,
            in_edges=in_edges,
            in_machines=in_machines,
            discarded=discarded,
            missing=missing,
            packing_nodes=packing_nodes,
            by_node=by_node,
        ),
        "by_edge": by_edge,
        "by_node": by_node,
    }
