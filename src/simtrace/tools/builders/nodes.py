"""Builders for FactorySimPy *Node* instances (the active components).

Each `create_*` function is a plain, transport-agnostic builder: it validates
its arguments, instantiates the FactorySimPy node bound to the shared model
environment, registers it, and returns a small JSON-serializable summary.
The MCP server wraps these as tools; tests call them directly.

Conventions (see architecture/node_tools.md):
  - reject duplicate ids,
  - always bind to `model.env`, never leak env/simpy objects in results,
  - in_edges/out_edges start empty — wiring happens later via `connect`,
  - delay params (inter_arrival_time, processing_delay) accept a constant
    int/float OR a distribution string ("uniform(a, b)", "normal(m, s)",
    "gauss(m, s)", "exp(x)"); node_setup_time is constant-only,
  - selection params accept a strategy string only.
"""

from __future__ import annotations

from factorysimpy.nodes.combiner import Combiner
from factorysimpy.nodes.machine import Machine
from factorysimpy.nodes.sink import Sink
from factorysimpy.nodes.source import Source
from factorysimpy.nodes.splitter import Splitter

from simtrace.model import FactoryModel, get_model
from simtrace.tools.builders.calendars import resolve_calendar
from simtrace.tools.builders.part_types import resolve_part_type
from simtrace.tools.builders.routing import (
    resolve_routes,
    resolve_shares,
    route_selector,
    share_selector,
)
from simtrace.tools.utils import require_number, resolve_delay


def create_source(
    id: str,
    inter_arrival_time: float | str = 1.0,
    flow_item_type: str = "item",
    item_length: float = 1,
    blocking: bool = False,
    out_edge_selection: str = "FIRST_AVAILABLE",
    node_setup_time: float = 0,
    part_type: str | None = None,
    attributes: dict[str, str | int | float | bool] | None = None,
    calendar: str | None = None,
    *,
    model: FactoryModel | None = None,
) -> dict:
    """Create a Source node and register it in the shared model.

    A Source generates flow items and has no in_edges and exactly one out_edge
    (wired later). A product mix is modeled as one Source per type, with the
    overall mean inter-arrival time divided by the type's mix fraction. With
    `part_type` set, every item the Source releases carries the type name and
    `attributes` in its payload; routing still follows the graph only.
    Args:
        id: unique node identifier.
        inter_arrival_time: time between item generations. Either a constant
            (int/float) or a distribution string sampled each cycle:
            "uniform(a, b)", "normal(m, s)", "gauss(m, s)", "exp(x)" (exp mean
            = x). Samples are clamped to >= 0. Must be non-zero when
            `blocking=False`.
        flow_item_type: "item" or "pallet".
        item_length: physical length assigned to each generated item; only
            used by conveyor edges (travel time = length/speed) and ignored by
            plain buffers.
        blocking: if True, wait for out_edge space; if False, discard when full.
        out_edge_selection: edge-selection strategy, one of "FIRST_AVAILABLE",
            "RANDOM", "ROUND_ROBIN".
        node_setup_time: constant initial setup delay.
        part_type: type name stamped on every item, or None for untyped
            items. Sources may share a name; the report counts them together.
        attributes: needs a part_type. Attribute name -> fixed value (a
            string, number or bool), stamped unchanged on every item. The name
            "part_type" is reserved.
        calendar: id of a shift calendar. None means the model's default
            calendar if there is one, else arrivals at any time. With a calendar, inter_arrival_time counts working time only, so
            items arrive only inside a window. The calendar starts after the
            setup.

    Returns a summary dict echoing the stored parameters.
    """
    model = model if model is not None else get_model()

    if not isinstance(id, str) or not id:
        raise ValueError("id must be a non-empty string.")
    if model.has_node(id):
        raise ValueError(f"A node with id '{id}' already exists.")
    resolved_attributes = resolve_part_type(part_type, attributes)
    resolved_calendar = resolve_calendar(model, calendar)

    resolved_iat = resolve_delay("inter_arrival_time", inter_arrival_time)
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
        inter_arrival_time=resolved_iat,
        flow_item_type=flow_item_type,
        item_length=item_length,
        blocking=blocking,
        out_edge_selection=out_edge_selection,
        calendar=resolved_calendar,
    )
    # Source.__init__ doesn't forward node_setup_time to the base Node, so set it
    # explicitly (validated above).
    node.node_setup_time = node_setup_time
    # Read by connect, which stamps the items on this Source's out-edges.
    node.part_type = part_type
    node.part_attributes = resolved_attributes
    # The calendar named here; None lets a later default calendar apply.
    node.calendar_id = calendar

    model.add_node(id, node)
    model.record(
        "create_source",
        {
            "id": id,
            "inter_arrival_time": inter_arrival_time,
            "flow_item_type": flow_item_type,
            "item_length": item_length,
            "blocking": blocking,
            "out_edge_selection": out_edge_selection,
            "node_setup_time": node_setup_time,
            "part_type": part_type,
            "attributes": attributes,
            "calendar": calendar,
        },
    )

    return {
        "id": id,
        "type": "Source",
        "inter_arrival_time": inter_arrival_time,
        "flow_item_type": flow_item_type,
        "item_length": item_length,
        "blocking": blocking,
        "out_edge_selection": out_edge_selection,
        "node_setup_time": node_setup_time,
        "part_type": part_type,
        "attributes": attributes,
        "calendar": calendar if calendar is not None else model.default_calendar,
        "in_edges": 0,
        "out_edges": 0,
    }


