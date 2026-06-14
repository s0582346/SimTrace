"""Central registration of MCP tools onto a FastMCP server.

Each tool here is a thin wrapper that delegates to
a plain builder in `simgen.tools.nodes` (which owns validation and the shared
model). As new node tools land, register them in `register_tools`.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from simgen.tools import nodes


def register_tools(mcp: FastMCP) -> FastMCP:
    """Register every block-building tool on `mcp` and return it."""

    @mcp.tool()
    def create_source(
        id: str,
        inter_arrival_time: float = 1.0,
        flow_item_type: str = "item",
        item_length: float = 1,
        blocking: bool = False,
        out_edge_selection: str = "FIRST_AVAILABLE",
        node_setup_time: float = 0,
    ) -> dict:
        """Create a Source node that generates flow items into the model.

        A Source has no in_edges and exactly one out_edge (wired later via
        connect).

        Args:
            id: unique node identifier.
            inter_arrival_time: constant time between item generations; must be
                non-zero when blocking is False.
            flow_item_type: "item" or "pallet".
            item_length: length assigned to each generated item.
            blocking: if True, wait for out_edge space; if False, discard when
                full.
            out_edge_selection: edge-selection strategy, e.g. "FIRST_AVAILABLE",
                "RANDOM", "ROUND_ROBIN", "FIRST", "LAST".
            node_setup_time: constant initial setup delay.
        """
        return nodes.create_source(
            id=id,
            inter_arrival_time=inter_arrival_time,
            flow_item_type=flow_item_type,
            item_length=item_length,
            blocking=blocking,
            out_edge_selection=out_edge_selection,
            node_setup_time=node_setup_time,
        )

    return mcp
