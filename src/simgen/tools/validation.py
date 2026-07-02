"""Post-run verification tools that check a *completed* simulation.

Where `simulation.run_simulation` executes the model, these tools inspect what
happened. `verify_conservation` answers "are all generated items accounted for?"
by a mass-balance over the nodes' and edges' ground-truth item counters.

This is distinct from the static graph checks a future `validate_model` will do
*before* a run (cardinality, orphans): conservation is a *dynamic* check — it
needs a run to have happened.

Conventions (mirrors the other tool modules):
  - fail with friendly ValueErrors, not raw KeyErrors,
  - never expose env/simpy objects in results,
  - operate on the shared session model unless a `model` is supplied.
"""

from __future__ import annotations

from simgen.model import FactoryModel
from simgen.model import get_model as get_session_model


# --- conservation (mass balance) -----------------------------------------
#
# Every physical item a Source generates must, when the run stops, be in exactly
# one place: delivered to a Sink, still sitting in an edge (buffer/conveyor/fleet
# WIP), still inside a Machine being processed, or discarded (dropped when a
# non-blocking node found its out_edge full). Nothing else can happen to it. So
#
#     generated  ==  received  +  in_edges  +  in_machines  +  discarded
#
# is an identity that must hold to the item; any shortfall means items vanished
# without a trace (a modelling or engine bug), any surplus means items appeared
# from nowhere. This is the check the flowchart calls for: reconcile the stats
# against what was generated.
#
# The three left-hand terms below come straight from ground-truth counters and
# live edge levels. `in_machines` is the one term with no direct counter, so it
# is *derived* as the residual — but only after we account for the count-changing
# nodes (see `_PACKING_NODES`), so a real leak can't hide inside the residual.

# Nodes that emit a different number of physical items than they consume:
#   - Splitter unpacks one packed pallet into the N items inside it (plus the
#     empty pallet), so it EMITS MORE physical items than it pulled in.
#   - Combiner packs items pulled from several in_edges into one pallet, so it
#     EMITS FEWER — the packed items ride downstream nested inside the pallet.
# Their `num_item_processed` counts physical items *emitted*, so the net change
# in item count across the whole line is (emitted - consumed) at these nodes.
# We can read `emitted` from the counter but not `consumed`, so the presence of
# any such node makes the strict global identity inexact; we report it rather
# than silently absorbing the difference into the machine residual.
_PACKING_NODES = frozenset({"Splitter", "Combiner"})

# Per node type, the stats field holding physical items that LEFT the node for a
# downstream edge (a Source emits what it generated; workers emit what they
# processed). Sinks are terminal (they receive, never emit) so they have none.
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

    Performs a mass balance over the last run: the number of items all Sources
    generated must equal the items delivered to Sinks, plus items still in
    transit inside edges (buffer/conveyor/fleet WIP), plus items still being
    processed inside Machines, plus items discarded. If the two sides differ,
    items were lost (or spuriously created) — the alarm the flowchart calls for.

    Requires a prior `run_simulation` (a model whose clock is still at 0 raises).

    The machine-WIP term has no direct counter; it is derived as the residual
    that closes the balance, so on a clean run `balanced` is always True and
    `in_machines` tells you how many items were mid-process when the clock
    stopped. The residual is trustworthy only when it can't be hiding a real
    leak, which is why count-changing nodes are handled explicitly: a Splitter
    or Combiner emits a different number of physical items than it consumed (it
    un/re-packs pallets), and its consumed-count isn't recorded. When the model
    contains any such node the strict identity can't be closed from counters
    alone; `balanced` is then reported as None and `exact` is False, with the
    offending nodes listed in `packing_nodes`. For the common source→machine→
    sink lines (no packing) the check is exact.

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

    # Items still inside machines are the ones that entered the transformation
    # stages but have neither been emitted downstream, discarded, delivered, nor
    # left sitting in an edge. Deriving it as the residual makes the identity
    # close by construction on a clean, packing-free run; the value itself is
    # the useful output (how much WIP was mid-process at the cutoff).
    in_machines = generated - received - in_edges - discarded

    exact = not packing_nodes
    if exact:
        # No count-changing nodes: the residual is genuine machine WIP and can't
        # go negative on a consistent run. A negative value means items appeared
        # from nowhere (surplus) — surface it instead of clamping.
        balanced: bool | None = in_machines >= 0
        in_machines_reported = in_machines
    else:
        # A splitter/combiner changed the physical item count by an amount we
        # can't read from counters, so the residual conflates real WIP with the
        # packing delta. Don't claim a verdict or a machine-WIP figure we can't
        # stand behind.
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
