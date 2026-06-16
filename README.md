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
| Lifecycle | `connect`, `get_model`, `reset_model`, `run_simulation` | `simgen.tools.simulation` |

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

## Connecting an MCP client

The server is a standard stdio MCP server. Point any client at the project's
venv Python — replace `/path/to/simgen` below with your clone's absolute path
(on Windows, e.g. `C:\\Users\\you\\simgen` with `\\.venv\\Scripts\\python.exe`).

**Claude Desktop** — add to `claude_desktop_config.json`, then fully restart:

```json
{
  "mcpServers": {
    "simgen": {
      "command": "/path/to/simgen/.venv/bin/python",
      "args": ["-m", "simgen.server"]
    }
  }
}
```

**Claude Code** — from the cloned repo:

```bash
claude mcp add simgen -- ./.venv/bin/python -m simgen.server
```

The `simgen` tools then appear in the client (check with `/mcp` in Claude Code).

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
