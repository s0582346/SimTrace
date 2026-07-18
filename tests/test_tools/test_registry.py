"""The registry wires builders onto a FastMCP server."""

import asyncio

from mcp.server.fastmcp import FastMCP

from simtrace.tools.registry import register_tools


def test_register_tools_exposes_create_source():
    mcp = FastMCP("test")
    register_tools(mcp)

    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}

    assert "create_source" in by_name
    schema = by_name["create_source"].inputSchema
    assert schema["required"] == ["id"]
    assert "inter_arrival_time" in schema["properties"]


def test_register_tools_exposes_create_sink():
    mcp = FastMCP("test")
    register_tools(mcp)

    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}

    assert "create_sink" in by_name
    schema = by_name["create_sink"].inputSchema
    assert schema["required"] == ["id"]
    assert "node_setup_time" in schema["properties"]


def test_register_tools_exposes_create_machine():
    mcp = FastMCP("test")
    register_tools(mcp)

    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}

    assert "create_machine" in by_name
    schema = by_name["create_machine"].inputSchema
    assert schema["required"] == ["id"]
    assert "work_capacity" in schema["properties"]
    assert "processing_delay" in schema["properties"]


def test_register_tools_exposes_create_splitter():
    mcp = FastMCP("test")
    register_tools(mcp)

    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}

    assert "create_splitter" in by_name
    schema = by_name["create_splitter"].inputSchema
    assert schema["required"] == ["id"]
    assert "mode" in schema["properties"]
    assert "split_quantity" in schema["properties"]


def test_register_tools_exposes_create_combiner():
    mcp = FastMCP("test")
    register_tools(mcp)

    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}

    assert "create_combiner" in by_name
    schema = by_name["create_combiner"].inputSchema
    assert schema["required"] == ["id"]
    assert "target_quantity_of_each_item" in schema["properties"]


def test_register_tools_exposes_create_buffer():
    mcp = FastMCP("test")
    register_tools(mcp)

    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}

    assert "create_buffer" in by_name
    schema = by_name["create_buffer"].inputSchema
    assert schema["required"] == ["id"]
    assert "capacity" in schema["properties"]
    assert "mode" in schema["properties"]


def test_register_tools_exposes_create_conveyor():
    mcp = FastMCP("test")
    register_tools(mcp)

    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}

    assert "create_conveyor" in by_name
    schema = by_name["create_conveyor"].inputSchema
    # conveyor_length/speed/item_length have no defaults — required alongside id.
    assert set(schema["required"]) == {"id", "conveyor_length", "speed", "item_length"}
    assert "accumulating" in schema["properties"]


def test_register_tools_exposes_create_fleet():
    mcp = FastMCP("test")
    register_tools(mcp)

    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}

    assert "create_fleet" in by_name
    schema = by_name["create_fleet"].inputSchema
    assert schema["required"] == ["id"]
    assert "capacity" in schema["properties"]
    assert "transit_delay" in schema["properties"]


def test_register_tools_exposes_connect():
    mcp = FastMCP("test")
    register_tools(mcp)

    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}

    assert "connect" in by_name
    schema = by_name["connect"].inputSchema
    assert set(schema["required"]) == {"edge_id", "src_id", "dest_id"}


def test_register_tools_exposes_get_model():
    mcp = FastMCP("test")
    register_tools(mcp)

    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}

    assert "get_model" in by_name
    # No required inputs — reads the session model.
    assert by_name["get_model"].inputSchema.get("required", []) == []


def test_register_tools_exposes_run_simulation():
    mcp = FastMCP("test")
    register_tools(mcp)

    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}

    assert "run_simulation" in by_name
    schema = by_name["run_simulation"].inputSchema
    assert schema["required"] == ["until"]


def test_server_exposes_modeling_instructions():
    # The session-level guide is the only channel telling an MCP client the
    # structural idioms (one Source per type, connect order = priority, ...).
    from simtrace.server import mcp as server_mcp

    instructions = server_mcp.instructions
    assert instructions
    assert "one Source per type" in instructions
    assert "connect order" in instructions
    assert "NO type/attributes" in instructions


def test_tool_descriptions_carry_decision_point_hints():
    mcp = FastMCP("test")
    register_tools(mcp)

    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}

    # create_source: product mix -> one Source per type, mean / fraction.
    assert "one Source per type" in by_name["create_source"].description
    # connect: wiring order at a shared station encodes priority.
    assert "Connect order matters" in by_name["connect"].description
