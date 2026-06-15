# Simulation Tools

MCP tools that operate on the **assembled model as a whole** — wiring nodes
together with edges, inspecting the current graph, and running the simulation.
These are the lifecycle tools that come *after* the `create_*` builders have
populated the session model with nodes and edges.

| Tool | FactorySimPy call | Description |
|---|---|---|
| `connect` | `edge.connect(src, dest)` | Wires a single edge between two nodes. Params: `edge_id`, `src_id`, `dest_id`. Looks all three up in the session model, then calls the edge's `connect`, which appends the edge to `src.out_edges` and `dest.in_edges`. Edges are 1-src→1-dest; a node accumulates several in/out edges by being the endpoint of several `connect` calls. Rejects unknown ids and an already-connected edge (FactorySimPy raises unless `reconnect=True`). |
| `get_model` | — (reads the session model) | Returns a JSON-serializable snapshot of the current graph: each node's `id`/`type`/`in_edges`count/`out_edges`count and each edge's `id`/`type`/`src`/`dest`. Read-only; never exposes `env`/`simpy` objects. Mirrors `simgen.model.get_model()` but flattens to a summary. |
| `reset_model` | `simgen.model.reset_model()` | Discards the whole session graph and starts a fresh, empty model — drops every node and edge **and** restarts the clock at 0 (a new `simpy.Environment`). Recovers a dirty session: leftover components, an orphaned node that can't be wired (there is no single-node delete), or a clock already past the desired `until`. No params; returns `cleared_nodes`/`cleared_edges`/`now`. Always targets the session singleton, so — unlike the others — it takes no `model` kwarg. |
| `run_simulation` | `env.run(until=...)` | Executes the scheduled `behaviour()` processes up to `until`. Params: `until` (positive number, simulation end time). Returns a small summary (e.g. `until`, per-node/edge stats like items received). |

## Conventions for the simulation tools

- **Resolve against the shared model.** Every tool takes `model` as an optional
  keyword (defaulting to `get_model()`), exactly like the builders, so tests can
  pass an isolated model.
- **Fail with friendly `ValueError`s, not raw lookups.** `connect` must check
  `model.has_node(src_id)` / `dest_id` and that `edge_id` exists in
  `model.edges` before touching FactorySimPy, so a typo'd id is a clear error
  rather than a `KeyError`.
- **Cardinality is a run-time property.** FactorySimPy validates edge cardinality
  inside each node's `behaviour()` generator, which only runs under `env.run()`:
    - Source: no in_edge, ≥1 out_edge
    - Sink: ≥1 in_edge, no out_edge
    - Machine / Splitter / Combiner: ≥1 in_edge and ≥1 out_edge

  So `connect` itself does **not** enforce these; a half-wired graph connects
  fine and only blows up at `run_simulation`. `validate_model` exists to surface
  these (plus edge-selection index ranges) as friendly errors *before* the run.