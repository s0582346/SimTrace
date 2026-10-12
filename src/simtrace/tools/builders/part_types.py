"""Part types: a Source stamps its type and attributes onto every item.

`create_source(part_type=..., attributes=...)` gives a Source a type name and
an attribute set. Every item the Source releases then carries
`{"part_type": <name>, <attribute>: <value>, ...}` in its `payload`.

FactorySimPy creates items inside `Source.behaviour` and hands each one to the
out-edge with `edge.put(event, item)`. `tag_source_edge` replaces `put` on that
one edge instance, so the stamp is set before the item enters the line. The
vendored FactorySimPy code is not changed.

Attributes only describe the item. Routing, priority and processing times stay
encoded in the graph.
"""

from __future__ import annotations

from typing import Union

from simtrace.model import FactoryModel
from simtrace.tools.distributions import is_distribution_spec

AttributeValue = Union[str, int, float, bool]

_RESERVED = "part_type"


def resolve_part_type(
    part_type: str | None, attributes: dict[str, AttributeValue] | None
) -> dict[str, AttributeValue]:
    """Validate a Source's part type and attributes; return a copy of them.

    Attribute values are fixed: a string, number or bool, the same for every
    item. A distribution string is rejected.
    """
    if part_type is None:
        if attributes:
            raise ValueError("attributes need a part_type.")
        return {}
    if not isinstance(part_type, str) or not part_type:
        raise ValueError("part_type must be a non-empty string.")
    if attributes is None:
        return {}
    if not isinstance(attributes, dict):
        raise ValueError("attributes must be a dict of name -> value.")

    for key, value in attributes.items():
        if not isinstance(key, str) or not key:
            raise ValueError("attribute names must be non-empty strings.")
        if key == _RESERVED:
            raise ValueError(f'"{_RESERVED}" is reserved and cannot be an attribute.')
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError(
                f"attributes.{key} must be a string, number or bool; got {value!r}."
            )
        if is_distribution_spec(value):
            raise ValueError(
                f"attributes.{key} must be a fixed value; distributions such as "
                f"{value!r} are not supported."
            )
    return dict(attributes)


def make_payload(source: object) -> dict:
    """Build one item's payload: the type name plus the Source's attributes."""
    return {_RESERVED: source.part_type, **source.part_attributes}


def tag_source_edge(model: FactoryModel, edge: object, source: object) -> None:
    """Stamp every item put into `edge` with a fresh payload from `source`.

    Called by `connect` for each out-edge of a Source that has a part type.
    Only items with an empty payload are stamped. Each stamped payload is also
    stored in `model.item_attributes` under the item id.
    """
    put = edge.put

    def tagged_put(event, item):
        if item.payload is None:
            item.payload = make_payload(source)
            model.item_attributes[item.id] = item.payload
        return put(event, item)

    edge.put = tagged_put


def part_type_names(model: FactoryModel) -> list[str]:
    """Part type names of the model's Sources, in creation order, no repeats."""
    names = []
    for node in model.nodes.values():
        name = getattr(node, "part_type", None)
        if name is not None and name not in names:
            names.append(name)
    return names


def part_type_report(model: FactoryModel) -> dict:
    """Count generated, completed and scrapped items per part type.

    Generated items are those stamped by their Source in the latest run. An
    item is completed when its trail in `model.item_paths` ends at a Sink, and
    scrapped when that Sink was created with scrap=True. For every numeric
    attribute the report sums the values over the completed items only.
    Sources that share a part type name are counted together.
    """
    from factorysimpy.nodes.sink import Sink

    report = {
        name: {
            "generated": 0,
            "completed": 0,
            "scrapped": 0,
            "completed_attribute_sums": {},
        }
        for name in part_type_names(model)
    }
    for item_id, payload in model.item_attributes.items():
        entry = report[payload[_RESERVED]]
        entry["generated"] += 1

        # Items still in the line have not reached a Sink yet.
        trail = model.item_paths.get(item_id)
        end = model.nodes.get(trail[-1]) if trail else None
        if not isinstance(end, Sink):
            continue
        # A scrap Sink counts the item but adds nothing to the sums.
        if getattr(end, "scrap", False):
            entry["scrapped"] += 1
            continue
        entry["completed"] += 1
        sums = entry["completed_attribute_sums"]
        for key, value in payload.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            sums[key] = sums.get(key, 0) + value

    return report
