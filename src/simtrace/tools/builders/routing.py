"""Conditional routing: a Machine picks its out-edge from an item attribute.

`create_machine(route_by=..., routes=...)` names the attribute to read and
maps each of its values to the id of one of the machine's out-edges. The key
"*" is an optional fallback for every other value.

When a worker is about to hand its item on, FactorySimPy's Machine calls a
callable `out_edge_selection` with that item (`Machine._get_out_edge_index`).
`route_selector` builds that callable.

Only string attribute values match a route. When the target edge is full, the
Machine waits (blocking=True) or discards the item (blocking=False); no other
edge is tried.
"""

from __future__ import annotations

from typing import Callable

FALLBACK = "*"


def resolve_routes(
    route_by: str | None,
    routes: dict[str, str] | None,
    out_edge_selection: str,
) -> dict[str, str] | None:
    """Validate a Machine's routing rule; return a copy of `routes` or None."""
    # No routing rule: the machine keeps its normal out_edge_selection.
    if route_by is None and routes is None:
        return None
    # The two parameters only work together.
    if route_by is None:
        raise ValueError("routes need a route_by attribute.")
    if routes is None:
        raise ValueError("route_by needs routes (attribute value -> edge id).")
    # Check the types: a non-empty name and a non-empty dict of strings.
    if not isinstance(route_by, str) or not route_by:
        raise ValueError("route_by must be a non-empty string.")
    if not isinstance(routes, dict) or not routes:
        raise ValueError("routes must be a non-empty dict of value -> edge id.")
    for value, edge_id in routes.items():
        if not isinstance(value, str) or not value:
            raise ValueError("route values must be non-empty strings.")
        if not isinstance(edge_id, str) or not edge_id:
            raise ValueError(f"routes[{value!r}] must be a non-empty edge id.")
    # The rule takes the place of out_edge_selection, so that must stay unset.
    if out_edge_selection != "FIRST_AVAILABLE":
        raise ValueError(
            "route_by replaces out_edge_selection; leave out_edge_selection at "
            f'"FIRST_AVAILABLE" (got {out_edge_selection!r}).'
        )
    return dict(routes)


def pick_edge_id(routes: dict[str, str], value: object) -> str | None:
    """The edge id `routes` gives for `value`, else the "*" edge, else None."""
    # Only string values can match a route.
    edge_id = routes.get(value) if isinstance(value, str) else None
    # No match: use the "*" edge if there is one.
    if edge_id is None:
        edge_id = routes.get(FALLBACK)
    return edge_id


def route_selector(
    machine: object, route_by: str, routes: dict[str, str]
) -> Callable[[object], int]:
    """Return the out-edge selector for `machine`.

    The selector gets the item and returns the index of the route's edge in
    `machine.out_edges`. It raises ValueError when the item's value has no route and there is no
    "*" fallback, or when the route's edge is not an out-edge of the machine.
    """

    def select(item: object) -> int:
        # Read the item's value; untyped items have no payload.
        value = (item.payload or {}).get(route_by)
        edge_id = pick_edge_id(routes, value)
        # No route and no "*": stop the run.
        if edge_id is None:
            raise ValueError(
                f"Machine '{machine.id}': item '{item.id}' has "
                f"{route_by}={value!r}, which no route matches and there is no "
                f'"{FALLBACK}" fallback.'
            )
        # FactorySimPy wants the edge's position in out_edges, not its id.
        for index, edge in enumerate(machine.out_edges):
            if edge.id == edge_id:
                return index
        # The route names an edge that does not leave this machine.
        raise ValueError(
            f"Machine '{machine.id}': route target '{edge_id}' is not one of "
            "its out-edges."
        )

    return select
