"""The hand calculation: what throughput should this line reach?

Everything else in this package reads the model or a finished run and asks
whether it is consistent with itself. This module produces a number the model
had no part in: the throughput a deterministic version of the line must reach,
worked out from the wiring and the parameters alone.

The rule is the bottleneck argument. Every station has a rate — how many items
per unit of time it can pass at full tilt — and the line runs at the smallest
of them, capped by what the sources feed in.

    Source                 1 / inter-arrival time
    Machine                workplaces / processing time
    Splitter               1 / processing time, in pallets
    Combiner               1 / processing time, in pallets
    Buffer with a delay    places / delay
    Conveyor               speed / item length

Two things make this more than one `min()` over a list.

**Fan-out.** A node with several out-edges divides its output, and how it
divides decides what the group can do. Under "ROUND_ROBIN" (and "RANDOM", which
the deterministic twin turns into it) every branch gets a fixed equal share, so
the slowest branch sets the pace and the group manages `n` times it. Under
"FIRST_AVAILABLE" the branches pool, because an item goes wherever there is
room, and the group manages their sum.

**Packing.** A Combiner turns several items into one pallet, and a Splitter
turns one pallet back into the items inside it plus the empty container. The
item count changes along the route, so a rate on one side of such a node is not
a rate on the other. Rates are therefore carried as *cycles* per unit of time
inside a node and converted to items on each edge.

The calculation runs in two passes over the graph. Backwards from the sinks it
works out how much each node can get through to a sink; forwards from the
sources it pushes the actual flow along and takes the smaller of supply and
capacity at every step. What comes out is the flow arriving at each sink and a
table of every station's rate next to the flow it carries, so the arithmetic
can be checked line by line.

A Fleet has no rate here. The vendored fleet applies its transit delay twice
and starts a fresh transit process for every activation over the same shared
item list, so several batches travel at once and its throughput follows the
interleaving of those processes rather than a transport policy. There is no
figure to state, so a route through a Fleet makes the whole calculation
inapplicable.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from simtrace.model import FactoryModel
from simtrace.tools.distributions import mean_of

INFINITE = float("inf")

# Selection strategies that hand every out-edge the same fixed share. RANDOM is
# here because the deterministic twin rewrites it to ROUND_ROBIN.
_EVEN_SPLIT = ("ROUND_ROBIN", "RANDOM")

_SOURCE = "Source"
_SINK = "Sink"
_MACHINE = "Machine"
_SPLITTER = "Splitter"
_COMBINER = "Combiner"

# Slack allowed when deciding whether a station carries its full rate, i.e.
# whether to name it as a bottleneck.
_TIE = 1e-9


def _per_time(count: float, duration: float) -> float:
    """`count` per `duration`, with a zero duration meaning no limit."""
    if duration <= 0:
        return INFINITE
    return count / duration


def _blocker(reason: str, component: Optional[str], message: str) -> dict:
    """One entry saying why no expected value can be stated."""
    return {"reason": reason, "component": component, "message": message}


class Plant:
    """The graph, its parameters, and the rates derived from both.

    Built from a `FactoryModel`: node and edge types come from the live
    components, every constructor argument from the recorded build spec. The
    spec is the only place some arguments survive — a ConveyorBelt keeps no
    `item_length` attribute — and it is also where a delay is still the
    original `"exp(5)"` string rather than a sampler.
    """

    def __init__(self, model: FactoryModel) -> None:
        self.model = model

        self.ops: Dict[str, str] = {}
        self.params: Dict[str, dict] = {}
        for step in model.spec:
            component_id = step["kwargs"].get("id")
            if isinstance(component_id, str):
                self.ops[component_id] = step["op"]
                self.params[component_id] = step["kwargs"]

        self.types = {
            node_id: type(node).__name__ for node_id, node in model.nodes.items()
        }

        self.endpoints: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        for edge_id, edge in model.edges.items():
            src = getattr(edge, "src_node", None)
            dest = getattr(edge, "dest_node", None)
            self.endpoints[edge_id] = (
                getattr(src, "id", None) if src is not None else None,
                getattr(dest, "id", None) if dest is not None else None,
            )

        self.out_edges: Dict[str, List[str]] = {
            node_id: self._edge_ids(node, "out_edges")
            for node_id, node in model.nodes.items()
        }
        self.in_edges: Dict[str, List[str]] = {
            node_id: self._edge_ids(node, "in_edges")
            for node_id, node in model.nodes.items()
        }

        self.upstream: Dict[str, List[str]] = {node_id: [] for node_id in self.types}
        self.downstream: Dict[str, List[str]] = {node_id: [] for node_id in self.types}
        for src, dest in self.endpoints.values():
            if src is None or dest is None:
                continue
            self.downstream.setdefault(src, []).append(dest)
            self.upstream.setdefault(dest, []).append(src)

        self._pack_counts: Dict[str, Optional[int]] = {}
        self.cap: Dict[str, float] = {}
        self.cycles: Dict[str, float] = {}
        self.flow: Dict[str, float] = {}
        self.delivered: Dict[str, float] = {}

    # -- reading the model -------------------------------------------------

    @staticmethod
    def _edge_ids(node: object, attr: str) -> List[str]:
        """A node's in_edges/out_edges as ids, in connect order.

        FactorySimPy leaves both as None until the first `connect`. Connect
        order is load-bearing: it is the order FIRST_AVAILABLE drains and fills
        them in, and the position a Combiner's per-edge quantities are indexed
        by.
        """
        return [edge.id for edge in (getattr(node, attr, None) or [])]

    def param(self, component_id: str, name: str, default: object = None) -> object:
        """One recorded constructor argument of a node or edge."""
        return self.params.get(component_id, {}).get(name, default)

    # -- rates -------------------------------------------------------------

    def edge_rate(self, edge_id: str) -> float:
        """Items per unit of time the edge itself can pass.

        A buffer with no holding time never holds anything back, so a plain
        hand-off is unlimited here and only the nodes at its ends constrain the
        flow.
        """
        op = self.ops.get(edge_id)
        if op == "create_buffer":
            delay = mean_of("delay", self.param(edge_id, "delay", 0))
            return _per_time(float(self.param(edge_id, "capacity", 1)), delay)
        if op == "create_conveyor":
            speed = float(self.param(edge_id, "speed", 1))
            item_length = float(self.param(edge_id, "item_length", 1))
            return _per_time(speed, item_length)
        # A Fleet reaches here only if the caller ignored the blocker.
        return INFINITE

    def own_rate(self, node_id: str) -> float:
        """Cycles per unit of time the node can complete on its own."""
        node_type = self.types.get(node_id)
        if node_type == _SOURCE:
            arrival = mean_of(
                "inter_arrival_time", self.param(node_id, "inter_arrival_time", 1.0)
            )
            return _per_time(1.0, arrival)
        if node_type == _SINK:
            return INFINITE
        delay = mean_of("processing_delay", self.param(node_id, "processing_delay", 0))
        workplaces = 1.0
        if node_type == _MACHINE:
            workplaces = float(self.param(node_id, "work_capacity", 1))
        return _per_time(workplaces, delay)

    def out_multiplier(self, node_id: str) -> float:
        """Items the node emits per cycle.

        One everywhere except a Splitter, which empties a pallet item by item
        and then pushes the empty container after them.
        """
        if self.types.get(node_id) != _SPLITTER:
            return 1.0
        packed = self.pack_count(node_id)
        return 1.0 if packed is None else float(packed + 1)

    def in_demand(self, node_id: str, edge_id: str) -> float:
        """Items the node takes from that in-edge per cycle.

        One everywhere except a Combiner, which is told per in-edge position
        how many to pull before it packs.
        """
        if self.types.get(node_id) != _COMBINER:
            return 1.0
        quantities = self.param(node_id, "target_quantity_of_each_item") or [1]
        edges = self.in_edges.get(node_id, [])
        if edge_id not in edges:
            return 1.0
        position = edges.index(edge_id)
        if position >= len(quantities):
            return 1.0
        return float(quantities[position])

    def pack_count(self, splitter_id: str) -> Optional[int]:
        """How many items sit in a pallet arriving at this splitter.

        A pallet is filled in exactly one place: a Combiner, which packs the
        quantities named for its in-edges after the first (the first is the
        pallet edge itself). A pallet straight from a Source is empty. So the
        count is read off whichever of the two the route upstream reaches, and
        the walk stops there.

        Returns None when no pallet origin is upstream, or when two origins
        disagree — either way there is no single number to unpack by.
        """
        if splitter_id in self._pack_counts:
            return self._pack_counts[splitter_id]

        found = set()
        seen = {splitter_id}
        queue = [splitter_id]
        while queue:
            current = queue.pop()
            for previous in self.upstream.get(current, ()):
                if previous in seen:
                    continue
                seen.add(previous)
                node_type = self.types.get(previous)
                if node_type == _COMBINER:
                    quantities = (
                        self.param(previous, "target_quantity_of_each_item") or [1]
                    )
                    found.add(int(sum(quantities[1:])))
                elif (
                    node_type == _SOURCE
                    and self.param(previous, "flow_item_type") == "pallet"
                ):
                    found.add(0)
                else:
                    queue.append(previous)

        count = found.pop() if len(found) == 1 else None
        self._pack_counts[splitter_id] = count
        return count

    # -- the two passes ----------------------------------------------------

    def branch_rates(self, node_id: str) -> List[float]:
        """Items per time each out-edge of the node can carry away."""
        rates = []
        for edge_id in self.out_edges.get(node_id, []):
            dest = self.endpoints[edge_id][1]
            accept = INFINITE
            if dest is not None:
                accept = self.cap.get(dest, INFINITE) * self.in_demand(dest, edge_id)
            rates.append(min(self.edge_rate(edge_id), accept))
        return rates

    def fanout_capacity(self, node_id: str) -> float:
        """Items per time the node can push into all of its out-edges."""
        if not self.out_edges.get(node_id):
            return INFINITE
        rates = self.branch_rates(node_id)
        selection = self.param(node_id, "out_edge_selection", "FIRST_AVAILABLE")
        if selection in _EVEN_SPLIT:
            return len(rates) * min(rates)
        return sum(rates)

    def topological_order(self) -> Optional[List[str]]:
        """Nodes ordered so every node follows all of its predecessors.

        None when the wiring contains a loop: both passes assume every upstream
        (or downstream) value is already settled, which a loop makes untrue.
        """
        remaining = {
            node_id: len(set(self.upstream.get(node_id, ()))) for node_id in self.types
        }
        ready = [node_id for node_id, count in remaining.items() if count == 0]
        order: List[str] = []
        while ready:
            current = ready.pop()
            order.append(current)
            for following in set(self.downstream.get(current, ())):
                remaining[following] -= 1
                if remaining[following] == 0:
                    ready.append(following)
        return order if len(order) == len(self.types) else None

    def _distribute(self, node_id: str, items: float) -> None:
        """Place a node's output onto its out-edges by its selection rule."""
        edges = self.out_edges.get(node_id, [])
        if not edges:
            return
        rates = self.branch_rates(node_id)
        selection = self.param(node_id, "out_edge_selection", "FIRST_AVAILABLE")
        if selection in _EVEN_SPLIT:
            share = min(items / len(edges), min(rates))
            for edge_id in edges:
                self.delivered[edge_id] = share
            return
        remaining = items
        for edge_id, rate in zip(edges, rates):
            taken = min(remaining, rate)
            self.delivered[edge_id] = taken
            remaining -= taken

    def solve(self, order: List[str]) -> None:
        """Fill in `cap`, `cycles`, `flow` and `delivered` for the whole graph."""
        for node_id in reversed(order):
            if self.types.get(node_id) == _SINK:
                self.cap[node_id] = INFINITE
                continue
            multiplier = self.out_multiplier(node_id)
            self.cap[node_id] = min(
                self.own_rate(node_id), self.fanout_capacity(node_id) / multiplier
            )

        for node_id in order:
            node_type = self.types.get(node_id)
            in_edges = self.in_edges.get(node_id, [])

            if node_type == _SINK:
                arriving = sum(self.delivered.get(e, 0.0) for e in in_edges)
                self.cycles[node_id] = arriving
                self.flow[node_id] = arriving
                continue

            multiplier = self.out_multiplier(node_id)
            limits = [
                self.own_rate(node_id),
                self.fanout_capacity(node_id) / multiplier,
            ]
            if node_type == _COMBINER:
                limits.append(
                    min(
                        self.delivered.get(e, 0.0) / self.in_demand(node_id, e)
                        for e in in_edges
                    )
                    if in_edges
                    else 0.0
                )
            elif node_type != _SOURCE:
                limits.append(sum(self.delivered.get(e, 0.0) for e in in_edges))

            cycles = min(limits)
            self.cycles[node_id] = cycles
            self.flow[node_id] = cycles * multiplier
            self._distribute(node_id, self.flow[node_id])