def create_sink(
    id: str,
    node_setup_time: float = 0,
    scrap: bool = False,
    *,
    model: FactoryModel | None = None,
) -> dict:
    """Create a Sink node and register it in the shared model.

    A Sink is a terminal node that collects flow items at the end of the line.
    It has no out_edge and needs at least one in_edge (wired later via connect).
    Args:
        id: unique node identifier.
        node_setup_time: constant initial setup delay.
        scrap: if True, items arriving here count as scrapped, not completed,
            in the per-type report of run_simulation.

    Returns a summary dict echoing the stored parameters.
    """
    model = model if model is not None else get_model()

    if not isinstance(id, str) or not id:
        raise ValueError("id must be a non-empty string.")
    if model.has_node(id):
        raise ValueError(f"A node with id '{id}' already exists.")

    require_number("node_setup_time", node_setup_time)
    if not isinstance(scrap, bool):
        raise ValueError("scrap must be a bool.")

    node = Sink(env=model.env, id=id, node_setup_time=node_setup_time)
    # Read by the per-type report.
    node.scrap = scrap

    model.add_node(id, node)
    model.record(
        "create_sink", {"id": id, "node_setup_time": node_setup_time, "scrap": scrap}
    )

    return {
        "id": id,
        "type": "Sink",
        "node_setup_time": node_setup_time,
        "scrap": scrap,
        "in_edges": 0,
        "out_edges": 0,
    }


def _resolve_breakdowns(
    time_to_failure: float | str | None, repair_time: float | str | None
) -> tuple:
    """Validate the breakdown pair and resolve each to a number or sampler.

    Returns (None, None) when neither is given. A constant time_to_failure
    must be > 0 and a constant repair_time >= 0.
    """
    if time_to_failure is None and repair_time is None:
        return None, None
    if time_to_failure is None or repair_time is None:
        raise ValueError("time_to_failure and repair_time must be given together.")
    resolved_failure = resolve_delay("time_to_failure", time_to_failure)
    resolved_repair = resolve_delay("repair_time", repair_time)
    if isinstance(resolved_failure, (int, float)) and resolved_failure <= 0:
        raise ValueError(f"time_to_failure must be > 0 (got {time_to_failure!r}).")
    if isinstance(resolved_repair, (int, float)) and resolved_repair < 0:
        raise ValueError(f"repair_time must be >= 0 (got {repair_time!r}).")
    return resolved_failure, resolved_repair


