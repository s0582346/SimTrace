"""Tests for the validate_model checks that read the numbers and calendars."""

import pytest

from simtrace.model import FactoryModel
from simtrace.tools.builders import (
    create_buffer,
    create_machine,
    create_shift_calendar,
    create_sink,
    create_source,
)
from simtrace.tools.simulation import connect
from simtrace.tools.validation import validate_model


@pytest.fixture
def model() -> FactoryModel:
    return FactoryModel()


def warnings(model, check):
    return [f for f in validate_model(model=model)["warnings"] if f["check"] == check]


def line(model, iat=4, processing=2, source=None, machine=None, buffer=None):
    """src -> b1 -> mac -> b2 -> snk with the given settings."""
    create_source("src", inter_arrival_time=iat, blocking=True, model=model,
                  **(source or {}))
    create_machine("mac", processing_delay=processing, model=model, **(machine or {}))
    create_sink("snk", model=model)
    create_buffer("b1", capacity=5, model=model, **(buffer or {}))
    create_buffer("b2", capacity=1, model=model)
    connect("b1", "src", "mac", model=model)
    connect("b2", "mac", "snk", model=model)
    return model


# --- station_overload -----------------------------------------------------


def test_machine_slower_than_arrivals_is_overloaded(model):
    found = warnings(line(model, iat="exp(3)", processing="uniform(4, 6)"), "station_overload")
    assert [f["component"] for f in found] == ["mac"]
    assert found[0]["message"] == "Machine 'mac' gets more parts than it can finish (utilization 167%)."
    details = found[0]["details"]
    assert details["effective_process_time"] == pytest.approx(5)
    assert details["interarrival_time"] == pytest.approx(3)
    assert details["utilization"] == pytest.approx(5 / 3)


def test_machine_faster_than_arrivals_is_quiet(model):
    assert warnings(line(model, iat=4, processing=2), "station_overload") == []


def test_breakdowns_count_into_the_load(model):
    # 2 per part at 50% availability is 4 per part, as fast as the arrivals.
    line(model, iat=4, processing=2,
         machine={"time_to_failure": "exp(20)", "repair_time": "exp(20)"})
    found = warnings(model, "station_overload")
    assert found[0]["details"]["effective_process_time"] == pytest.approx(4)
    assert found[0]["details"]["utilization"] == pytest.approx(1)


def test_parallel_machines_share_the_flow(model):
    create_source("src", inter_arrival_time=3, blocking=True, model=model)
    create_machine("split", processing_delay=0, out_edge_selection="ROUND_ROBIN", model=model)
    create_machine("m1", processing_delay=5, model=model)
    create_machine("m2", processing_delay=5, model=model)
    create_sink("snk", model=model)
    for edge_id in ("b_in", "b1", "b2", "o1", "o2"):
        create_buffer(edge_id, capacity=5, model=model)
    connect("b_in", "src", "split", model=model)
    connect("b1", "split", "m1", model=model)
    connect("b2", "split", "m2", model=model)
    connect("o1", "m1", "snk", model=model)
    connect("o2", "m2", "snk", model=model)
    # Each machine gets one part every 6 and needs 5.
    assert warnings(model, "station_overload") == []


def test_overloaded_machine_passes_on_only_what_it_finishes(model):
    create_source("src", inter_arrival_time=1, blocking=True, model=model)
    create_machine("slow", processing_delay=4, model=model)
    create_machine("next", processing_delay=3, model=model)
    create_sink("snk", model=model)
    for edge_id in ("b1", "b2", "b3"):
        create_buffer(edge_id, capacity=5, model=model)
    connect("b1", "src", "slow", model=model)
    connect("b2", "slow", "next", model=model)
    connect("b3", "next", "snk", model=model)
    assert [f["component"] for f in warnings(model, "station_overload")] == ["slow"]


# --- buffer_delay_capacity ------------------------------------------------


def test_short_buffer_with_delay_is_warned(model):
    # One part every 4, each stays 40, so about 10 places are needed.
    line(model, iat=4, processing=2, buffer={"delay": 40})
    found = warnings(model, "buffer_delay_capacity")
    assert [f["component"] for f in found] == ["b1"]
    assert "about 10.0 places" in found[0]["message"]


def test_large_enough_buffer_with_delay_is_quiet(model):
    line(model, iat=4, processing=2, buffer={"delay": 8})
    assert warnings(model, "buffer_delay_capacity") == []


# --- calendars --------------------------------------------------------------