def _node_basis(plant: Plant, node_id: str, node_type: str, multiplier: float) -> str:
    """The arithmetic behind a node's rate, spelled out for checking by hand."""
    if node_type == _SOURCE:
        arrival = mean_of(
            "inter_arrival_time", plant.param(node_id, "inter_arrival_time", 1.0)
        )
        return f"1 item / {arrival:g}"
    delay = mean_of("processing_delay", plant.param(node_id, "processing_delay", 0))
    if node_type == _MACHINE:
        return f"{plant.param(node_id, 'work_capacity', 1)} workplace(s) / {delay:g}"
    if node_type == _SPLITTER:
        return f"{multiplier:g} item(s) per pallet / {delay:g}"
    return f"1 pallet / {delay:g}"


def _edge_basis(plant: Plant, edge_id: str) -> Tuple[str, str]:
    """An edge's kind and the arithmetic behind its rate."""
    op = plant.ops.get(edge_id)
    if op == "create_buffer":
        delay = mean_of("delay", plant.param(edge_id, "delay", 0))
        if delay <= 0:
            return "Buffer", "no holding time"
        return "Buffer", f"{plant.param(edge_id, 'capacity', 1)} place(s) / {delay:g}"
    if op == "create_conveyor":
        return (
            "Conveyor",
            f"speed {plant.param(edge_id, 'speed')} / item length "
            f"{plant.param(edge_id, 'item_length')}",
        )
    return "Fleet", "not rated"


