from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from simtrace.tools.registry import register_tools
from simtrace.tools.telemetry import configure_telemetry

INSTRUCTIONS = """\
Discrete-event material-flow simulation tools (FactorySimPy): active nodes
(source/machine/splitter/combiner/sink) wired by passive edges
(buffer/conveyor/fleet), then run and verify.

Modeling patterns:
- Part types: create_source(part_type=<name>, attributes={...}) stamps the
  type and its attributes on every item the Source releases. run_simulation
  returns per-type counts and attribute sums. Attributes change where an item
  goes only at a machine with route_by.
- Every node-to-node hop needs an edge. A bare hand-off is a buffer with
  capacity=1, delay=0.
- Product mix: one Source per type; inter_arrival_time = overall mean / mix
  fraction. E.g. one order every 8 min, 60%/40% mix -> "exp(13.33)" and
  "exp(20)". Never use the mix fraction itself as the exp argument.
- Per-type routings: share a station between types and split them afterwards
  with create_machine(route_by=<attribute>, routes={<value>: <out-edge id>,
  "*": <out-edge id>}). The machine sends each item to the out-edge its
  attribute value names; "*" catches every other value. Merging (several
  in-edges) needs nothing special.
- Scrap and rework: create_machine(route_shares={<out-edge id>: <share>, ...})
  sends each item to an edge at random, e.g. {"b_ship": 0.95, "b_scrap":
  0.05}; the shares add up to 1. Put a create_sink(scrap=True) behind the
  scrap edge so the per-type report counts those items as scrapped. For
  rework, let an edge lead back to an earlier station.
- Priority: a machine with in_edge_selection="FIRST_AVAILABLE" drains its
  in-edges in connect order — connect the higher-priority type's buffer
  first. Non-preemptive: a running job is never interrupted.
- Passive waiting (curing, cooling, drying, ovens): the buffer's own delay
  with a capacity, not a machine.
- N identical machines, one shared queue ("any free machine takes the next
  item"): one machine with work_capacity=N. N machines with their own queues:
  N machine nodes, each fed by its own buffer from the upstream node. Set the
  upstream node's out_edge_selection="ROUND_ROBIN" to spread items evenly.
- Pass seed to run_simulation for a reproducible run. After a run, use
  verify_conservation and verify_item_flow to catch lost or misrouted items.
- Stochastic models (any distribution string, or RANDOM selection): one
  run_simulation is a single sample and can be badly unrepresentative. Before
  quoting throughput or utilization, use run_replications — it runs the model
  several times and reports means with 95% confidence intervals. Report the
  mean and the interval, not one run's number.
- How many replications: when the count is not given to you, use
  find_replication_count first and run_replications with the count it reports.
  It runs the model until the interval is tight enough and stays tight, so the
  count is measured rather than guessed. Every run_replications result also
  carries a `precision` section saying how few of the runs it made would have
  sufficed; if it reports the target was never reached, say the result is not
  settled instead of quoting the mean as if it were.
"""

mcp = FastMCP("simpy_blocks", instructions=INSTRUCTIONS)
register_tools(mcp)


def main() -> None:
    configure_telemetry()
    mcp.run()


if __name__ == "__main__":
    main()
