"""Routing: a Machine picks its out-edge by item attribute or by chance.

`create_machine(route_by=..., routes=...)` names the attribute to read and
maps each of its values to the id of one of the machine's out-edges. The key
"*" is an optional fallback for every other value.

`create_machine(route_shares=...)` maps out-edge ids to shares that add up to
1, e.g. {"b_ship": 0.95, "b_scrap": 0.05}. For each item, chance picks the
edge, weighted by the shares. The picks use Python's global `random`, so the
seed passed to run_simulation makes them repeatable.

When a worker is about to hand its item on, FactorySimPy's Machine calls a
callable `out_edge_selection` with that item (`Machine._get_out_edge_index`).
`route_selector` builds that callable.

Only string attribute values match a route. When the target edge is full, the
Machine waits (blocking=True) or discards the item (blocking=False); no other
edge is tried.
"""

from __future__ import annotations

import math
import random
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


def resolve_shares(
    route_shares: dict[str, float] | None,
    route_by: str | None,
    out_edge_selection: str,
) -> dict[str, float] | None:
    """Validate a Machine's share rule; return a copy of `route_shares` or None."""
    # no share rule.
    if route_shares is None:
        return None
    # only one way of choosing the out-edge per machine.
    if route_by is not None:
        raise ValueError("route_shares cannot be combined with route_by.")
    if out_edge_selection != "FIRST_AVAILABLE":
        raise ValueError(
            "route_shares replaces out_edge_selection; leave out_edge_selection "
            f'at "FIRST_AVAILABLE" (got {out_edge_selection!r}).'
        )
    # Must be a dict like {"b_ship": 0.9}: each edge id gets a share from 0 to 1.
    if not isinstance(route_shares, dict) or not route_shares:
        raise ValueError("route_shares must be a non-empty dict of edge id -> share.")
    for edge_id, share in route_shares.items():
        if not isinstance(edge_id, str) or not edge_id:
            raise ValueError("route_shares keys must be non-empty edge ids.")
        if isinstance(share, bool) or not isinstance(share, (int, float)):
            raise ValueError(f"route_shares[{edge_id!r}] must be a number.")
        if not 0 <= share <= 1:
            raise ValueError(
                f"route_shares[{edge_id!r}] must be between 0 and 1 (got {share})."
            )
    # The shares must cover every item exactly once.
    total = sum(route_shares.values())
    if not math.isclose(total, 1.0, abs_tol=1e-6):
        raise ValueError(f"route_shares must add up to 1 (they add up to {total}).")
    return dict(route_shares)


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


def share_selector(
    machine: object, route_shares: dict[str, float]
) -> Callable[[object], int]:
    """Return the out-edge selector for a machine with a share rule.

    The selector draws one random number per item and returns the index, in
    `machine.out_edges`, of the edge whose share it falls into. It raises
    ValueError when that edge is not an out-edge of the machine.
    """
    edge_ids = list(route_shares)

    def select(item: object) -> int:
        # Pick a random number from 0 to 1 and see which share it lands in.
        # If rounding leaves a tiny gap at the end, the last edge takes it.
        draw = random.random()
        edge_id = edge_ids[-1]
        for candidate in edge_ids:
            draw -= route_shares[candidate]
            if draw < 0:
                edge_id = candidate
                break
        # FactorySimPy wants the edge's position in out_edges, not its id.
        for index, edge in enumerate(machine.out_edges):
            if edge.id == edge_id:
                return index
        # The share rule names an edge that does not leave this machine.
        raise ValueError(
            f"Machine '{machine.id}': route_shares target '{edge_id}' is not "
            "one of its out-edges."
        )

    return select