def create_machine(
    id: str,
    work_capacity: int = 1,
    processing_delay: float | str = 0,
    blocking: bool = True,
    in_edge_selection: str = "FIRST_AVAILABLE",
    out_edge_selection: str = "FIRST_AVAILABLE",
    node_setup_time: float = 0,
    route_by: str | None = None,
    routes: dict[str, str | list[str]] | None = None,
    route_shares: dict[str, float] | None = None,
    time_to_failure: float | str | None = None,
    repair_time: float | str | None = None,
    calendar: str | None = None,
    *,
    model: FactoryModel | None = None,
) -> dict:
    """Create a Machine node and register it in the shared model.

    A Machine processes items. It needs at least one in_edge and one out_edge
    (both wired later via connect). With `route_by` set, the machine sends each
    item to the out-edge that `routes` gives for the item's attribute value,
    or to the first free one of the out-edges it lists.
    With `route_shares` set, chance picks each item's out-edge, weighted by
    the shares.
    Args:
        id: unique node identifier.
        work_capacity: number of items that can be processed simultaneously
            (one worker thread per item); must be a positive int.
        processing_delay: time to process one item. Constant (int/float) or a
            distribution string sampled each cycle: "uniform(a, b)",
            "normal(m, s)", "gauss(m, s)", "exp(x)" (exp mean = x). Samples are
            clamped to >= 0.
        blocking: if True, wait for out_edge space; if False, discard when full.
        in_edge_selection: in-edge selection strategy, one of "FIRST_AVAILABLE",
            "RANDOM", "ROUND_ROBIN".
        out_edge_selection: out-edge selection strategy, one of
            "FIRST_AVAILABLE", "RANDOM", "ROUND_ROBIN".
        node_setup_time: constant initial setup delay.
        route_by: needs routes. Name of the item attribute that decides the
            out-edge, e.g. "family" or "part_type".
        routes: needs route_by. Attribute value -> id of one of this machine's
            out-edges, or a list of such ids. With a list, the machine waits
            on all of them and takes the first that has space; the earlier
            one in the list wins a tie. A list needs blocking=True. The key
            "*" catches every other value. Only string values match. Leave
            out_edge_selection at its default; routes takes over that job.
        route_shares: out-edge id -> share of items sent there; the shares
            add up to 1. Cannot be combined with route_by. Leave
            out_edge_selection at its default; route_shares takes over that
            job.
        time_to_failure: needs repair_time. Time from the end of one repair
            (or of the setup) to the next breakdown, on the simulation clock,
            whatever the machine is doing. Constant (> 0) or a distribution
            string, e.g. "exp(480)" for an MTBF of 480. None means the machine
            never breaks down. A breakdown stops the whole machine: items
            being processed finish their remaining time after the repair, no
            item is pulled in, and a processed item waiting for space may
            still leave.
        repair_time: needs time_to_failure. Time one breakdown lasts.
            Constant (>= 0) or a distribution string, e.g. "exp(30)" for an
            MTTR of 30.
        calendar: id of a shift calendar. None means the model's default
            calendar if there is one, else the machine works all the time.
            Outside a window the machine starts no new item, and
            items being processed pause and finish their remaining time in the
            next window. With a calendar, time_to_failure and repair_time count
            working time only. The calendar starts after the setup.

    Returns a summary dict echoing the stored parameters.
    """
    model = model if model is not None else get_model()

    if not isinstance(id, str) or not id:
        raise ValueError("id must be a non-empty string.")
    if model.has_node(id):
        raise ValueError(f"A node with id '{id}' already exists.")

    # bool is an int subclass; exclude it so True/False aren't accepted as a count.
    if isinstance(work_capacity, bool) or not isinstance(work_capacity, int):
        raise ValueError(f"work_capacity must be an int (got {work_capacity!r}).")
    if work_capacity < 1:
        raise ValueError("work_capacity must be a positive int (>= 1).")

    resolved_delay = resolve_delay("processing_delay", processing_delay)
    require_number("node_setup_time", node_setup_time)
    if not isinstance(blocking, bool):
        raise ValueError("blocking must be a bool.")
    if not isinstance(in_edge_selection, str):
        raise ValueError("in_edge_selection must be a string (v1).")
    if not isinstance(out_edge_selection, str):
        raise ValueError("out_edge_selection must be a string (v1).")
    resolved_routes = resolve_routes(
        route_by, routes, out_edge_selection, blocking
    )
    resolved_shares = resolve_shares(route_shares, route_by, out_edge_selection)
    resolved_failure, resolved_repair = _resolve_breakdowns(
        time_to_failure, repair_time
    )
    resolved_calendar = resolve_calendar(model, calendar)

    node = Machine(
        env=model.env,
        id=id,
        node_setup_time=node_setup_time,
        work_capacity=work_capacity,
        processing_delay=resolved_delay,
        blocking=blocking,
        in_edge_selection=in_edge_selection,
        out_edge_selection=out_edge_selection,
        time_to_failure=resolved_failure,
        repair_time=resolved_repair,
        calendar=resolved_calendar,
    )
    # The rule as given; the selector below is what the Machine calls.
    node.route_by = route_by
    node.routes = resolved_routes
    node.route_shares = resolved_shares
    # The calendar named here; None lets a later default calendar apply.
    node.calendar_id = calendar
    if resolved_routes is not None:
        node.out_edge_selection = route_selector(node, route_by, resolved_routes)
    if resolved_shares is not None:
        node.out_edge_selection = share_selector(node, resolved_shares)

    model.add_node(id, node)
    model.record(
        "create_machine",
        {
            "id": id,
            "work_capacity": work_capacity,
            "processing_delay": processing_delay,
            "blocking": blocking,
            "in_edge_selection": in_edge_selection,
            "out_edge_selection": out_edge_selection,
            "node_setup_time": node_setup_time,
            "route_by": route_by,
            "routes": routes,
            "route_shares": route_shares,
            "time_to_failure": time_to_failure,
            "repair_time": repair_time,
            "calendar": calendar,
        },
    )

    return {
        "id": id,
        "type": "Machine",
        "work_capacity": work_capacity,
        "processing_delay": processing_delay,
        "blocking": blocking,
        "in_edge_selection": in_edge_selection,
        "out_edge_selection": out_edge_selection,
        "node_setup_time": node_setup_time,
        "route_by": route_by,
        "routes": routes,
        "route_shares": route_shares,
        "time_to_failure": time_to_failure,
        "repair_time": repair_time,
        "calendar": calendar if calendar is not None else model.default_calendar,
        "in_edges": 0,
        "out_edges": 0,
    }


