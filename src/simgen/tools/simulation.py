"""Lifecycle tools that operate on the assembled model as a whole.

Where the `create_*` builders in `simgen.tools.nodes` / `simgen.tools.edges`
populate the session model with components, these tools wire those components
together (`connect`), inspect the current graph (`get_model`), and run the
simulation (`run_simulation`).

Conventions (see architecture/simulation_tools.md):
  - fail with friendly ValueErrors on unknown ids, not raw KeyErrors,
  - FactorySimPy prints to stdout during connect/run; that is suppressed here so
    it can't corrupt the MCP stdio (JSON-RPC) channel.
"""

from __future__ import annotations

from simgen.model import FactoryModel
from simgen.model import get_model as get_session_model
from simgen.model import reset_model as reset_session_model
from simgen.tools.telemetry import traced_stdout
from simgen.tools.utils import require_positive_number


def _edge_count(value: object) -> int:
    """Count a node's in_edges/out_edges, which start as None before wiring."""
    return len(value) if value else 0


def connect(
    edge_id: str,
    src_id: str,
    dest_id: str,
    *,
    model: FactoryModel | None = None,
) -> dict:
    """Wire a single edge between two nodes in the shared model.

    Resolves all three ids, then calls the edge's `connect`, which appends the
    edge to the source node's out_edges and the destination node's in_edges. An
    edge connects exactly one src to one dest; a node accumulates several in/out
    edges by being the endpoint of several connect calls.
    Args:
        edge_id: id of an existing edge (Buffer/Conveyor/Fleet).
        src_id: id of the source (upstream) node.
        dest_id: id of the destination (downstream) node.

    Returns a summary dict of the wiring.
    """
    model = model if model is not None else get_session_model()

    if not model.has_edge(edge_id):
        raise ValueError(f"No edge with id '{edge_id}' exists.")
    if not model.has_node(src_id):
        raise ValueError(f"No node with id '{src_id}' exists.")
    if not model.has_node(dest_id):
        raise ValueError(f"No node with id '{dest_id}' exists.")

    edge = model.edges[edge_id]
    if edge.src_node is not None or edge.dest_node is not None:
        raise ValueError(
            f"Edge '{edge_id}' is already connected "
            f"('{edge.src_node.id}' -> '{edge.dest_node.id}')."
        )

    src = model.nodes[src_id]
    dest = model.nodes[dest_id]
    with traced_stdout():
        edge.connect(src, dest)

    return {
        "edge": edge_id,
        "type": type(edge).__name__,
        "src": src_id,
        "dest": dest_id,
    }


def get_model(*, model: FactoryModel | None = None) -> dict:
    """Return a JSON-serializable snapshot of the current graph.

    Read-only: lists every node (with its in/out edge counts) and every edge
    (with its resolved src/dest node ids). Never exposes env/simpy objects.
    """
    model = model if model is not None else get_session_model()

    nodes = [
        {
            "id": node_id,
            "type": type(node).__name__,
            "in_edges": _edge_count(node.in_edges),
            "out_edges": _edge_count(node.out_edges),
        }
        for node_id, node in model.nodes.items()
    ]
    edges = [
        {
            "id": edge_id,
            "type": type(edge).__name__,
            "src": edge.src_node.id if edge.src_node is not None else None,
            "dest": edge.dest_node.id if edge.dest_node is not None else None,
        }
        for edge_id, edge in model.edges.items()
    ]
    return {"nodes": nodes, "edges": edges}


def reset_model() -> dict:
    """Discard the current session graph and start a fresh, empty one.

    Replaces the session model with a brand-new one: every node and edge is
    dropped and the simulation clock restarts at 0 (a new `simpy.Environment`).
    Use this to recover from a dirty session — leftover components from earlier
    work, an orphaned node that can't be wired or deleted individually, or a
    clock that has already advanced past the `until` you want to run to.

    Unlike the other lifecycle tools, this always operates on the shared session
    model (there is nothing to reset in a caller-supplied one).

    Returns a summary of what was cleared and the reset clock.
    """
    old = get_session_model()
    cleared_nodes = len(old.nodes)
    cleared_edges = len(old.edges)

    reset_session_model()

    return {
        "cleared_nodes": cleared_nodes,
        "cleared_edges": cleared_edges,
        "now": 0,
    }


def run_simulation(
    until: float,
    *,
    model: FactoryModel | None = None,
) -> dict:
    """Run the simulation up to time `until` and return a stats summary.

    This is where FactorySimPy's scheduled `behaviour()` processes actually
    execute, so any unmet edge-cardinality requirement (e.g. a Source with no
    out_edge) surfaces here as an AssertionError. Run `validate_model` first to
    catch those as friendly errors.
    Args:
        until: simulation end time; must be a positive number.

    Returns a summary dict with the end time and per-node/edge stats.
    """
    model = model if model is not None else get_session_model()

    require_positive_number("until", until)

    # Capture this run's item-flow events onto the model so the validation tools
    # can analyze them afterwards. Cleared first so each run reflects only its own events
    model.events.clear()
    with traced_stdout(collector=model.events):
        model.env.run(until=until)

    nodes = {
        node_id: getattr(node, "stats", None)
        for node_id, node in model.nodes.items()
    }
    edges = {
        edge_id: getattr(edge, "stats", None)
        for edge_id, edge in model.edges.items()
    }
    return {
        "until": until,
        "now": model.env.now,
        "nodes": nodes,
        "edges": edges,
    }
