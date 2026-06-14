"""Builders for FactorySimPy *Node* instances (the active components).

Each `create_*` function is a plain, transport-agnostic builder: it validates
its arguments, instantiates the FactorySimPy node bound to the shared model
environment, registers it, and returns a small JSON-serializable summary.
The MCP server wraps these as tools; tests call them directly.

Conventions (see architecture/TOOLS_PLAN.md):
  - reject duplicate ids,
  - always bind to `model.env`, never leak env/simpy objects in results,
  - in_edges/out_edges start empty — wiring happens later via `connect`,
  - v1 delay/selection params accept constant int/float only.
"""

from __future__ import annotations

from factorysimpy.nodes.sink import Sink
from factorysimpy.nodes.source import Source

from simgen.model import FactoryModel, get_model
from simgen.tools.utils import require_number


def create_source(
    id: str,
    inter_arrival_time: float = 1.0,
    flow_item_type: str = "item",
    item_length: float = 1,
    blocking: bool = False,
    out_edge_selection: str = "FIRST_AVAILABLE",
    node_setup_time: float = 0,
    *,
    model: FactoryModel | None = None,
) -> dict:
    """Create a Source node and register it in the shared model.

    A Source generates flow items and has no in_edges and exactly one out_edge
    (wired later). Args:
        id: unique node identifier.
        inter_arrival_time: constant time between item generations. Must be
            non-zero when `blocking=False`.
        flow_item_type: "item" or "pallet".
        item_length: physical length assigned to each generated item; only
            used by conveyor edges (travel time = length/speed) and ignored by
            plain buffers.
        blocking: if True, wait for out_edge space; if False, discard when full.
        out_edge_selection: edge-selection strategy, one of "FIRST_AVAILABLE",
            "RANDOM", "ROUND_ROBIN".
        node_setup_time: constant initial setup delay.

    Returns a summary dict echoing the stored parameters.
    """
    model = model if model is not None else get_model()

    if not isinstance(id, str) or not id:
        raise ValueError("id must be a non-empty string.")
    if model.has_node(id):
        raise ValueError(f"A node with id '{id}' already exists.")

    require_number("inter_arrival_time", inter_arrival_time)
    require_number("item_length", item_length)
    require_number("node_setup_time", node_setup_time)
    if flow_item_type not in ("item", "pallet"):
        raise ValueError('flow_item_type must be "item" or "pallet".')
    if not isinstance(out_edge_selection, str):
        raise ValueError("out_edge_selection must be a string (v1).")
    if not isinstance(blocking, bool):
        raise ValueError("blocking must be a bool.")

    node = Source(
        env=model.env,
        id=id,
        inter_arrival_time=inter_arrival_time,
        flow_item_type=flow_item_type,
        item_length=item_length,
        blocking=blocking,
        out_edge_selection=out_edge_selection,
    )
    # Source.__init__ doesn't forward node_setup_time to the base Node, so set it
    # explicitly (validated above).
    node.node_setup_time = node_setup_time

    model.add_node(id, node)

    return {
        "id": id,
        "type": "Source",
        "inter_arrival_time": inter_arrival_time,
        "flow_item_type": flow_item_type,
        "item_length": item_length,
        "blocking": blocking,
        "out_edge_selection": out_edge_selection,
        "node_setup_time": node_setup_time,
        "in_edges": 0,
        "out_edges": 0,
    }


def create_sink(
    id: str,
    node_setup_time: float = 0,
    *,
    model: FactoryModel | None = None,
) -> dict:
    """Create a Sink node and register it in the shared model.

    A Sink is a terminal node that collects flow items at the end of the line.
    It has no out_edge and needs at least one in_edge (wired later via connect).
    Args:
        id: unique node identifier.
        node_setup_time: constant initial setup delay.

    Returns a summary dict echoing the stored parameters.
    """
    model = model if model is not None else get_model()

    if not isinstance(id, str) or not id:
        raise ValueError("id must be a non-empty string.")
    if model.has_node(id):
        raise ValueError(f"A node with id '{id}' already exists.")

    require_number("node_setup_time", node_setup_time)

    node = Sink(env=model.env, id=id, node_setup_time=node_setup_time)

    model.add_node(id, node)

    return {
        "id": id,
        "type": "Sink",
        "node_setup_time": node_setup_time,
        "in_edges": 0,
        "out_edges": 0,
    }