def create_splitter(
    id: str,
    mode: str = "UNPACK",
    split_quantity: int | None = None,
    processing_delay: float | str = 0,
    blocking: bool = True,
    in_edge_selection: str = "FIRST_AVAILABLE",
    out_edge_selection: str = "FIRST_AVAILABLE",
    node_setup_time: float = 0,
    pallet_edge: str | None = None,
    *,
    model: FactoryModel | None = None,
) -> dict:
    """Create a Splitter node and register it in the shared model.

    A Splitter takes one incoming item and emits several, one per out-edge
    dispatch. It needs at least one in_edge and one out_edge (wired later via
    connect).
    Args:
        id: unique node identifier.
        mode: "UNPACK" (emit each item inside a packed item, then the empty
            container) or "SPLIT" (split the item into `split_quantity` items).
        split_quantity: number of items to split into; required when
            mode="SPLIT", ignored when mode="UNPACK". Must be a positive int.
        processing_delay: time to process one item. Constant (int/float) or a
            distribution string sampled each cycle: "uniform(a, b)",
            "normal(m, s)", "gauss(m, s)", "exp(x)" (exp mean = x). Samples are
            clamped to >= 0.
        blocking: if True, wait for out_edge space; if False, discard when full.
        in_edge_selection: in-edge selection strategy, one of "FIRST_AVAILABLE",
            "RANDOM", "ROUND_ROBIN".
        out_edge_selection: out-edge selection strategy, one of
            "FIRST_AVAILABLE", "RANDOM", "ROUND_ROBIN".
        node_setup_time: constant initial setup delay.
        pallet_edge: id of the out-edge the empty pallet leaves by, e.g. the
            buffer to a pallet return. mode="UNPACK" and
            out_edge_selection="FIRST_AVAILABLE" only. The unpacked items then
            use the other out-edges. None sends the pallet like any item, to
            the first out-edge with space. The edge must be connected to this
            splitter before the run.

    Returns a summary dict echoing the stored parameters.
    """
    model = model if model is not None else get_model()

    if not isinstance(id, str) or not id:
        raise ValueError("id must be a non-empty string.")
    if model.has_node(id):
        raise ValueError(f"A node with id '{id}' already exists.")

    if mode not in ("UNPACK", "SPLIT"):
        raise ValueError('mode must be "UNPACK" or "SPLIT".')
    if mode == "SPLIT":
        if split_quantity is None:
            raise ValueError('split_quantity is required when mode="SPLIT".')
        # bool is an int subclass; exclude it so True/False aren't a count.
        if isinstance(split_quantity, bool) or not isinstance(split_quantity, int):
            raise ValueError(
                f"split_quantity must be an int (got {split_quantity!r})."
            )
        if split_quantity < 1:
            raise ValueError("split_quantity must be a positive int (>= 1).")

    resolved_delay = resolve_delay("processing_delay", processing_delay)
    require_number("node_setup_time", node_setup_time)
    if not isinstance(blocking, bool):
        raise ValueError("blocking must be a bool.")
    if not isinstance(in_edge_selection, str):
        raise ValueError("in_edge_selection must be a string (v1).")
    if not isinstance(out_edge_selection, str):
        raise ValueError("out_edge_selection must be a string (v1).")
    if pallet_edge is not None:
        if not isinstance(pallet_edge, str) or not pallet_edge:
            raise ValueError("pallet_edge must be an edge id or None.")
        if mode != "UNPACK":
            raise ValueError('pallet_edge needs mode="UNPACK".')
        if out_edge_selection != "FIRST_AVAILABLE":
            raise ValueError('pallet_edge needs out_edge_selection="FIRST_AVAILABLE".')

    node = Splitter(
        env=model.env,
        id=id,
        node_setup_time=node_setup_time,
        processing_delay=resolved_delay,
        blocking=blocking,
        mode=mode,
        split_quantity=split_quantity,
        in_edge_selection=in_edge_selection,
        out_edge_selection=out_edge_selection,
        pallet_edge=pallet_edge,
    )

    model.add_node(id, node)
    model.record(
        "create_splitter",
        {
            "id": id,
            "mode": mode,
            "split_quantity": split_quantity,
            "processing_delay": processing_delay,
            "blocking": blocking,
            "in_edge_selection": in_edge_selection,
            "out_edge_selection": out_edge_selection,
            "node_setup_time": node_setup_time,
            "pallet_edge": pallet_edge,
        },
    )

    return {
        "id": id,
        "type": "Splitter",
        "mode": mode,
        "split_quantity": split_quantity,
        "processing_delay": processing_delay,
        "blocking": blocking,
        "in_edge_selection": in_edge_selection,
        "out_edge_selection": out_edge_selection,
        "node_setup_time": node_setup_time,
        "pallet_edge": pallet_edge,
        "in_edges": 0,
        "out_edges": 0,
    }


