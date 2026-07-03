"""Central registration of MCP tools onto a FastMCP server.

Each tool here is a thin wrapper that delegates to a plain builder in
`simgen.tools.nodes`/`edges`/`simulation` (which own validation and the shared
model). Every wrapper is decorated with `traced`, so each tool call becomes an
OpenTelemetry span. As new tools land, register them in `register_tools`.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from simgen.tools import edges, nodes, simulation
from simgen.tools.telemetry import traced
from simgen.tools.validation import validation


def register_tools(mcp: FastMCP) -> FastMCP:
    """Register every block-building tool on `mcp` and return it."""

    @mcp.tool()
    @traced
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
            item_length: physical length assigned to each generated item;
                only used by conveyor edges (travel time = length/speed) and
                ignored by plain buffers.
            blocking: if True, wait for out_edge space; if False, discard when
                full.
            out_edge_selection: edge-selection strategy, one of
                "FIRST_AVAILABLE", "RANDOM", "ROUND_ROBIN".
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

    @mcp.tool()
    @traced
    def create_sink(
        id: str,
        node_setup_time: float = 0,
    ) -> dict:
        """Create a Sink node that collects flow items at the end of the line.

        A Sink is terminal: it has no out_edge and needs at least one in_edge
        (wired later via connect).

        Args:
            id: unique node identifier.
            node_setup_time: constant initial setup delay.
        """
        return nodes.create_sink(
            id=id,
            node_setup_time=node_setup_time,
        )

    @mcp.tool()
    @traced
    def create_machine(
        id: str,
        work_capacity: int = 1,
        processing_delay: float = 0,
        blocking: bool = True,
        in_edge_selection: str = "FIRST_AVAILABLE",
        out_edge_selection: str = "FIRST_AVAILABLE",
        node_setup_time: float = 0,
    ) -> dict:
        """Create a Machine node that processes flow items.

        A Machine needs at least one in_edge and one out_edge (both wired later
        via connect).

        Args:
            id: unique node identifier.
            work_capacity: number of items processed simultaneously (one worker
                thread per item); must be a positive int.
            processing_delay: constant time to process one item.
            blocking: if True, wait for out_edge space; if False, discard when
                full.
            in_edge_selection: in-edge selection strategy, one of
                "FIRST_AVAILABLE", "RANDOM", "ROUND_ROBIN".
            out_edge_selection: out-edge selection strategy, one of
                "FIRST_AVAILABLE", "RANDOM", "ROUND_ROBIN".
            node_setup_time: constant initial setup delay.
        """
        return nodes.create_machine(
            id=id,
            work_capacity=work_capacity,
            processing_delay=processing_delay,
            blocking=blocking,
            in_edge_selection=in_edge_selection,
            out_edge_selection=out_edge_selection,
            node_setup_time=node_setup_time,
        )

    @mcp.tool()
    @traced
    def create_splitter(
        id: str,
        mode: str = "UNPACK",
        split_quantity: int | None = None,
        processing_delay: float = 0,
        blocking: bool = True,
        in_edge_selection: str = "FIRST_AVAILABLE",
        out_edge_selection: str = "FIRST_AVAILABLE",
        node_setup_time: float = 0,
    ) -> dict:
        """Create a Splitter node that emits several items from one incoming item.

        A Splitter needs at least one in_edge and one out_edge (both wired later
        via connect).

        Args:
            id: unique node identifier.
            mode: "UNPACK" (emit each item inside a packed item, then the empty
                container) or "SPLIT" (split into `split_quantity` items).
            split_quantity: number of items to split into; required when
                mode="SPLIT", ignored when mode="UNPACK". Must be a positive int.
            processing_delay: constant time to process one item.
            blocking: if True, wait for out_edge space; if False, discard when
                full.
            in_edge_selection: in-edge selection strategy, one of
                "FIRST_AVAILABLE", "RANDOM", "ROUND_ROBIN".
            out_edge_selection: out-edge selection strategy, one of
                "FIRST_AVAILABLE", "RANDOM", "ROUND_ROBIN".
            node_setup_time: constant initial setup delay.
        """
        return nodes.create_splitter(
            id=id,
            mode=mode,
            split_quantity=split_quantity,
            processing_delay=processing_delay,
            blocking=blocking,
            in_edge_selection=in_edge_selection,
            out_edge_selection=out_edge_selection,
            node_setup_time=node_setup_time,
        )

    @mcp.tool()
    @traced
    def create_combiner(
        id: str,
        target_quantity_of_each_item: list[int] | None = None,
        processing_delay: float = 0,
        blocking: bool = True,
        out_edge_selection: str = "FIRST_AVAILABLE",
        node_setup_time: float = 0,
    ) -> dict:
        """Create a Combiner node that packs items from several in_edges into one.

        A Combiner pulls a container (pallet) from its first in_edge, packs
        items from the other in_edges into it, and emits the packed container.
        It needs at least one in_edge and one out_edge (wired later via
        connect) and has no in_edge_selection.

        Args:
            id: unique node identifier.
            target_quantity_of_each_item: per-in_edge counts — how many items to
                pull from each in_edge per combine cycle (indexed by in_edge
                position; the first position is the pallet edge). Each entry must
                be a positive int. Defaults to [1].
            processing_delay: constant time to process one combine cycle.
            blocking: if True, wait for out_edge space; if False, discard when
                full.
            out_edge_selection: out-edge selection strategy, one of
                "FIRST_AVAILABLE", "RANDOM", "ROUND_ROBIN".
            node_setup_time: constant initial setup delay.
        """
        return nodes.create_combiner(
            id=id,
            target_quantity_of_each_item=target_quantity_of_each_item,
            processing_delay=processing_delay,
            blocking=blocking,
            out_edge_selection=out_edge_selection,
            node_setup_time=node_setup_time,
        )

    @mcp.tool()
    @traced
    def create_buffer(
        id: str,
        capacity: int = 1,
        delay: float = 0,
        mode: str = "FIFO",
    ) -> dict:
        """Create a Buffer edge: a FIFO/LIFO queue between two nodes.

        A Buffer holds items waiting to be accepted by its destination node. It
        connects exactly one src node to one dest node (both wired later via
        connect).

        Args:
            id: unique edge identifier (unique across nodes and edges).
            capacity: max items the buffer can hold; must be a positive int.
            delay: constant time after which a put item becomes available to get.
            mode: "FIFO" (oldest item available first) or "LIFO" (newest first).
        """
        return edges.create_buffer(
            id=id,
            capacity=capacity,
            delay=delay,
            mode=mode,
        )

    @mcp.tool()
    @traced
    def create_conveyor(
        id: str,
        conveyor_length: float,
        speed: float,
        item_length: float,
        accumulating: bool = False,
    ) -> dict:
        """Create a ConveyorBelt edge that moves items along a belt.

        Travel time derives from length/speed. It connects exactly one src node
        to one dest node (both wired later via connect). The belt's capacity is
        derived (int(ceil(conveyor_length) / item_length)) and returned in the
        summary; it must come out >= 1.

        Args:
            id: unique edge identifier (unique across nodes and edges).
            conveyor_length: physical length of the belt; must be > 0.
            speed: belt speed; must be > 0.
            item_length: length of each item on the belt; must be > 0 and should
                match the item_length of items the upstream Source emits.
            accumulating: if True, items bunch up when the belt stalls; if False,
                the belt blocks.
        """
        return edges.create_conveyor(
            id=id,
            conveyor_length=conveyor_length,
            speed=speed,
            item_length=item_length,
            accumulating=accumulating,
        )

    @mcp.tool()
    @traced
    def create_fleet(
        id: str,
        capacity: int = 1,
        delay: float = 1,
        transit_delay: float = 0,
    ) -> dict:
        """Create a Fleet edge: transporters (AGVs) moving items between nodes.

        A Fleet moves up to `capacity` items at once. It connects exactly one
        src node to one dest node (both wired later via connect).

        Args:
            id: unique edge identifier (unique across nodes and edges).
            capacity: number of items moved per trip; must be a positive int.
            delay: constant wait before the fleet departs if it hasn't filled to
                capacity.
            transit_delay: constant src->dest travel time.
        """
        return edges.create_fleet(
            id=id,
            capacity=capacity,
            delay=delay,
            transit_delay=transit_delay,
        )

    @mcp.tool()
    @traced
    def connect(edge_id: str, src_id: str, dest_id: str) -> dict:
        """Wire a single edge between two nodes.

        Appends the edge to the source node's out_edges and the destination
        node's in_edges. An edge connects exactly one src to one dest; a node
        accumulates several in/out edges by being the endpoint of several
        connect calls.

        Args:
            edge_id: id of an existing edge (Buffer/Conveyor/Fleet).
            src_id: id of the source (upstream) node.
            dest_id: id of the destination (downstream) node.
        """
        return simulation.connect(edge_id=edge_id, src_id=src_id, dest_id=dest_id)

    @mcp.tool()
    @traced
    def get_model() -> dict:
        """Return a snapshot of the current graph.

        Lists every node (with its in/out edge counts) and every edge (with its
        resolved src/dest node ids). Read-only.
        """
        return simulation.get_model()

    @mcp.tool()
    @traced
    def reset_model() -> dict:
        """Discard the current graph and start a fresh, empty model.

        Drops every node and edge and restarts the simulation clock at 0. Use it
        to recover from a dirty session: leftover components from earlier work,
        an orphaned node that can't be wired, or a clock that has already
        advanced past the `until` you want to run to. Returns the counts cleared.
        """
        return simulation.reset_model()

    @mcp.tool()
    @traced
    def run_simulation(until: float) -> dict:
        """Run the simulation up to time `until` and return a stats summary.

        This executes the model: any unmet edge-cardinality requirement (e.g. a
        Source with no out_edge) surfaces here. Returns per-node/edge stats.

        Args:
            until: simulation end time; must be a positive number.
        """
        return simulation.run_simulation(until=until)

    @mcp.tool()
    @traced
    def verify_conservation() -> dict:
        """Reconcile every generated item against where it ended up.

        Mass-balances the last run: items all Sources generated must equal items
        received by Sinks + items still in edges (WIP) + items still in machines
        + items discarded. A mismatch means items were lost or created — the
        alarm to raise. Requires a prior run_simulation. Exact for lines without
        splitters/combiners; those change the physical item count, so the check
        reports `balanced: null` / `exact: false` and names them in
        `packing_nodes`.
        """
        return validation.verify_conservation()

    @mcp.tool()
    @traced
    def verify_item_flow() -> dict:
        """Check that every delivered item followed a proper route.

        For each item that reached a sink in the last run, replays its captured
        node path and checks it is a connected route along wired edges ending at a
        sink. Clean items are counted in `passed`; an item that took a hop with no
        wired edge behind it is listed in `improper` with the offending hop. Only delivered items are judged (stuck/discarded items are
        verify_conservation's concern). Requires a prior run_simulation.
        """
        return validation.verify_item_flow()

    return mcp
