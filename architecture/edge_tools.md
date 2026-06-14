# Edge Tools

MCP tools that create **FactorySimPy Edge** instances (e.g. Buffer, Conveyor, Fleet) and register them in the shared,
session-scoped model. Edges *hold and move* flow items between exactly two
nodes; they do no work themselves. They are wired to nodes later via `connect`
(see `architecture/simulation_tools.md`).

| Tool | FactorySimPy class | Description |
|---|---|---|
| `create_buffer` | `Buffer` (`edges.buffer`) | A FIFO/LIFO queue holding items waiting to be accepted by the destination node. Params: `id`, `capacity` (positive int), `delay` (constant; time before a put item becomes available to get), `mode` (`FIFO`/`LIFO`). Holds exactly 1 src + 1 dest, both empty until `connect`. |
| `create_conveyor` | `ConveyorBelt` (`edges.continuous_conveyor`) | A moving belt; travel time derives from length/speed. Params: `id`, `conveyor_length` (positive number), `speed` (positive number), `item_length` (positive number — should match the `item_length` of items the upstream Source emits), `accumulating` (bool — whether items bunch up when the belt stalls vs. block). **`capacity` is derived** (`int(ceil(conveyor_length)/item_length)`), not a user param, and must come out ≥ 1. Holds 1 src + 1 dest. |
| `create_fleet` | `Fleet` (`edges.fleet`) | A group of transporters/AGVs that move up to `capacity` items at once between two nodes. Params: `id`, `capacity` (positive int — items moved per trip), `delay` (constant; wait before the fleet departs if it hasn't filled to capacity), `transit_delay` (constant; src→dest travel time). Holds 1 src + 1 dest. |

## Conventions for every `create_*` edge tool

- Reject duplicate `id`. Ids are **global across nodes and edges**, so check against
  *both* `model.nodes` and `model.edges`.
- `src_node`/`dest_node` always start `None` — wiring happens later via
  `connect`, which sets them and appends the edge to the nodes' edge lists.
- Return a small JSON-serializable summary (`id`, `type`, echoed params; for
  conveyor, include the *derived* `capacity` so the agent can see it).
- Validation of constant params lives in shared helpers in
  `src/simgen/tools/utils.py`: `require_unique_id` (non-empty, not already
  taken), `require_positive_int` (capacity), `require_positive_number`
  (conveyor `conveyor_length`/`speed`/`item_length`), and `require_number`
  (delays). Only checks with no reusable shape stay inline (the `mode` string,
  the `accumulating` bool, and the derived-capacity ≥ 1 guard). `capacity` must
  be a positive int (the `Edge` base asserts this); for the conveyor the derived
  capacity must come out ≥ 1 (guard against `item_length` larger than the belt,
  which would floor capacity to 0).
- Delay params (`delay`, `transit_delay`) accept int/float/generator/callable in
  FactorySimPy. **v1: constant int/float only**; generators/callables are a
  later extension. (Same policy as the node builders.)
- Note on scheduled processes: instantiating a `ConveyorBelt` immediately calls
  `env.process(self.behaviour())` (like a Node), so its belt-movement generator
  is scheduled at tool-call time but doesn't run until `env.run()`. `Buffer` and
  `Fleet` do **not** self-schedule a process at construction, so they are inert
  until connected and driven by their neighbouring nodes. Either way, creating an
  edge before the graph is wired is safe.