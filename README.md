# simgen

Traceable, tool-building DES toolkit for SimPy models, exposed over MCP.

simgen exposes [FactorySimPy](https://github.com/FactorySimPy/FactorySimPy)
discrete-event simulation primitives as **MCP tools** so an LLM agent can build a
factory model step by step — create nodes, create edges, wire them together, and
run the simulation — with every tool call traced via OpenTelemetry.

The server speaks the open [MCP](https://modelcontextprotocol.io) protocol over
stdio and is **client-agnostic** — any MCP client can drive it (Claude Desktop,
other agent frameworks, or a custom client). It contains no model-provider code
itself; a Claude-driven agent loop is the intended bundled driver, but it is not
required to use the tools.

## Tools

| Group | Tools | Module |
|---|---|---|
| Nodes (active) | `create_source`, `create_sink`, `create_machine`, `create_splitter`, `create_combiner` | `simgen.tools.nodes` |
| Edges (passive) | `create_buffer`, `create_conveyor`, `create_fleet` | `simgen.tools.edges` |
| Lifecycle | `connect`, `get_model`, `run_simulation` | `simgen.tools.simulation` |

Design notes live in [`architecture/`](architecture/) (`node_tools.md`,
`edge_tools.md`, `simulation_tools.md`, `observability.md`).

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

This installs all dependencies, including the vendored FactorySimPy checkout
under `vendor/FactorySimPy`.

## Running the tests

```bash
uv run pytest
```

## Running the MCP server

```bash
uv run python -m simgen.server
```

The server speaks MCP over stdio, so it is normally launched by an MCP client
(see below) rather than run by hand.

> Note: the `simgen` console script in `pyproject.toml` is not currently wired
> up — use `python -m simgen.server`.

## Connecting to Claude Desktop

1. Make sure dependencies are installed (`uv sync`).
2. Edit the Claude Desktop config:
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
3. Add the server, pointing at the project's venv Python (most robust — no
   reliance on `uv` being on the client's PATH):

   ```json
   {
     "mcpServers": {
       "simgen": {
         "command": "C:\\local\\htw\\simgen\\.venv\\Scripts\\python.exe",
         "args": ["-m", "simgen.server"]
       }
     }
   }
   ```

   On macOS/Linux use `.venv/bin/python` instead.
4. Fully restart Claude Desktop (quit from the tray/menu bar, not just the
   window).
5. In a chat, the `simgen` tools appear under the tools icon. Try:
   *"create a source S1, a sink Sink1, a buffer B1 (capacity 4), connect B1 from
   S1 to Sink1, then run the simulation until 10."*

## Observability (OpenTelemetry + Jaeger)

Every tool call becomes an OpenTelemetry span, exported over OTLP/HTTP to a local
Jaeger instance.

1. Start Jaeger:

   ```bash
   docker compose up -d
   ```

2. Generate some spans without needing an MCP client — the smoke script invokes
   the real tools end to end (build a `source -> buffer -> sink` line, run it,
   and trigger one error span):

   ```bash
   uv run python scripts/trace_smoke.py
   ```

   It runs fine even if Jaeger is down (spans are just dropped), so it also
   doubles as a quick check that the tool path works.

3. View traces at <http://localhost:16686> → Service **simgen** → *Find Traces*.

When the server is driven by Claude Desktop, spans are exported the same way —
just keep Jaeger running. See [`architecture/observability.md`](architecture/observability.md)
for the design and configuration details (e.g. `OTEL_EXPORTER_OTLP_ENDPOINT`).