def _station_rows(plant: Plant) -> List[dict]:
    """One row per node and edge: its rate, its flow, and the arithmetic.

    A rate of None means the component places no limit of its own — a buffer
    with no holding time, a machine with no processing time.
    """
    rows: List[dict] = []

    for node_id, node_type in plant.types.items():
        if node_type == _SINK:
            continue
        multiplier = plant.out_multiplier(node_id)
        rows.append(
            {
                "component": node_id,
                "kind": node_type,
                "rate": plant.own_rate(node_id) * multiplier,
                "flow": plant.flow.get(node_id, 0.0),
                "basis": _node_basis(plant, node_id, node_type, multiplier),
            }
        )

    for edge_id in plant.endpoints:
        kind, basis = _edge_basis(plant, edge_id)
        rows.append(
            {
                "component": edge_id,
                "kind": kind,
                "rate": plant.edge_rate(edge_id),
                "flow": plant.delivered.get(edge_id, 0.0),
                "basis": basis,
            }
        )

    for row in rows:
        rate = row["rate"]
        row["binding"] = rate < INFINITE and row["flow"] >= rate * (1 - _TIE)
        if rate == INFINITE:
            row["rate"] = None
    return rows


def _collect_blockers(plant: Plant) -> List[dict]:
    """Everything that stops an expected value from being stated."""
    blockers: List[dict] = []

    for edge_id, op in plant.ops.items():
        if op == "create_fleet":
            blockers.append(
                _blocker(
                    "fleet_on_route",
                    edge_id,
                    f"'{edge_id}' is a fleet. The fleet applies its transit "
                    "delay twice and runs several transits at once over one "
                    "shared item list, so it has no throughput that can be "
                    "worked out in advance. Replace it with a buffer whose "
                    "holding time is the travel time to run this check.",
                )
            )

    for edge_id, (src, dest) in plant.endpoints.items():
        if src is None or dest is None:
            blockers.append(
                _blocker(
                    "edge_not_connected",
                    edge_id,
                    f"'{edge_id}' is not wired at both ends. Run validate_model "
                    "and fix the wiring first.",
                )
            )

    if _SOURCE not in plant.types.values():
        blockers.append(
            _blocker("no_source", None, "The model has no source, so nothing arrives.")
        )
    if _SINK not in plant.types.values():
        blockers.append(
            _blocker("no_sink", None, "The model has no sink, so nothing is finished.")
        )

    for node_id, node_type in plant.types.items():
        if node_type != _SPLITTER:
            continue
        if plant.param(node_id, "mode") == "SPLIT":
            blockers.append(
                _blocker(
                    "splitter_split_mode",
                    node_id,
                    f"Splitter '{node_id}' is set to SPLIT. The vendored "
                    "splitter only ever unpacks pallets, so this model cannot "
                    "run at all. Set mode to UNPACK and feed it packed pallets.",
                )
            )
            continue
        if plant.pack_count(node_id) is None:
            blockers.append(
                _blocker(
                    "splitter_pack_count",
                    node_id,
                    f"How many items a pallet reaching splitter '{node_id}' "
                    "holds cannot be read off the model: there is no combiner "
                    "or pallet source upstream of it, or two of them disagree.",
                )
            )

    return blockers


