"""Generate real tool spans and export them to Jaeger.

Invokes the registered (traced) MCP tools end-to-end, builds a tiny
source -> buffer -> sink line, runs it, then flushes spans so they reach the
collector before the process exits.

Usage:
    docker compose up -d                 # start Jaeger first
    uv run python scripts/trace_smoke.py
    # open http://localhost:16686 -> service "simgen"
"""

from __future__ import annotations

import asyncio

from opentelemetry import trace

from simgen.server import mcp
from simgen.tools.telemetry import configure_telemetry


async def _build_and_run() -> None:
    await mcp.call_tool("create_source", {"id": "S1", "inter_arrival_time": 1, "blocking": True})
    await mcp.call_tool("create_sink", {"id": "Sink1"})
    await mcp.call_tool("create_buffer", {"id": "B1", "capacity": 4})
    await mcp.call_tool("connect", {"edge_id": "B1", "src_id": "S1", "dest_id": "Sink1"})
    await mcp.call_tool("run_simulation", {"until": 10})
    await mcp.call_tool("get_model", {})
    # Also exercise the error path so an ERROR span shows up in the UI.
    try:
        await mcp.call_tool("create_buffer", {"id": "B1"})  # duplicate id
    except Exception:
        pass


def main() -> None:
    configure_telemetry()
    asyncio.run(_build_and_run())
    # BatchSpanProcessor batches; flush so spans export before we exit.
    trace.get_tracer_provider().force_flush()
    print("Done. Open http://localhost:16686 and pick service 'simgen'.")


if __name__ == "__main__":
    main()
