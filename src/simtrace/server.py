from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from simtrace.tools.registry import register_tools
from simtrace.tools.telemetry import configure_telemetry

INSTRUCTIONS = """\
Discrete-event material-flow simulation tools (FactorySimPy): active nodes
(source/machine/splitter/combiner/sink) wired by passive edges
(buffer/conveyor/fleet), then run and verify.

Modeling patterns items carry NO type/attributes. Encode everything in the
graph structure:
- Every node-to-node hop needs an edge. A bare hand-off is a buffer with
  capacity=1, delay=0.
- Product mix: one Source per type; inter_arrival_time = overall mean / mix
  fraction. E.g. one order every 8 min, 60%/40% mix -> "exp(13.33)" and
  "exp(20)". Never use the mix fraction itself as the exp argument.
- Per-type routings: give each type its own branch of nodes; share stations
  only from the point where all routings are identical to the end. Merging
  (several in-edges) is fine; splitting by type after a shared station is
  impossible.
- Priority: a machine with in_edge_selection="FIRST_AVAILABLE" drains its
  in-edges in connect order — connect the higher-priority type's buffer
  first. Non-preemptive: a running job is never interrupted.
- Passive waiting (curing, cooling, drying, ovens): the buffer's own delay
  with a capacity, not a machine.
- N identical machines, one shared queue ("any free machine takes the next
  item"): one machine with work_capacity=N. N machines with their own queues:
  N machine nodes, each fed by its own buffer from the upstream node. Set the
  upstream node's out_edge_selection="ROUND_ROBIN" to spread items evenly.
- Pass seed to run_simulation for a reproducible run; distinct seeds give
  independent replications. After a run, use verify_conservation and
  verify_item_flow to catch lost or misrouted items.
"""

mcp = FastMCP("simpy_blocks", instructions=INSTRUCTIONS)
register_tools(mcp)


def main() -> None:
    configure_telemetry()
    mcp.run()


if __name__ == "__main__":
    main()
