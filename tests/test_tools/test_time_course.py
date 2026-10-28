"""Tests for verify_time_course."""

import contextlib
import io

import pytest

from simtrace.model import FactoryModel
from simtrace.tools.builders import (
    create_buffer,
    create_combiner,
    create_machine,
    create_shift_calendar,
    create_sink,
    create_source,
)
from simtrace.tools.simulation import connect, run_simulation
from simtrace.tools.simulation.report import render_report, report_view
from simtrace.tools.validation import verify_time_course


def rework_line(rework_first: bool) -> FactoryModel:
    """Machining and testing with 20 % rework back to machining over a rack of 3.

    Under FIRST_AVAILABLE the in-edge connected first is taken first. With the
    new parts first, the rework rack fills, testing blocks, then machining.
    """
    m = FactoryModel()
    create_source("parts", inter_arrival_time="exp(5)", blocking=True, model=m)
    create_machine("machining", processing_delay="uniform(3, 4)", model=m)
    create_machine("testing", processing_delay="uniform(2, 3)",
                   route_shares={"b_out": 0.8, "b_rework": 0.2}, model=m)
    create_sink("store", model=m)
    for edge_id, cap in (("b_in", 5), ("b_rework", 3), ("b_test", 5), ("b_out", 5)):
        create_buffer(edge_id, capacity=cap, model=m)
    inputs = [("b_rework", "testing", "machining"), ("b_in", "parts", "machining")]
    if not rework_first:
        inputs.reverse()
    for edge_id, src, dest in inputs + [("b_test", "machining", "testing"),
                                        ("b_out", "testing", "store")]:
        connect(edge_id, src, dest, model=m)
    return m


def shift_line(cycle: float, windows: list, testing_windows: list | None = None) -> FactoryModel:
    m = FactoryModel()
    create_shift_calendar("shift", cycle, windows, default=True, model=m)
    if testing_windows is not None:
        create_shift_calendar("testing_plan", cycle, testing_windows, model=m)
    create_source("parts", inter_arrival_time="exp(5)", blocking=True, model=m)
    create_machine("machining", processing_delay="uniform(3, 4)", model=m)
    kwargs = {"calendar": "testing_plan"} if testing_windows is not None else {}
    create_machine("testing", processing_delay="uniform(3.5, 4.5)", **kwargs, model=m)
    create_sink("store", model=m)
    for edge_id, src, dest in (("b_in", "parts", "machining"), ("b_test", "machining", "testing"),
                               ("b_out", "testing", "store")):
        create_buffer(edge_id, capacity=5, model=m)
        connect(edge_id, src, dest, model=m)
    return m


def pallet_line(per_pallet: int) -> FactoryModel:
    m = FactoryModel()
    create_source("parts", inter_arrival_time="exp(2)", blocking=True, model=m)
    create_source("pallets", inter_arrival_time=8, flow_item_type="pallet", blocking=True,
                  model=m)
    create_machine("checking", processing_delay="uniform(0.8, 1.2)", model=m)
    create_combiner("packing", target_quantity_of_each_item=[1, per_pallet],
                    processing_delay="uniform(0.5, 1.5)", model=m)
    create_sink("shipping", model=m)
    for edge_id, cap, src, dest in (("b_parts", 5, "parts", "checking"),
                                    ("b_pallets", 4, "pallets", "packing"),
                                    ("b_checked", 12, "checking", "packing"),
                                    ("b_out", 4, "packing", "shipping")):
        create_buffer(edge_id, capacity=cap, model=m)
        connect(edge_id, src, dest, model=m)
    return m


def checks(result: dict) -> set:
    return {(f["check"], f["component"]) for f in result["findings"]}


