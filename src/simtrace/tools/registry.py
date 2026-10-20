"""Central registration of MCP tools onto a FastMCP server.

Each tool here is a thin wrapper that delegates to a plain function in
`simtrace.tools.builders` (create_*), `simtrace.tools.simulation` (connect,
get_model, reset_model, run_simulation, run_replications), or
`simtrace.tools.validation` (verify_*) — those modules own the argument
validation and the shared model.
Every wrapper is decorated with `traced`, so each tool call becomes an
OpenTelemetry span. As new tools land, register them in `register_tools`.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from simtrace.tools import builders, simulation, validation
from simtrace.tools.telemetry import traced


def register_tools(mcp: FastMCP) -> FastMCP:
    """Register every block-building tool on `mcp` and return it."""

    @mcp.tool()
    @traced
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
    ) -> dict:
        """Create a Source node that generates flow items into the model.

        A Source has no in_edges and exactly one out_edge (wired later via
        connect). To model a product mix, create one Source per type and
        divide the overall mean inter-arrival time by the type's mix fraction
        (e.g. arrivals every 8 min with a 60/40 mix -> two Sources with
        "exp(13.33)" and "exp(20)"). Give each Source a part_type to tag its items.

        Args:
            id: unique node identifier.
            inter_arrival_time: time between item generations. Constant
                (int/float) or a distribution string sampled each cycle:
                "uniform(a, b)", "normal(m, s)", "gauss(m, s)", "exp(x)" (exp
                mean = x); samples clamped to >= 0. Must be non-zero when
                blocking is False.
            flow_item_type: "item" or "pallet".
            item_length: physical length assigned to each generated item;
                only used by conveyor edges (travel time = length/speed) and
                ignored by plain buffers.
            blocking: if True, wait for out_edge space; if False, discard when
                full.
            out_edge_selection: edge-selection strategy, one of
                "FIRST_AVAILABLE", "RANDOM", "ROUND_ROBIN".
            node_setup_time: constant initial setup delay.
            part_type: type name stamped on every item this Source
                releases. run_simulation then reports, per type, the generated
                and completed item counts and the sum of every numeric
                attribute over the completed items. Routing still follows the
                graph only.
            attributes: needs a part_type. A dict of extra properties stamped
                on every item this Source releases, e.g.
                {"family": "B", "price": 160}. Each value is fixed: a string,
                number or bool, the same for every item. Distributions are
                not allowed here. The key "part_type" is reserved.
            calendar: id of a calendar from create_shift_calendar, for
                deliveries that only come in working hours. The
                inter_arrival_time then counts working time only, so items
                arrive only inside a window. None means the default
                calendar if the model has one, else items arrive at any time.
        """
        return builders.create_source(
            id=id,
            inter_arrival_time=inter_arrival_time,
            flow_item_type=flow_item_type,
            item_length=item_length,
            blocking=blocking,
            out_edge_selection=out_edge_selection,
            node_setup_time=node_setup_time,
            part_type=part_type,
            attributes=attributes,
            calendar=calendar,
        )

    @mcp.tool()
    @traced
    def create_sink(
        id: str,
        node_setup_time: float = 0,
        scrap: bool = False,
    ) -> dict:
        """Create a Sink node that collects flow items at the end of the line.

        A Sink is terminal: it has no out_edge and needs at least one in_edge
        (wired later via connect).

        Args:
            id: unique node identifier.
            node_setup_time: constant initial setup delay.
            scrap: set True for a scrap bin. Items arriving here count as
                scrapped, not completed, in the per-type report of
                run_simulation, and add nothing to its attribute sums.
        """
        return builders.create_sink(
            id=id,
            node_setup_time=node_setup_time,
            scrap=scrap,
        )

    @mcp.tool()
    @traced
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
    ) -> dict:
        """Create a Machine node that processes flow items.

        A Machine needs at least one in_edge and one out_edge (both wired later
        via connect).

        Args:
            id: unique node identifier.
            work_capacity: number of items processed simultaneously (one worker
                thread per item); must be a positive int.
            processing_delay: time to process one item. Constant (int/float) or
                a distribution string sampled each cycle: "uniform(a, b)",
                "normal(m, s)", "gauss(m, s)", "exp(x)" (exp mean = x); samples
                clamped to >= 0.
            blocking: if True, wait for out_edge space; if False, discard when
                full.
            in_edge_selection: in-edge selection strategy, one of
                "FIRST_AVAILABLE", "RANDOM", "ROUND_ROBIN".
            out_edge_selection: out-edge selection strategy, one of
                "FIRST_AVAILABLE", "RANDOM", "ROUND_ROBIN".
            node_setup_time: constant initial setup delay.
            route_by: needs routes. Name of the item attribute that decides
                where each item goes, e.g. "family" or "part_type". Items get
                their attributes from create_source(part_type=...,
                attributes=...). Leave out_edge_selection at its default;
                route_by and routes take over that job.
            routes: needs route_by. Attribute value -> id of one of this
                machine's out-edges, e.g. {"A": "b_paint", "B": "b_drill"},
                or a list of out-edge ids the value may use, e.g.
                {"alu": ["b_m1", "b_m2"], "*": "b_m3"}. With a list, the
                machine waits on all listed edges and puts the item into the
                first that has space; the earlier one in the list wins a tie.
                A list needs blocking=True. The key "*" catches every other
                value. Only string values match. An item with no matching
                route and no "*" stops the run with an error. When a single
                target edge is full, the machine waits (blocking=True) or
                discards the item (blocking=False); no other edge is tried.
                Use processing_delay=0 for a machine that only sorts.
            route_shares: out-edge id -> share of items sent there, adding up
                to 1, e.g. {"b_ship": 0.95, "b_scrap": 0.05}. Chance picks
                the edge for each item. An edge may lead anywhere, also back
                to an earlier station for rework. Not with route_by or
                out_edge_selection.
            time_to_failure: needs repair_time. Uptime from the end of the
                setup or of a repair to the next breakdown, drawn anew each
                time and counted even while the machine is idle or blocked.
                Constant (> 0) or a distribution string, e.g. "exp(480)".
                If the MTBF includes the repair time, use MTBF - MTTR.
                With a calendar, only working time counts.
                During a breakdown, items being processed pause and finish
                their remaining time after the repair.
            repair_time: needs time_to_failure. Time one breakdown lasts.
                Constant (>= 0) or a distribution string; for an MTTR of 30
                use "exp(30)". run_simulation then reports num_breakdowns,
                time_down and BREAKDOWN_STATE for this machine. With a
                calendar, a repair only goes on in working time.
            calendar: id of a calendar from create_shift_calendar. Outside
                its windows the machine starts no new item, and an item
                being processed pauses and finishes its remaining time in the
                next window. None means the default calendar if the model
                has one, else the machine works all the time.
        """
        return builders.create_machine(
            id=id,
            work_capacity=work_capacity,
            processing_delay=processing_delay,
            blocking=blocking,
            in_edge_selection=in_edge_selection,
            out_edge_selection=out_edge_selection,
            node_setup_time=node_setup_time,
            route_by=route_by,
            routes=routes,
            route_shares=route_shares,
            time_to_failure=time_to_failure,
            repair_time=repair_time,
            calendar=calendar,
        )

    @mcp.tool()
    @traced
    def create_splitter(
        id: str,
        mode: str = "UNPACK",
        split_quantity: int | None = None,
        processing_delay: float | str = 0,
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
            processing_delay: time to process one item. Constant (int/float) or
                a distribution string sampled each cycle: "uniform(a, b)",
                "normal(m, s)", "gauss(m, s)", "exp(x)" (exp mean = x); samples
                clamped to >= 0.
            blocking: if True, wait for out_edge space; if False, discard when
                full.
            in_edge_selection: in-edge selection strategy, one of
                "FIRST_AVAILABLE", "RANDOM", "ROUND_ROBIN".
            out_edge_selection: out-edge selection strategy, one of
                "FIRST_AVAILABLE", "RANDOM", "ROUND_ROBIN".
            node_setup_time: constant initial setup delay.
        """
        return builders.create_splitter(
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
        processing_delay: float | str = 0,
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
            processing_delay: time to process one combine cycle. Constant
                (int/float) or a distribution string sampled each cycle:
                "uniform(a, b)", "normal(m, s)", "gauss(m, s)", "exp(x)" (exp
                mean = x); samples clamped to >= 0.
            blocking: if True, wait for out_edge space; if False, discard when
                full.
            out_edge_selection: out-edge selection strategy, one of
                "FIRST_AVAILABLE", "RANDOM", "ROUND_ROBIN".
            node_setup_time: constant initial setup delay.
        """
        return builders.create_combiner(
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
        delay: float | str = 0,
        mode: str = "FIFO",
    ) -> dict:
        """Create a Buffer edge: a FIFO/LIFO queue between two nodes.

        A Buffer holds items waiting to be accepted by its destination node. It
        connects exactly one src node to one dest node (both wired later via
        connect).

        Args:
            id: unique edge identifier (unique across nodes and edges).
            capacity: max items the buffer can hold; must be a positive int.
            delay: time after which a put item becomes available to get.
                Constant (int/float) or a distribution string sampled each
                cycle: "uniform(a, b)", "normal(m, s)", "gauss(m, s)", "exp(x)"
                (exp mean = x); samples clamped to >= 0.
            mode: "FIFO" (oldest item available first) or "LIFO" (newest first).
        """
        return builders.create_buffer(
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
        return builders.create_conveyor(
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
        delay: float | str = 1,
        transit_delay: float | str = 0,
    ) -> dict:
        """Create a Fleet edge: transporters (AGVs) moving items between nodes.

        A Fleet moves up to `capacity` items at once. It connects exactly one
        src node to one dest node (both wired later via connect).

        Args:
            id: unique edge identifier (unique across nodes and edges).
            capacity: number of items moved per trip; must be a positive int.
            delay: wait before the fleet departs if it hasn't filled to
                capacity. Constant (int/float) or a distribution string sampled
                each cycle: "uniform(a, b)", "normal(m, s)", "gauss(m, s)",
                "exp(x)" (exp mean = x); samples clamped to >= 0.
            transit_delay: src->dest travel time. Same forms as `delay`.
        """
        return builders.create_fleet(
            id=id,
            capacity=capacity,
            delay=delay,
            transit_delay=transit_delay,
        )

    @mcp.tool()
    @traced
    def create_shift_calendar(
        id: str,
        cycle: float,
        windows: list[list[float]],
        default: bool = False,
    ) -> dict:
        """Create a shift calendar: working windows that repeat every cycle.

        When most stations work the same hours, create that calendar with
        default=True: every machine and source without its own `calendar`
        then follows it, also those created before it. Give a station
        calendar=<id> only when its hours differ. A station that must work
        around the clock in such a model gets its own calendar with one
        window over the whole cycle, e.g. windows=[[0, 1440]].
        Time 0 of the run is the
        start of the first cycle, so with a day of 1440 minutes, time 0 is
        midnight and 360 is 6:00. Run until a whole number of cycles to
        cover full days or weeks, e.g. until=7200 for five days.

        Example in minutes, two shifts 6:00-14:00 and 14:00-22:00 with a
        15-minute break at 9:00 and 17:00 and a 30-minute break at 12:00 and
        20:00, no night work:
            cycle=1440, windows=[[360, 540], [555, 720], [750, 1020],
                                 [1035, 1200], [1230, 1320]]
        For Monday to Friday only, use a week, cycle=10080, and repeat the
        day's windows shifted by 1440 for each working day.

        Args:
            id: unique calendar identifier.
            cycle: length of one cycle in model time units (the same unit as
                every other time in the model); must be > 0.
            windows: working windows inside one cycle as [start, end] pairs,
                0 <= start < end <= cycle, in ascending order and not
                overlapping. A window includes its start and excludes its end.
                Windows that touch are joined into one.
            default: if True, every machine and source without its own
                calendar follows this one. A model has at most one default.
        """
        return builders.create_shift_calendar(
            id=id, cycle=cycle, windows=windows, default=default
        )

    @mcp.tool()
    @traced
    def connect(edge_id: str, src_id: str, dest_id: str) -> dict:
        """Wire a single edge between two nodes.

        Appends the edge to the source node's out_edges and the destination
        node's in_edges. An edge connects exactly one src to one dest; a node
        accumulates several in/out edges by being the endpoint of several
        connect calls.

        Connect order matters at shared destinations: a node with
        in_edge_selection="FIRST_AVAILABLE" drains its in-edges in the order
        they were connected, so wire the higher-priority stream's edge first
        to give it non-preemptive priority.

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
        resolved src/dest node ids). With shift calendars it also lists each
        calendar with its windows and the stations that use it. Read-only.
        """
        return simulation.get_model()

    @mcp.tool()
    @traced
    def render_model_diagram(path: str = "out/model.html") -> dict:
        """Draw the current model as a flowchart in an HTML page at `path`.

        Call it after building the model and before run_simulation or
        run_replications, so the user can check how the model was built
        before it runs. Open the file in a browser to see it. Read-only.
        """
        return simulation.render_model_diagram(path=path)

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
    def run_simulation(
        until: float, seed: int | None = None, report_path: str | None = None
    ) -> dict:
        """Run the simulation up to time `until` and return a stats summary.

        This executes the model: any unmet edge-cardinality requirement (e.g. a
        Source with no out_edge) surfaces here. Returns per-node/edge stats.

        Args:
            until: simulation end time; must be a positive number.
            seed: optional RNG seed. With a seed, the same freshly built model
                reproduces the run exactly (identical draws, routing, stats);
                omit it for a fresh random run. Use distinct seeds to sample
                several reproducible replications of a stochastic model.
            report_path: optional path of an HTML report, e.g.
                "out/report.html": the model diagram plus tables of the
                results. The result then holds `report`, the full path.
        """
        return simulation.run_simulation(
            until=until, seed=seed, report_path=report_path
        )

    @mcp.tool()
    @traced
    def run_replications(
        until: float,
        replications: int = 10,
        random_seed_base: int = 0,
        report_path: str | None = None,
    ) -> dict:
        """Run the current model many times and report means with confidence intervals.

        Use this instead of `run_simulation` to state a result for a stochastic
        model (any distribution string, or RANDOM selection).

        Each replication rebuilds the graph and runs it from a clock at 0, so
        the runs are independent. The current model is only read — the `verify_*`
        tools still refer to your last `run_simulation`. Build the model with
        the create_*/connect tools first.

        Independent runs are spread across CPU cores when the batch is heavy
        enough to be worth it, which this decides for itself by timing the first
        replication on this machine. It changes nothing about the numbers: a
        replication's result depends on its seed alone.

        Args:
            until: simulation end time for each run; must be a positive number.
            replications: number of independent runs; an int in [2, 100]. 10 is
                a reasonable default; more narrows the confidence intervals.
            random_seed_base: base RNG seed. Replication i uses
                `random_seed_base + i * 1000`, so the whole batch replays
                exactly from this one number.
            report_path: optional path of an HTML report, e.g.
                "out/report.html": the model diagram plus tables of the
                results over all runs. The result then holds `report`, the
                full path.

        Returns a dict with `summary` (a formatted text report — read this
        first), `analysis` (per-metric mean/CI, keyed `node_id.stat_name`;
        with part types also `part_types.<type>.completed`, `.scrapped`,
        `.generated` and `.completed_attribute_sums.<attribute>`, e.g. the
        price sum per type), `precision` (per metric, the fewest of these runs that would have put
        the 95% interval within +/-10% of the mean, or `reached: false` when
        this batch never got there), `requested_replications`, `successful_replications`,
        `failures` (any runs that raised, with their seeds), and `execution`
        (how many cores the batch actually used).
        """
        return simulation.run_replications(
            until=until,
            replications=replications,
            random_seed_base=random_seed_base,
            report_path=report_path,
        )

    @mcp.tool()
    @traced
    def find_replication_count(
        until: float,
        target_metrics: list[str] | None = None,
        desired_precision: float = 0.10,
        replication_budget: int = 50,
        random_seed_base: int = 0,
    ) -> dict:
        """Find how many replications a stochastic model needs, by running it.

        Use this before `run_replications` when you do not know what count to
        ask for. It keeps running the model until the 95% confidence interval
        is within `desired_precision` of the mean **and stays there** for five
        further runs, then reports the count where that first held. Feed that
        count to `run_replications` to produce the numbers you quote.

        Expect the search to run somewhat more simulations than the count it reports, and more than
        `replication_budget` when it never settles.

        Args:
            until: simulation end time for each run; must be a positive number.
            target_metrics: metrics that must settle, named `node_id.stat_name`
                exactly as `analysis` reports them (e.g.
                "snk.num_item_received"). Omit for every sink's throughput,
                which is the usual answer. All named metrics must settle.
            desired_precision: how tight the interval must be, as a share of the
                mean. 0.10 means "within +/-10% of the mean" and is the usual
                figure; 0.05 is stricter and costs roughly four times the runs.
            replication_budget: where the search gives up, an int in [2, 100].
            random_seed_base: base RNG seed, so a search replays exactly.

        Returns a dict with `replications_needed` (per metric, or null where the
        target never held), `converged`, `unresolved` (metrics that did not
        settle), `runs_executed`, `tables` (the running mean
        and interval per run) and `settings`.

        When `converged` is false, say so and report what precision was reached
        instead of quoting a mean as if it were settled.
        """
        return simulation.find_replication_count(
            until=until,
            target_metrics=target_metrics,
            desired_precision=desired_precision,
            replication_budget=replication_budget,
            random_seed_base=random_seed_base,
        )

    @mcp.tool()
    @traced
    def validate_model() -> dict:
        """Check the assembled graph before running it.

        Reads the wiring, runs nothing, changes nothing. Call it once
        the model is built and wired, and before run_simulation. It catches
        what a run cannot tell you apart from a genuine result.

        Fix every entry in `errors` before you run: each one either stops the
        run outright or leaves part of the model taking no part in it. They
        are: a node missing an in/out edge, an edge created but never
        connected, a Combiner whose target_quantity_of_each_item doesn't match
        its in_edge count, a node on no route from a Source to a Sink.

        `warnings` mean the run will succeed at simulating something other than
        what was intended: a non-blocking node that silently drops items at a
        full out-edge, a conveyor whose item_length disagrees with the Source
        feeding it, an UNPACK splitter or a Combiner with no pallets upstream.

        Returns `valid`, `errors`, `warnings`
        (each finding naming its check, component and what to do), and
        `checked` (how many nodes and edges were inspected).
        """
        return validation.validate_model()

    @mcp.tool()
    @traced
    def verify_extreme_conditions(
        until: float,
        seed: int | None = None,
        parts_per_source: int = 1,
        sigmas: float = 3.0,
        quantile: float = 0.99,
    ) -> dict:
        """Check every station's time by sending a single part through the model.

        One part is released into a copy of the model, three times over: with
        every delay distribution at its lower bound, with the model as built,
        and with every delay at its upper bound. With one part in the plant
        nothing queues, so the time it spends at a station is that station's own
        time, and the report holds that against the value the station was given.
        A part that waits where no waiting time was entered, a station that
        takes longer than it was given, a time that can fall to zero, or a
        distribution with no upper bound all surface here.

        The times themselves are for the reader to judge against the real
        plant: 38 minutes of hardening or 16 minutes of turning are numbers a
        run at normal load never shows.

        When the model has a finished run, its parts are measured too: per
        station the fastest, the mean and the slowest, and every part's total
        against the single part's total at the lower bound. That total is a
        floor no part can beat, since waiting only adds to it — a part below it
        skipped a station, and its route is named.

        The session model is only read: its clock and its last run's stats are
        left alone for the other verify_* tools.

        Args:
            until: end time for each of the three runs; must be a positive
                number, long enough for a part to reach a sink at the upper
                bound.
            seed: RNG seed for the run at the built values.
            parts_per_source: items each source releases at time 0. Keep it at
                1 so nothing queues; raise it only when a combiner needs
                several items per cycle, and read the times as including
                waiting.
            sigmas: how far a normal/gauss reaches each way, in standard
                deviations. 3 covers all but about 3 draws in 1000.
            quantile: stands in for an exp's missing upper bound.

        Returns a dict with `summary` (read this first), `passed`, `stations`
        (per place: the value entered, the three measured times, the run's
        fastest/mean/slowest), `single`, `run`, `below_floor`, `findings`,
        `substitutions` and `settings`.
        """
        return validation.verify_extreme_conditions(
            until=until,
            seed=seed,
            parts_per_source=parts_per_source,
            sigmas=sigmas,
            quantile=quantile,
        )

    @mcp.tool()
    @traced
    def verify_conservation() -> dict:
        """Check that every item of the last run is accounted for, and say where.

        Splits the items all Sources generated into those received by Sinks,
        those still in edges, those still in stations (counted directly) and
        those discarded, and checks that the four add up. `missing` is the gap:
        positive means items were lost inside the simulation, negative that items
        appeared from nowhere. `summary` and `findings` state what the numbers
        show, including items dropped at a non-blocking node and how much of the
        run's output never left the line (a run too short or a line congested).
        Requires a prior run_simulation. Splitters and combiners change the
        physical item count, so on those lines `in_machines` and `missing` are
        null, `exact` is false and they are named in `packing_nodes`.
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

        Also returns where the items went: `routes` (delivered items per part
        type and station sequence, with end sink, scrap flag and share),
        `repeat_visits` (items that passed a station more than once),
        `not_delivered` (items still in the line, by where they wait) and
        `warnings` (unvisited_station: no item passed a node;
        type_not_delivered: a part type reached no non-scrap sink). Compare
        the routes with the plant description.
        """
        return validation.verify_item_flow()

    return mcp
