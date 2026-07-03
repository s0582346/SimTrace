# Example: chair workshop

A realistic, domain-language prompt — no FactorySimPy/tool vocabulary — for
testing how an MCP client maps a real problem onto the simtrace tools.

## Prompt

> I run a small workshop making wooden chairs. Raw chair kits arrive roughly
> every 2 minutes. Each kit is first sanded (about 3 minutes per kit), then
> painted (about 5 minutes per kit). There's a small holding area between
> sanding and painting that fits at most 5 kits. Finished chairs leave the shop.
> Model this workshop and simulate one 8-hour shift, then tell me the
> throughput — how many chairs we finish — and where the bottleneck is.

## What to watch for

This is an evaluation, so the point is to observe what the
model decides:

- **Primitive choice** — arrivals → a source, sanding/painting → machines
  (`processing_delay` 3 and 5), holding area → a buffer (capacity 5), finished
  chairs → a sink.
- **Every link needs an edge** — nodes can only be wired through an edge
  (buffer/conveyor), so the model must add an edge on *each* link
  (source→sand, sand→paint, paint→sink), not just the one holding area named in
  the prompt.
- **Units & horizon** — keep everything in minutes and run until 480.
- **Cardinality** — a source has one out-edge, a sink at least one in-edge,
  machines both; otherwise the run-time assertion fires.
- **The analysis** — "where's the bottleneck" is reasoning over the returned
  stats: painting at 5 min/kit against arrivals every 2 min makes paint the
  constraint, and the upstream buffer fills.
