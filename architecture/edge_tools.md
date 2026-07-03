# Edge Tools

MCP tools that create **FactorySimPy Edge** instances (Buffer, Conveyor, Fleet)
and register them in the shared, session-scoped model. Edges *hold and move* flow
items between exactly two nodes, wired later via `connect`
(see `architecture/simulation_tools.md`).

| Tool | FactorySimPy class | Description |
|---|---|---|
| `create_buffer` | `Buffer` (`edges.buffer`) | FIFO/LIFO queue holding items until the destination accepts them. Params: `id`, `capacity` (positive int), `delay` (time before a put item becomes gettable), `mode` (`FIFO`/`LIFO`). |
| `create_conveyor` | `ConveyorBelt` (`edges.continuous_conveyor`) | Moving belt; travel time derives from length/speed. Params: `id`, `conveyor_length` (positive), `speed` (positive), `item_length` (positive — should match the upstream Source's `item_length`), `accumulating` (bool — items bunch up when the belt stalls vs. block). `capacity` is **derived** (`int(ceil(conveyor_length)/item_length)`), not a param, and must be ≥ 1. |
| `create_fleet` | `Fleet` (`edges.fleet`) | Transporters/AGVs moving up to `capacity` items per trip. Params: `id`, `capacity` (positive int, items per trip), `delay` (wait before departing under capacity), `transit_delay` (src→dest travel time). |

Every edge holds exactly 1 src + 1 dest, both `None` until `connect`.

## Conventions for every `create_*` edge tool

- Reject duplicate `id`. Ids are **global across nodes and edges** — check both
  `model.nodes` and `model.edges`.
- `src_node`/`dest_node` start `None`; `connect` sets them and appends the edge
  to the nodes' edge lists.
- Return a small JSON-serializable summary (`id`, `type`, echoed params; for
  conveyor, include the *derived* `capacity`).
- Constant-param validation lives in `src/simtrace/tools/utils.py`:
  `require_unique_id`, `require_positive_int` (capacity), `require_positive_number`
  (conveyor `conveyor_length`/`speed`/`item_length`), `require_number` (delays).
- Delay params (`delay`, `transit_delay`) accept a **constant or a distribution
  string**, parsed by `src/simtrace/tools/distributions.py`. Same policy as node
  builders.
- Scheduled processes: constructing a `ConveyorBelt` schedules its belt-movement
  generator via `env.process` (runs at `env.run()`); `Buffer` and `Fleet` stay
  inert until connected. Creating an edge before wiring is safe either way.
