"""Graph tools: wire the assembled components and inspect the wiring.

`connect` appends an edge to its src node's out_edges and its dest node's
in_edges; `get_model` returns a read-only snapshot of the resulting graph. Both
operate on the shared session model unless a `model` is supplied.

Conventions (see architecture/simulation_tools.md):
  - fail with friendly ValueErrors on unknown ids, not raw KeyErrors,
  - FactorySimPy prints to stdout during connect; that is suppressed here so it
    can't corrupt the MCP stdio (JSON-RPC) channel.
"""

from __future__ import annotations

from simgen.model import FactoryModel
from simgen.model import get_model as get_session_model
from simgen.tools.telemetry import traced_stdout


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
