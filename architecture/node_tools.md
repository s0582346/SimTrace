# Node Tools

MCP tools that create **FactorySimPy Node** instances ( Source, Machine, Sink, Splitter, Combiner) and register them in
a shared, session-scoped model. Edge tools (Buffer, Conveyor, Fleet),
`connect`, `get_model`, `validate_model`, and `run_simulation` are a later
phase and out of scope here.

All five builders are implemented in `src/simtrace/tools/nodes.py`, wrapped as
MCP tools in `src/simtrace/tools/registry.py`, and covered by tests in
`tests/test_tools/`.

| Tool | FactorySimPy class | Description |
|---|---|---|
| `create_source` | `Source` | Generates flow items into the model. 0 in_edges, 1 out_edge. `inter_arrival_time` must be non-zero when `blocking=False`. |
| `create_sink` | `Sink` | Terminal node that collects flow items. No out_edge param; needs ≥1 in_edge. |
| `create_machine` | `Machine` | Processes items. Needs ≥1 in_edge and ≥1 out_edge. `blocking` defaults to `True`. |
| `create_splitter` | `Splitter` | Turns one incoming item into many. Needs ≥1 in_edge and ≥1 out_edge. `split_quantity` required (positive int) when `mode="SPLIT"`, ignored for `UNPACK`. |
| `create_combiner` | `Combiner` | Packs items from several in_edges into a container (a pallet pulled from the first in_edge). No `in_edge_selection` — draws from all in_edges by design; the first in_edge must supply `Pallet` items at run time. |

## Conventions for every `create_*` tool

- Reject duplicate `id` (check against `model.nodes`).
- Always pass `env=model.env`; never expose `env`/`simpy` objects in tool results.
- `in_edges`/`out_edges` always start empty — wiring happens later via `connect`.
- Return a small JSON-serializable summary (`id`, `type`, echoed params).
- Validation of constant params (numbers, edge-selection strings, counts) lives
  in `src/simtrace/tools/utils.py` (`require_number`) and inline in each builder.
- Delay params (`inter_arrival_time`, `processing_delay`, etc.) accept a
  **constant or a distribution string**, parsed by
  `src/simtrace/tools/distributions.py`. The summary echoes the original spec
  string, not the callable. `node_setup_time` stays constant-only.
- Selection params (`in_edge_selection`, `out_edge_selection`) accept a strategy
  string only.
- Note: instantiating a Node subclass immediately calls
  `env.process(self.behaviour())`. This only *schedules* the process — the
  generator body (with its in/out-edge assertions) doesn't run until
  `env.run()`, so it's safe at tool-call time before the graph is fully wired.