def test_source_delivering_while_machine_is_off(model):
    create_shift_calendar("day", 1440, [[360, 1320]], model=model)
    line(model, machine={"calendar": "day"})
    found = warnings(model, "calendar_mismatch")
    assert [f["component"] for f in found] == ["src"]
    assert "about 480 time units" in found[0]["message"]
    assert "'src' waits" in found[0]["message"]


def test_non_blocking_source_throws_parts_away(model):
    create_shift_calendar("day", 1440, [[360, 1320]], model=model)
    create_source("src", inter_arrival_time=4, model=model)
    create_machine("mac", processing_delay=2, calendar="day", model=model)
    create_sink("snk", model=model)
    create_buffer("b1", capacity=5, model=model)
    create_buffer("b2", capacity=1, model=model)
    connect("b1", "src", "mac", model=model)
    connect("b2", "mac", "snk", model=model)
    assert "throws its parts away" in warnings(model, "calendar_mismatch")[0]["message"]


def test_machine_working_longer_than_the_source_is_quiet(model):
    create_shift_calendar("shift", 1440, [[360, 540], [555, 1320]], default=True, model=model)
    create_shift_calendar("long", 1440, [[360, 1320]], model=model)
    line(model, machine={"calendar": "long"})
    assert warnings(model, "calendar_mismatch") == []


def test_unused_calendar(model):
    create_shift_calendar("day", 1440, [[360, 1320]], model=model)
    create_shift_calendar("spare", 1440, [[0, 720]], model=model)
    line(model, machine={"calendar": "day"}, source={"calendar": "day"})
    assert [f["component"] for f in warnings(model, "unused_calendar")] == ["spare"]


def test_default_calendar_counts_as_used(model):
    create_shift_calendar("day", 1440, [[360, 1320]], default=True, model=model)
    line(model)
    assert warnings(model, "unused_calendar") == []


# --- breakdowns and distributions -----------------------------------------


def test_swapped_breakdown_times(model):
    line(model, machine={"time_to_failure": "exp(20)", "repair_time": "exp(580)"})
    found = warnings(model, "low_availability")
    assert [f["component"] for f in found] == ["mac"]
    assert "97%" in found[0]["message"]


def test_normal_breakdown_times_are_quiet(model):
    line(model, machine={"time_to_failure": "exp(580)", "repair_time": "exp(20)"})
    assert warnings(model, "low_availability") == []


def test_wide_normal_is_warned(model):
    line(model, processing="normal(2, 1.5)")
    found = warnings(model, "normal_clamping")
    assert [f["component"] for f in found] == ["mac"]
    assert "about 9%" in found[0]["message"]


def test_narrow_normal_is_quiet(model):
    line(model, processing="normal(8, 1)")
    assert warnings(model, "normal_clamping") == []


# --- no_effect_setting ----------------------------------------------------


def test_round_robin_over_one_edge(model):
    line(model, machine={"out_edge_selection": "ROUND_ROBIN"})
    found = warnings(model, "no_effect_setting")
    assert [f["component"] for f in found] == ["mac"]
    assert "only 1 out-edge" in found[0]["message"]


def test_round_robin_over_two_edges_is_quiet(model):
    create_source("src", inter_arrival_time=4, blocking=True, model=model)
    create_machine("mac", processing_delay=0, out_edge_selection="ROUND_ROBIN", model=model)
    create_sink("snk", model=model)
    for edge_id in ("b_in", "o1", "o2"):
        create_buffer(edge_id, capacity=5, model=model)
    connect("b_in", "src", "mac", model=model)
    connect("o1", "mac", "snk", model=model)
    connect("o2", "mac", "snk", model=model)
    assert warnings(model, "no_effect_setting") == []


# --- part_type_dead_end ---------------------------------------------------


def test_part_type_routed_only_to_scrap(model):
    create_source("src_a", inter_arrival_time=5, part_type="a",
                  attributes={"family": "A"}, blocking=True, model=model)
    create_source("src_b", inter_arrival_time=5, part_type="b",
                  attributes={"family": "B"}, blocking=True, model=model)
    create_machine("m", processing_delay=1, route_by="family",
                   routes={"A": "b_good", "*": "b_scrap"}, model=model)
    create_sink("good", model=model)
    create_sink("bin", scrap=True, model=model)
    for edge_id in ("b_a", "b_b", "b_good", "b_scrap"):
        create_buffer(edge_id, capacity=3, model=model)
    connect("b_a", "src_a", "m", model=model)
    connect("b_b", "src_b", "m", model=model)
    connect("b_good", "m", "good", model=model)
    connect("b_scrap", "m", "bin", model=model)
    found = warnings(model, "part_type_dead_end")
    assert [f["component"] for f in found] == ["src_b"]
    assert "'b' parts" in found[0]["message"]