def create_combiner(
    id: str,
    target_quantity_of_each_item: list[int] | None = None,
    processing_delay: float | str = 0,
    blocking: bool = True,
    out_edge_selection: str = "FIRST_AVAILABLE",
    node_setup_time: float = 0,
    *,
    model: FactoryModel | None = None,
) -> dict:
    """Create a Combiner node and register it in the shared model.

    A Combiner packs items from several in_edges into a container (a pallet
    pulled from the first in_edge) and emits the packed container. It needs at
    least one in_edge and one out_edge (wired later via connect). Note: it has
    no in_edge_selection — it draws from all in_edges by design.
    Args:
        id: unique node identifier.
        target_quantity_of_each_item: per-in_edge counts — how many items to
            pull from each in_edge per combine cycle (indexed by in_edge
            position; the first position is the pallet edge). Each entry must be
            a positive int. Defaults to [1].
        processing_delay: time to process one combine cycle. Constant
            (int/float) or a distribution string sampled each cycle:
            "uniform(a, b)", "normal(m, s)", "gauss(m, s)", "exp(x)" (exp mean
            = x). Samples are clamped to >= 0.
        blocking: if True, wait for out_edge space; if False, discard when full.
        out_edge_selection: out-edge selection strategy, one of
            "FIRST_AVAILABLE", "RANDOM", "ROUND_ROBIN".
        node_setup_time: constant initial setup delay.

    Returns a summary dict echoing the stored parameters.
    """
    model = model if model is not None else get_model()

    if not isinstance(id, str) or not id:
        raise ValueError("id must be a non-empty string.")
    if model.has_node(id):
        raise ValueError(f"A node with id '{id}' already exists.")

    # Avoid a mutable default argument; fall back to FactorySimPy's default.
    if target_quantity_of_each_item is None:
        target_quantity_of_each_item = [1]
    if (
        not isinstance(target_quantity_of_each_item, list)
        or not target_quantity_of_each_item
    ):
        raise ValueError(
            "target_quantity_of_each_item must be a non-empty list of ints."
        )
    for qty in target_quantity_of_each_item:
        # bool is an int subclass; exclude it so True/False aren't a count.
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
            raise ValueError(
                "target_quantity_of_each_item entries must be positive ints "
                f"(got {qty!r})."
            )

    resolved_delay = resolve_delay("processing_delay", processing_delay)
    require_number("node_setup_time", node_setup_time)
    if not isinstance(blocking, bool):
        raise ValueError("blocking must be a bool.")
    if not isinstance(out_edge_selection, str):
        raise ValueError("out_edge_selection must be a string (v1).")

    node = Combiner(
        env=model.env,
        id=id,
        node_setup_time=node_setup_time,
        target_quantity_of_each_item=target_quantity_of_each_item,
        processing_delay=resolved_delay,
        blocking=blocking,
        out_edge_selection=out_edge_selection,
    )

    model.add_node(id, node)
    model.record(
        "create_combiner",
        {
            "id": id,
            # Copy so a later mutation of the caller's list can't rewrite history.
            "target_quantity_of_each_item": list(target_quantity_of_each_item),
            "processing_delay": processing_delay,
            "blocking": blocking,
            "out_edge_selection": out_edge_selection,
            "node_setup_time": node_setup_time,
        },
    )

    return {
        "id": id,
        "type": "Combiner",
        "target_quantity_of_each_item": target_quantity_of_each_item,
        "processing_delay": processing_delay,
        "blocking": blocking,
        "out_edge_selection": out_edge_selection,
        "node_setup_time": node_setup_time,
        "in_edges": 0,
        "out_edges": 0,
    }
