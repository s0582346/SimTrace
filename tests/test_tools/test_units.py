"""Tests for strict_units: units in the tool arguments."""

import pytest
from mcp.server.fastmcp import FastMCP

from simtrace.model import configure_units, get_model, reset_model
from simtrace.tools.registry import register_tools
from simtrace.tools.units import read


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


# --- planted errors: one setting of each dimension, given a wrong one ---------

# (tool, id, setting, right argument, wrong arguments, dimension needed).
# Every other argument of the call is right, so only the planted one can fail.
PLANTED = [
    ("create_machine", "m", "processing_delay", "uniform(3 min, 4 min)",
     ["uniform(3 m, 4 m)", "3 parts/min", "3 m/min", "3 parts"], "time"),
    ("create_source", "s", "item_length", "0.3 m",
     ["0.3 min", "0.3 m/s", "3 parts/h", "3 parts"], "length"),
    ("create_conveyor", "c", "speed", "6 m/min",
     ["6 m", "6 min", "20 parts/min", "6 parts"], "length/time"),
]
OTHER = {
    "create_machine": {},
    "create_source": {"inter_arrival_time": "exp(5 min)"},
    "create_conveyor": {"conveyor_length": "6 m", "item_length": "0.3 m"},
}


def planted_cases():
    for tool, id_, setting, right, wrongs, needs in PLANTED:
        for wrong in wrongs:
            yield pytest.param(tool, id_, setting, right, wrong, needs,
                               id=f"{setting}={wrong}")


@pytest.mark.parametrize("tool, id_, setting, right, wrong, needs", list(planted_cases()))
def test_a_planted_wrong_dimension_is_refused_and_the_right_one_is_built(
        tools, tool, id_, setting, right, wrong, needs):
    tools["reset_model"]()
    with pytest.raises(ValueError, match=rf"the setting needs \[{needs}\]\.$"):
        tools[tool](id=id_, **OTHER[tool], **{setting: wrong})
    assert not get_model().has_node(id_) and not get_model().has_edge(id_)
    tools[tool](id=id_, **OTHER[tool], **{setting: right})


@pytest.mark.parametrize("wrong", ["8 h", "8 m", "3 parts/h"])
def test_a_count_takes_no_unit(tools, wrong):
    tools["reset_model"]()
    with pytest.raises(ValueError):
        tools["create_buffer"](id="b", capacity=wrong)


@pytest.mark.parametrize("wrong", ["10 m", "12 parts/h", "1 m/min"])
def test_a_planted_wrong_run_length_is_refused(tools, wrong):
    line(tools)
    with pytest.raises(ValueError, match=r"the setting needs \[time\]\.$"):
        tools["run_simulation"](until=wrong, seed=1)


# --- reading arguments -------------------------------------------------------

@pytest.mark.parametrize("text, dim, value", [
    ("5 * 8 h", (1, 0), 5 * 8 * 3600),
    ("2 h 30 min", (1, 0), 9000),
    ("1 h 30 min 15 s", (1, 0), 5415),
    ("2 h 30 min / 2", (1, 0), 7200 + 900),
    ("1 m 20 cm", (0, 1), 1.2),
    ("12 parts/h", (-1, 0), 12 / 3600),
    ("12 parts per hour", (-1, 0), 12 / 3600),
    ("3 per 8 h", (-1, 0), 3 / (8 * 3600)),
    ("0.1 m/s", (-1, 1), 0.1),
    ("6 m / 1 min", (-1, 1), 6 / 60),
    ("2 m·s⁻¹", (-1, 1), 2),
    ("1 m/s²", (-2, 1), 1),
    ("1 / (12 parts/h)", (1, 0), 300),
    ("5 Min", (1, 0), 300),
])
def test_read_units(text, dim, value):
    [quantity] = read(text).args
    assert quantity.dim == dim
    assert quantity.value == pytest.approx(value)


@pytest.mark.parametrize("text, kind, values", [
    ("Uniform(3 min, 4 min)", "uniform", [180, 240]),
    ("EXP(5 min)", "exp", [300]),
    ("Gauss(8 min, 3 min)", "normal", [480, 180]),
    ("exp(3 failures per 8 h)", "exp", [3 / (8 * 3600)]),
])
def test_read_distributions(text, kind, values):
    expression = read(text)
    assert expression.kind == kind
    assert [q.value for q in expression.args] == pytest.approx(values)


@pytest.mark.parametrize("text", ["2 h 30", "2 m + 3 s"])
def test_a_sum_of_two_dimensions_cannot_be_read(text):
    with pytest.raises(ValueError, match="sum"):
        read(text)


def test_mixed_dimensions_in_a_distribution_are_refused(tools):
    tools["reset_model"]()
    with pytest.raises(ValueError, match=r"is \[length\], the setting needs \[time\]"):
        tools["create_machine"](id="m", processing_delay="uniform(2 min, 3 m)")


def test_descriptions_name_the_unit_rule_only_when_strict():
    configure_units(True)
    strict = {t.name: t.description for t in register_tools(FastMCP("a"))._tool_manager.list_tools()}
    configure_units(False)
    loose = {t.name: t.description for t in register_tools(FastMCP("b"))._tool_manager.list_tools()}
    assert "needs the unit on every time" in strict["create_machine"]
    assert 'needs the unit on `until`' in strict["run_simulation"]
    assert "Units: this server" not in loose["create_machine"]
    assert "Units: this server" not in strict["connect"]
