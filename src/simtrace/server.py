"""MCP server exposing the SimPy/FactorySimPy block-building tools.

Thin transport layer: it builds the FastMCP server and delegates all tool
registration to `simtrace.tools.registry`.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from simtrace.tools.registry import register_tools
from simtrace.tools.telemetry import configure_telemetry

mcp = FastMCP("simpy_blocks")
register_tools(mcp)


def main() -> None:
    configure_telemetry()
    mcp.run()


if __name__ == "__main__":
    main()
