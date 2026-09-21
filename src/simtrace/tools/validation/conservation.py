"""Post-run material balance: report where every generated item ended up.

`verify_conservation` reads the nodes' and edges' ground-truth item counters and
reports the run's material balance — delivered, discarded, still in edges, still
in stations — with plain findings on what those numbers show. It gives no
pass/fail verdict. See `architecture/conservation.md` for why a verdict would be
vacuous and what the numbers can and cannot reveal.
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


def _station_capacity(model: FactoryModel) -> int:
    """Return how many items can sit inside nodes at one moment.

    A machine holds one item per worker thread, so `work_capacity` items, and a
    source holds at most the one item it generated but could not hand over yet.
    Sinks hold nothing. The result is an upper bound on a run's genuine
    machine WIP, which is what makes an oversized residual readable as loss.
    """
    total = 0
    for node in model.nodes.values():
        node_type = type(node).__name__
        if node_type == "Machine":
            capacity = getattr(node, "work_capacity", 1)
            total += capacity if isinstance(capacity, int) and capacity > 0 else 1
        elif node_type == "Source":
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
    station_capacity: int | None,
    packing_nodes: list[str],
    by_node: dict[str, dict],
) -> list[dict[str, str]]:
    """Describe what the run's numbers show, without judging the model.

    Each entry states an observation the model's author can act on. A clean run
    still yields entries (what is left in the line), so an empty list never
    doubles as an all-clear.
    """
    findings: list[dict[str, str]] = []

    if in_machines is not None and station_capacity is not None:
        if in_machines < 0:
            findings.append({
                "kind": "impossible",
                "text": (
                    f"{-in_machines} more items were accounted for than were "
                    "generated. Items cannot appear from nowhere, so this points "
                    "at a fault in the simulation rather than in the model."
                ),
            })
        elif in_machines > station_capacity:
            findings.append({
                "kind": "unaccounted",
                "text": (
                    f"{in_machines} items are left over once deliveries, edge "
                    "contents and discards are subtracted, but the stations can "
                    f"hold at most {station_capacity} items at once. At least "
                    f"{in_machines - station_capacity} items went missing."
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
        if in_machines is None:
            inside = f"{in_edges} in edges"
        elif station_capacity is not None and in_machines > station_capacity:
            inside = f"{in_edges} in edges and {in_machines} left over"
        else:
            inside = f"{in_edges} in edges and {in_machines} in stations"
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
                "of physical items changes there. Items inside stations cannot be "
                "derived, and counts taken before and after these nodes are not "
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
    station_capacity: int | None,
) -> str:
    """State where every generated item ended up, in one sentence."""
    parts = [f"{received} reached a sink"]
    if discarded:
        parts.append(f"{discarded} were discarded")
    parts.append(f"{in_edges} are in edges")
    if in_machines is None:
        parts.append("the number inside stations is unknown")
    elif station_capacity is not None and in_machines > station_capacity:
        parts.append(f"{in_machines} are left over, more than the stations can hold")
    else:
        parts.append(f"{in_machines} are in stations")
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

    Mass-balances the last run against the identity

        generated == received + in_edges + in_machines + discarded

    where `generated`, `received`, `discarded`, and `in_edges` are read from
    ground-truth counters and live edge levels, and `in_machines` is derived as
    the residual (there is no machine-WIP counter):

        in_machines = generated - received - in_edges - discarded

    Requires a prior `run_simulation` (a model whose clock is still at 0 raises).

    Because `in_machines` is the residual, the identity closes by construction,
    so there is no pass/fail verdict to give: lost items are absorbed by the
    residual instead of breaking the sum. What the residual can be measured
    against is `station_capacity`, the number of items the nodes can hold at
    once — a residual above it means items went missing, and a negative one
    means items appeared from nowhere. Both are reported in `findings`.

    A Splitter or Combiner emits a different number of physical items than it
    consumed (it un/re-packs pallets) and its consumed-count is not recorded, so
    the residual conflates machine WIP with that unknown delta. When the model
    contains any such node, `in_machines` and `station_capacity` are reported as
    None, `exact` is False, and the offending node ids are listed in
    `packing_nodes`. For lines without a Splitter or Combiner the check is exact.

    Returns a summary dict:
        {
          "now": <sim clock at end of last run>,
          "generated": int,          # items all Sources produced
          "received": int,           # items all Sinks collected
          "in_edges": int,           # flow-item objects still inside buffers/
                                     # conveyors/fleets (a Pallet = 1, its packed
                                     # contents are not counted)
          "in_machines": int,        # items still being processed (derived residual)
          "discarded": int,          # items dropped at non-blocking nodes
          "accounted": int,          # received + in_edges + in_machines + discarded
          "station_capacity": int,   # items all nodes can hold at once (None if
                                     # not exact); the residual's upper bound
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

    # No counter for machine WIP, so derive it as the residual: whatever wasn't
    # delivered, left in an edge, or discarded is still mid-process at the cutoff.
    in_machines = generated - received - in_edges - discarded

    exact = not packing_nodes
    if exact:
        in_machines_reported: int | None = in_machines
        station_capacity: int | None = _station_capacity(model)
    else:
        # A splitter/combiner changed the physical item count by an amount we
        # can't read from counters, so the residual conflates real WIP with the
        # packing delta, and no bound on it is meaningful.
        in_machines_reported = None
        station_capacity = None

    accounted = received + in_edges + discarded
    if in_machines_reported is not None:
        accounted += in_machines_reported

    return {
        "now": model.env.now,
        "generated": generated,
        "received": received,
        "in_edges": in_edges,
        "in_machines": in_machines_reported,
        "discarded": discarded,
        "accounted": accounted,
        "station_capacity": station_capacity,
        "exact": exact,
        "packing_nodes": packing_nodes,
        "summary": _summary(
            generated=generated,
            received=received,
            in_edges=in_edges,
            in_machines=in_machines_reported,
            discarded=discarded,
            station_capacity=station_capacity,
        ),
        "findings": _findings(
            generated=generated,
            in_edges=in_edges,
            in_machines=in_machines_reported,
            discarded=discarded,
            station_capacity=station_capacity,
            packing_nodes=packing_nodes,
            by_node=by_node,
        ),
        "by_edge": by_edge,
        "by_node": by_node,
    }
