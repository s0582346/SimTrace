"""Tests for strict_units: units in the tool arguments."""

import pytest
from mcp.server.fastmcp import FastMCP

from simtrace.model import configure_units, get_model, reset_model
from simtrace.tools.registry import register_tools


@pytest.fixture
def tools():
    configure_units(True)
    reset_model()
    mcp = register_tools(FastMCP("test"))
    found = {tool.name: tool.fn for tool in mcp._tool_manager.list_tools()}
    yield found
    configure_units(False)
    reset_model()


def line(tools, speed="20 parts/min * 0.3 m", arrival="exp(5 min)"):
    tools["reset_model"]()
    tools["create_source"](id="src", inter_arrival_time=arrival, blocking=True,
                           item_length="0.3 m")
    tools["create_buffer"](id="b_in", capacity=5)
    tools["create_machine"](id="cnc", processing_delay="uniform(3 min, 4 min)")
    tools["create_conveyor"](id="belt", conveyor_length="6 m", speed=speed,
                             item_length="300 mm")
    tools["create_machine"](id="test", processing_delay="uniform(2 min, 3 min)")
    tools["create_buffer"](id="b_out", capacity=5)
    tools["create_sink"](id="store")
    tools["connect"](edge_id="b_in", src_id="src", dest_id="cnc")
    tools["connect"](edge_id="belt", src_id="cnc", dest_id="test")
    tools["connect"](edge_id="b_out", src_id="test", dest_id="store")


def kwargs_of(component):
    for step in get_model().spec:
        if step["kwargs"].get("id") == component:
            return step["kwargs"]
    raise KeyError(component)


def test_arguments_are_converted_into_model_units(tools):
    line(tools)
    assert kwargs_of("belt")["speed"] == pytest.approx(6.0)
    assert kwargs_of("belt")["item_length"] == pytest.approx(0.3)
    assert kwargs_of("cnc")["processing_delay"] == "uniform(3.0, 4.0)"
    assert get_model().written["belt"]["speed"] == "20 parts/min * 0.3 m"


def test_a_rate_for_a_speed_is_refused_and_nothing_is_built(tools):
    with pytest.raises(ValueError, match=r"is \[1/time\], the setting needs \[length/time\]\.$"):
        line(tools, speed="20 parts/min")
    assert not get_model().has_edge("belt")


def test_a_rate_for_a_time_is_refused_but_its_inversion_is_accepted(tools):
    with pytest.raises(ValueError, match=r"\[1/time\], the setting needs \[time\]"):
        line(tools, arrival="exp(12 parts/h)")
    line(tools, arrival="exp(1 / (12 parts/h))")
    assert kwargs_of("src")["inter_arrival_time"] == "exp(5.0)"


def test_a_bare_number_is_refused_but_counts_and_zero_are_not(tools):
    tools["reset_model"]()
    with pytest.raises(ValueError, match="has no unit"):
        tools["create_machine"](id="m", processing_delay=3)
    tools["create_machine"](id="m", processing_delay=0, work_capacity=2)
    tools["create_buffer"](id="b", capacity=5)


def test_until_needs_a_unit_and_the_result_names_the_time_unit(tools):
    line(tools)
    with pytest.raises(ValueError, match="has no unit"):
        tools["run_simulation"](until=60, seed=1)
    reset_model()
    line(tools)
    result = tools["run_simulation"](until="1 h", seed=1)
    assert result["time_unit"] == "min"


def test_reset_keeps_the_server_setting_and_the_agent_cannot_change_it(tools):
    assert tools["reset_model"]()["time_unit"] == "min"
    assert get_model().strict_units
    with pytest.raises(TypeError):
        tools["reset_model"](strict_units=False)


def test_server_reads_the_setting_from_the_environment(monkeypatch):
    from simtrace import server
    monkeypatch.setenv("SIMTRACE_STRICT_UNITS", "1")
    monkeypatch.setenv("SIMTRACE_TIME_UNIT", "s")
    settings = server._units_from_env()
    assert settings == {"strict_units": True, "time_unit": "s", "length_unit": "m"}
    text = server._instructions(settings)
    assert text.index("Units: every time") < text.index("Modeling patterns:")
    assert "Results are in s and m." in text
    monkeypatch.setenv("SIMTRACE_STRICT_UNITS", "")
    assert "Units: every time" not in server._instructions(server._units_from_env())


def test_shift_windows_are_converted(tools):
    tools["reset_model"]()
    tools["create_shift_calendar"](id="shifts", cycle="24 h", windows=[["0 h", "8 h"]])
    step = kwargs_of("shifts")
    assert step["cycle"] == pytest.approx(1440)
    assert step["windows"] == [[pytest.approx(0), pytest.approx(480)]]


def test_without_the_flag_nothing_changes(tools):
    configure_units(False)
    tools["reset_model"]()
    tools["create_machine"](id="m", processing_delay="uniform(3, 4)")
    assert kwargs_of("m")["processing_delay"] == "uniform(3, 4)"
    with pytest.raises(ValueError):
        tools["create_conveyor"](id="belt", conveyor_length=6, speed="6 m/min", item_length=0.3)


def test_quote_marks_around_a_setting_are_dropped(tools):
    tools["reset_model"]()
    tools["create_machine"](id="m", processing_delay='"uniform(3 min, 4 min)"')
    assert kwargs_of("m")["processing_delay"] == "uniform(3.0, 4.0)"
    configure_units(False)
    tools["reset_model"]()
    tools["create_machine"](id="m", processing_delay='"uniform(3, 4)"')


def test_descriptions_name_the_unit_rule_only_when_strict():
    configure_units(True)
    strict = {t.name: t.description for t in register_tools(FastMCP("a"))._tool_manager.list_tools()}
    configure_units(False)
    loose = {t.name: t.description for t in register_tools(FastMCP("b"))._tool_manager.list_tools()}
    assert "needs the unit on every time" in strict["create_machine"]
    assert 'needs the unit on `until`' in strict["run_simulation"]
    assert "Units: this server" not in loose["create_machine"]
    assert "Units: this server" not in strict["connect"]
