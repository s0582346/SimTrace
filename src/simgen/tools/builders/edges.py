"""Builders for FactorySimPy *Edge* instances.

Each `create_*` function is a plain builder mirroring the node builders in
`simgen.tools.nodes`: it validates its arguments, instantiates the FactorySimPy
edge bound to the shared model environment, registers it, and returns a small
JSON-serializable summary. The MCP server wraps these as tools; tests call them
directly.

Edges *hold and move* flow items between exactly two nodes and do no work
themselves. They are wired to nodes later via `connect`.

Conventions (see architecture/edge_tools.md):
  - reject duplicate ids (ids are global across nodes and edges),
  - src_node/dest_node start None — wiring happens later via `connect`,
  - edges have no node_setup_time (that param is node-only),
  - v1 delay params accept constant int/float only.
"""

from __future__ import annotations

import math

from factorysimpy.edges.buffer import Buffer
from factorysimpy.edges.continuous_conveyor import ConveyorBelt
from factorysimpy.edges.fleet import Fleet

from simgen.model import FactoryModel, get_model
from simgen.tools.utils import (
    require_number,
    require_positive_int,
    require_positive_number,
    require_unique_id,
)


def create_buffer(
    id: str,
    capacity: int = 1,
    delay: float = 0,
    mode: str = "FIFO",
    *,
    model: FactoryModel | None = None,
) -> dict:
    """Create a Buffer edge and register it in the shared model.

    A Buffer is a FIFO/LIFO queue holding items that are waiting to be accepted
    by its destination node. It connects exactly one src node to one dest node
    (both wired later via connect).
    Args:
        id: unique edge identifier (unique across nodes and edges).
        capacity: max items the buffer can hold; must be a positive int.
        delay: constant time after which a put item becomes available to get.
        mode: "FIFO" (oldest item available first) or "LIFO" (newest first).

    Returns a summary dict echoing the stored parameters.
    """
    model = model if model is not None else get_model()

    require_unique_id(id, model)
    require_positive_int("capacity", capacity)
    require_number("delay", delay)
    if mode not in ("FIFO", "LIFO"):
        raise ValueError('mode must be "FIFO" or "LIFO".')

    edge = Buffer(env=model.env, id=id, capacity=capacity, delay=delay, mode=mode)

    model.add_edge(id, edge)

    return {
        "id": id,
        "type": "Buffer",
        "capacity": capacity,
        "delay": delay,
        "mode": mode,
        "src": None,
        "dest": None,
    }


def create_conveyor(
    id: str,
    conveyor_length: float,
    speed: float,
    item_length: float,
    accumulating: bool = False,
    *,
    model: FactoryModel | None = None,
) -> dict:
    """Create a ConveyorBelt edge and register it in the shared model.

    A conveyor moves items along a belt; travel time derives from
    length/speed. It connects exactly one src node to one dest node (both wired
    later via connect). Its capacity is *derived*, not set directly:
    capacity = int(ceil(conveyor_length) / item_length), and must be >= 1.
    Args:
        id: unique edge identifier (unique across nodes and edges).
        conveyor_length: physical length of the belt; must be > 0.
        speed: belt speed; must be > 0.
        item_length: length of each item on the belt; must be > 0 and should
            match the item_length of items the upstream Source emits.
        accumulating: if True, items bunch up when the belt stalls; if False,
            the belt blocks.

    Returns a summary dict echoing the stored parameters plus the derived
    capacity.
    """
    model = model if model is not None else get_model()

    require_unique_id(id, model)
    require_positive_number("conveyor_length", conveyor_length)
    require_positive_number("speed", speed)
    require_positive_number("item_length", item_length)
    if not isinstance(accumulating, bool):
        raise ValueError("accumulating must be a bool.")

    # how many items fit on the belt. FactorySimPy works it out from
    # length and item size. If an item is longer than the whole belt, this comes
    # out as 0, which FactorySimPy rejects, so we check for it below.
    capacity = int(math.ceil(conveyor_length) / item_length)
    if capacity < 1:
        raise ValueError(
            "derived conveyor capacity must be >= 1; item_length "
            f"({item_length}) is too large for conveyor_length "
            f"({conveyor_length})."
        )

    edge = ConveyorBelt(
        env=model.env,
        id=id,
        conveyor_length=conveyor_length,
        speed=speed,
        item_length=item_length,
        accumulating=accumulating,
    )

    model.add_edge(id, edge)

    return {
        "id": id,
        "type": "ConveyorBelt",
        "conveyor_length": conveyor_length,
        "speed": speed,
        "item_length": item_length,
        "accumulating": accumulating,
        "capacity": capacity,
        "src": None,
        "dest": None,
    }


def create_fleet(
    id: str,
    capacity: int = 1,
    delay: float = 1,
    transit_delay: float = 0,
    *,
    model: FactoryModel | None = None,
) -> dict:
    """Create a Fleet edge and register it in the shared model.

    A Fleet is a group of transporters (AGVs) that move up to `capacity` items
    at once between two nodes. It connects exactly one src node to one dest node
    (both wired later via connect).
    Args:
        id: unique edge identifier (unique across nodes and edges).
        capacity: number of items moved per trip; must be a positive int.
        delay: constant wait before the fleet departs if it hasn't filled to
            capacity.
        transit_delay: constant src->dest travel time.

    Returns a summary dict echoing the stored parameters.
    """
    model = model if model is not None else get_model()

    require_unique_id(id, model)
    require_positive_int("capacity", capacity)
    require_number("delay", delay)
    require_number("transit_delay", transit_delay)

    edge = Fleet(
        env=model.env,
        id=id,
        capacity=capacity,
        delay=delay,
        transit_delay=transit_delay,
    )

    model.add_edge(id, edge)

    return {
        "id": id,
        "type": "Fleet",
        "capacity": capacity,
        "delay": delay,
        "transit_delay": transit_delay,
        "src": None,
        "dest": None,
    }