def quiet(fn, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


# --- the run itself --------------------------------------------------------


def test_the_copy_is_the_run_with_the_same_seed():
    model = rework_line(True)
    summary = quiet(run_simulation, 600, seed=3, model=model)
    result = quiet(verify_time_course, 600, seed=3, model=model)

    for node_id in ("machining", "testing"):
        assert result["stations"][node_id]["finished"] == summary["nodes"][node_id][
            "num_item_processed"]


def test_session_model_is_left_alone():
    model = rework_line(True)
    quiet(run_simulation, 300, seed=1, model=model)
    before = model.nodes["testing"].stats["num_item_processed"]

    quiet(verify_time_course, 600, seed=1, model=model)

    assert model.env.now == 300
    assert model.nodes["testing"].stats["num_item_processed"] == before


def test_shares_add_up_and_order_follows_the_flow():
    result = quiet(verify_time_course, 600, seed=1, model=rework_line(True))

    for station in result["stations"].values():
        assert sum(station["shares"].values()) == pytest.approx(1.0)
    order = result["order"]
    assert order[0] == "parts"
    assert order.index("machining") < order.index("b_test") < order.index("testing")
    assert "store" not in order


# --- findings --------------------------------------------------------------


def test_correct_rework_line_has_no_hint():
    result = quiet(verify_time_course, 2400, seed=1, model=rework_line(True))

    assert result["findings"] == []
    assert result["summary"].startswith("No hint")


def test_new_parts_first_stops_the_line():
    result = quiet(verify_time_course, 2400, seed=1, model=rework_line(False))
    found = checks(result)

    assert ("line_stop", None) in found
    assert {("full_to_end", "b_rework"), ("full_to_end", "b_test"),
            ("blocked_to_end", "testing"), ("blocked_to_end", "machining"),
            ("no_output_to_end", "testing")} <= found
    stop = next(f for f in result["findings"] if f["check"] == "line_stop")
    assert stop["severity"] == "hint"
    assert 0 < stop["details"]["first_stop"] <= stop["details"]["from"] < 2400


def test_new_parts_first_names_the_circular_wait():
    result = quiet(verify_time_course, 2400, seed=1, model=rework_line(False))
    circles = [f for f in result["findings"] if f["check"] == "circular_wait"]

    assert len(circles) == 1
    assert circles[0]["component"] == "machining"
    assert circles[0]["details"]["circle"] == ["machining", "b_test", "testing", "b_rework"]
    assert "testing blocked on full b_rework, which feeds machining" in circles[0]["message"]
    assert circles[0]["details"]["priority"] == [
        {"station": "machining", "first": ["b_in"], "then": "b_rework"}]
    assert ("machining takes from b_in before b_rework (FIRST_AVAILABLE), and b_in is full."
            in circles[0]["message"])


def test_a_stop_without_a_circle_names_none():
    result = quiet(verify_time_course, 2880, seed=1,
                   model=shift_line(1440, [[0, 1440]], testing_windows=[[0, 60]]))

    assert "circular_wait" not in {f["check"] for f in result["findings"]}


def test_run_that_starts_before_the_shift_starts_late():
    result = quiet(verify_time_course, 480, seed=1, model=shift_line(1440, [[360, 840]]))
    late = [f for f in result["findings"] if f["check"] == "late_start"]

    assert {f["component"] for f in late} == {"machining", "testing"}
    assert all(f["details"]["first"] >= 360 for f in late)


def test_week_cycle_with_one_window_stops_after_day_one():
    result = quiet(verify_time_course, 7200, seed=1, model=shift_line(10080, [[0, 960]]))
    stop = next(f for f in result["findings"] if f["check"] == "line_stop")

    assert 900 < stop["details"]["from"] <= 1000
    assert "off_shift" in result["findings"][1]["details"]["states"]


def test_shifted_station_calendar_swings_daily():
    twin = quiet(verify_time_course, 5760, seed=1, model=shift_line(1440, [[0, 960]]))
    error = quiet(verify_time_course, 5760, seed=1,
                  model=shift_line(1440, [[0, 960]], testing_windows=[[360, 1320]]))

    assert twin["findings"] == []
    assert ("daily_swing", "b_test") in checks(error)
    assert error["cycle"] == 1440
    swing = next(f for f in error["findings"] if f["component"] == "b_test")
    assert swing["details"]["src"] == "machining"
    assert swing["details"]["src_blocked_per_cycle"] > 0
    assert "machining blocked while it is at capacity" in swing["message"]


def test_combiner_waiting_for_pallets_is_input_starved():
    twin = quiet(verify_time_course, 480, seed=1, model=pallet_line(4))
    error = quiet(verify_time_course, 480, seed=1, model=pallet_line(2))

    assert twin["findings"] == []
    starved = next(f for f in error["findings"] if f["check"] == "input_starved")
    assert starved["component"] == "packing"
    assert starved["details"]["empty"] == "b_pallets"
    assert starved["details"]["full"] == "b_checked"


# --- output and input checks -----------------------------------------------


def test_series_only_on_request():
    model = rework_line(True)
    plain = quiet(verify_time_course, 300, seed=1, model=model)
    full = quiet(verify_time_course, 300, seed=1, series=True, model=model)

    assert "series" not in plain
    points = full["series"]["edges"]["b_in"]
    assert points[0][0] == pytest.approx(full["step"])
    assert all(a[1] != b[1] for a, b in zip(points, points[1:]))


@pytest.mark.parametrize("until", [0, -5, "480"])
def test_rejects_bad_until(until):
    with pytest.raises(ValueError):
        verify_time_course(until, model=rework_line(True))


def test_rejects_bad_seed():
    with pytest.raises(ValueError):
        verify_time_course(100, seed=1.5, model=rework_line(True))


def test_rejects_empty_model():
    with pytest.raises(ValueError, match="empty"):
        verify_time_course(100, model=FactoryModel())


def test_report_shows_the_section():
    model = rework_line(False)
    summary = quiet(run_simulation, 2400, seed=1, model=model)
    html = quiet(render_report, model, [report_view(summary, model)], 2400, seed=1)

    assert "verify_time_course" in html
    assert "line_stop" in html
    assert "<svg" in html
