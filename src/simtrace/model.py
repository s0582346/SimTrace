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

    def __init__(self, strict_units: bool = False, time_unit: str = "min",
                 length_unit: str = "m") -> None:
        self.strict_units = strict_units # True: time, length and speed arguments need a unit (see tools.units)
        self.time_unit = time_unit # the unit model times are kept in
        self.length_unit = length_unit # the unit model lengths are kept in
        self.written: dict[str, dict[str, object]] = {} # component id -> {argument: text as written with its unit}, strict_units only
        self.env = simpy.Environment()
        self.nodes: dict[str, object] = {}
        self.edges: dict[str, object] = {}
        self.calendars: dict[str, object] = {} # calendar id -> ShiftCalendar; machines and sources refer to one by id
        self.default_calendar: str | None = None # id of the calendar that machines and sources without their own calendar follow
        self.events: list[dict] = [] # Item-flow events captured from the most recent run_simulation
        self.item_paths: dict[str, list[str]] = {} # Per-item trail through the plant from the most recent run. Built from `events` by telemetry.build_item_paths
        self.item_attributes: dict[str, dict] = {} # Item id -> payload stamped on it in the most recent run (part type plus its Source's attributes)
        self.spec: list[dict] = [] # Ordered build log ({"op", "kwargs"} per create_*/connect, raw args); replications replay it to rebuild the graph (see rebuild.build_from_spec)

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

    def record(self, op: str, kwargs: dict) -> None:
        """Append one build step to the spec.

        `kwargs` holds the caller's raw args (the `"exp(5)"` string, not the
        sampler) and no `model` — the replayer supplies a fresh one.
        """
        self.spec.append({"op": op, "kwargs": dict(kwargs)})

    def add_node(self, node_id: str, node: object) -> None:
        if self.has_id(node_id):
            raise ValueError(f"A node with id '{node_id}' already exists.")
        self.nodes[node_id] = node

    def add_edge(self, edge_id: str, edge: object) -> None:
        if self.has_id(edge_id):
            raise ValueError(f"An edge with id '{edge_id}' already exists.")
        self.edges[edge_id] = edge


_model: FactoryModel | None = None

# Unit settings of every new session model. Set once by the server at start
# (SIMTRACE_STRICT_UNITS, SIMTRACE_TIME_UNIT, SIMTRACE_LENGTH_UNIT); no tool
# changes them.
_unit_settings: dict = {"strict_units": False, "time_unit": "min", "length_unit": "m"}


def configure_units(strict_units: bool, time_unit: str = "min", length_unit: str = "m") -> None:
    """Set the unit settings for every session model made from now on."""
    from simtrace.tools.units import LENGTH_UNITS, TIME_UNITS

    if time_unit not in TIME_UNITS:
        raise ValueError(f"time unit must be one of {', '.join(TIME_UNITS)} (got {time_unit!r}).")
    if length_unit not in LENGTH_UNITS:
        raise ValueError(
            f"length unit must be one of {', '.join(LENGTH_UNITS)} (got {length_unit!r})."
        )
    _unit_settings.update(strict_units=bool(strict_units), time_unit=time_unit,
                          length_unit=length_unit)


def unit_settings() -> dict:
    return dict(_unit_settings)


def get_model() -> FactoryModel:
    """Return the current session model, creating it on first use."""
    global _model
    if _model is None:
        _model = FactoryModel(**_unit_settings)
    return _model


def reset_model() -> FactoryModel:
    """Discard the current model and start a fresh one."""
    global _model
    _model = FactoryModel(**_unit_settings)
    return _model
