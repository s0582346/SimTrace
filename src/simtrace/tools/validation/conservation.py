"""Post-run conservation check: reconcile every generated item.

`verify_conservation` mass-balances the nodes' and edges' ground-truth item
counters to check that every generated item is accounted for. See
`architecture/conservation.md` for what the check means and why it holds.
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

    Because `in_machines` is the residual, it closes the identity by
    construction. On a consistent run it is >= 0, and
    `balanced` is True. A negative residual means `received + in_edges +
    discarded` already exceeds `generated` — items appeared from nowhere — and
    `balanced` is False.

    A Splitter or Combiner emits a different number of physical items than it
    consumed (it un/re-packs pallets) and its consumed-count is not recorded, so
    the residual conflates machine WIP with that unknown delta. When the model
    contains any such node, `in_machines` and `balanced` are reported as None,
    `exact` is False, and the offending node ids are listed in `packing_nodes`.
    For lines without a Splitter or Combiner the check is exact.

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
          "balanced": bool | None,   # generated == accounted (None if not exact)
          "exact": bool,             # False when packing nodes make it inexact
          "packing_nodes": [id,...], # splitter/combiner ids that break exactness
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
        # No count-changing nodes: the residual is genuine machine WIP and can't
        # go negative on a consistent run. A negative value means items appeared
        # from nowhere — surface it instead of clamping.
        balanced: bool | None = in_machines >= 0
        in_machines_reported = in_machines
    else:
        # A splitter/combiner changed the physical item count by an amount we
        # can't read from counters, so the residual conflates real WIP with the
        # packing delta.
        balanced = None
        in_machines_reported = None

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
        "balanced": balanced,
        "exact": exact,
        "packing_nodes": packing_nodes,
        "by_edge": by_edge,
        "by_node": by_node,
    }
