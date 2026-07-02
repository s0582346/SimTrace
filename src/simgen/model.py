"""Shared, session-scoped simulation model.

Holds the single `simpy.Environment` that every Node instance is bound to,
plus the registries of nodes and edges that the MCP tools build up.

For now there is exactly one model per MCP server session, exposed as a
module-level singleton via `get_model()`. `reset_model()` starts a fresh one
(used by tests and, eventually, a "new model" tool).
"""

from __future__ import annotations

import simpy


class FactoryModel:
    """The mutable graph the tools assemble: env + node/edge registries."""

    def __init__(self) -> None:
        self.env = simpy.Environment()
        self.nodes: dict[str, object] = {}
        self.edges: dict[str, object] = {}
        self.events: list[dict] = [] # Item-flow events captured from the most recent run_simulation

    def has_node(self, node_id: str) -> bool:
        return node_id in self.nodes

    def has_edge(self, edge_id: str) -> bool:
        return edge_id in self.edges

    def has_id(self, component_id: str) -> bool:
        """True if the id is taken by either a node or an edge.

        Node and edge ids share one namespace so that `connect`/`validate_model`
        can resolve any endpoint by a single, unambiguous id.
        """
        return component_id in self.nodes or component_id in self.edges

    def add_node(self, node_id: str, node: object) -> None:
        if self.has_id(node_id):
            raise ValueError(f"A node with id '{node_id}' already exists.")
        self.nodes[node_id] = node

    def add_edge(self, edge_id: str, edge: object) -> None:
        if self.has_id(edge_id):
            raise ValueError(f"An edge with id '{edge_id}' already exists.")
        self.edges[edge_id] = edge


_model: FactoryModel | None = None


def get_model() -> FactoryModel:
    """Return the current session model, creating it on first use."""
    global _model
    if _model is None:
        _model = FactoryModel()
    return _model


def reset_model() -> FactoryModel:
    """Discard the current model and start a fresh one."""
    global _model
    _model = FactoryModel()
    return _model
