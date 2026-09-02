# Simulation Tools

MCP tools that operate on the **assembled model as a whole** — wiring nodes
together with edges, inspecting the current graph, and running the simulation.
These are the lifecycle tools that come *after* the `create_*` builders have
populated the session model with nodes and edges.

| Tool | FactorySimPy call | Description |
|---|---|---|
| `connect` | `edge.connect(src, dest)` | Wires a single edge between two nodes. Params: `edge_id`, `src_id`, `dest_id`. Looks all three up in the session model, then calls the edge's `connect`, which appends the edge to `src.out_edges` and `dest.in_edges`. Edges are 1-src→1-dest; a node accumulates several in/out edges by being the endpoint of several `connect` calls. Rejects unknown ids and an already-connected edge (FactorySimPy raises unless `reconnect=True`). |
| `get_model` | — (reads the session model) | Returns a JSON-serializable snapshot of the current graph: each node's `id`/`type`/`in_edges`count/`out_edges`count and each edge's `id`/`type`/`src`/`dest`. Read-only; never exposes `env`/`simpy` objects. Mirrors `simtrace.model.get_model()` but flattens to a summary. |
| `reset_model` | `simtrace.model.reset_model()` | Discards the whole session graph and starts a fresh, empty model — drops every node and edge **and** restarts the clock at 0 (a new `simpy.Environment`). Recovers a dirty session: leftover components, an orphaned node that can't be wired (there is no single-node delete), or a clock already past the desired `until`. No params; returns `cleared_nodes`/`cleared_edges`/`now`. Always targets the session singleton, so — unlike the others — it takes no `model` kwarg. |
| `run_simulation` | `env.run(until=...)` | Executes the scheduled `behaviour()` processes up to `until`. Params: `until` (positive number, simulation end time). Returns a small summary (e.g. `until`, per-node/edge stats like items received). |
| `run_replications` | repeated `env.run()` on rebuilt models | Runs the model `replications` times from a clock at 0 and reports per-metric means with confidence intervals. Each run rebuilds the graph from the recorded build spec, so the runs are independent and the session model is only read. Params: `until`, `replications` (2..20 over MCP), `random_seed_base` — run i seeds with `random_seed_base + i * 1000`. Independent runs are spread over worker processes when timing the first one says it pays. Also returns a `precision` section: how few of the runs made would have sufficed. |
| `find_replication_count` | repeated `env.run()`, count decided while running | Answers *how many* runs are needed instead of taking a number. Keeps running until the confidence interval is within `desired_precision` of the mean and stays there for the look-ahead window, then reports the count where that first held. Params: `until`, `target_metrics` (default: every sink's throughput), `desired_precision` (default 0.10), `replication_budget` (2..20 over MCP), `random_seed_base`. Returns `replications_needed` per metric, `converged`, `unresolved`, `runs_executed` and the per-run tables. |

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
  these as errors *before* the run.
## Choosing a replication count

A stochastic model gives a different answer every run, so a single
`run_simulation` is one sample. Two accepted methods decide how many samples are
enough, both from Hoad, Robinson & Davies (2010), *Automated selection of the
number of replications for a discrete-event simulation*, JORS 61(11). Both are
applied through the `sim-tools` package rather than reimplemented.

**The confidence interval method**, after the fact. Walk the runs in order,
keeping a running mean and a 95% interval, and take the first count whose
half-width sits within a target share of the mean. `run_replications` reports
this for every metric in its `precision` section, at no extra cost — the runs
have already been made. It is retrospective: it tells you the batch was long
enough, not that a shorter one would have been.

**The replications algorithm**, while running. `find_replication_count` keeps
adding runs until the target is met *and holds* for a look-ahead window of five
further runs. The window is the point of the method: a running interval can dip
below the target on one lucky run and climb back out on the next, and stopping
at the dip reports a precision the model does not have.