def expected_throughput(model: FactoryModel) -> dict:
    """Work out the throughput this model's deterministic version must reach.

    Args:
        model: a wired model. Delays are read as means, so this can be given
            either the stochastic original or its deterministic twin — the
            answer is the same.

    Returns:
        A dict with:
          - `applicable`: whether a figure could be stated at all,
          - `blockers`: what stopped it, each naming its component,
          - `per_sink`: items per unit of time arriving at each sink,
          - `total`: those added up,
          - `stations`: every node and edge with its rate, the flow it carries,
            and the arithmetic behind the rate,
          - `bottlenecks`: the components running at their full rate.
    """
    plant = Plant(model)
    blockers = _collect_blockers(plant)

    order = plant.topological_order()
    if order is None:
        blockers.append(
            _blocker(
                "loop_in_wiring",
                None,
                "The wiring contains a loop. A rework loop has no single "
                "bottleneck rate that can be worked out this way.",
            )
        )

    if blockers:
        return {
            "applicable": False,
            "blockers": blockers,
            "per_sink": {},
            "total": None,
            "stations": [],
            "bottlenecks": [],
        }

    plant.solve(order)
    per_sink = {
        node_id: plant.flow[node_id]
        for node_id, node_type in plant.types.items()
        if node_type == _SINK
    }
    stations = _station_rows(plant)

    return {
        "applicable": True,
        "blockers": [],
        "per_sink": per_sink,
        "total": sum(per_sink.values()),
        "stations": stations,
        "bottlenecks": [row["component"] for row in stations if row["binding"]],
    }
