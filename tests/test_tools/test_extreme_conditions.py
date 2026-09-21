"""Tests for the limit test: the bounds, the single part, and the floor.

Every expected time below is the value a station was built with. That is the
whole point of the check: with one part in the plant, nothing queues, so a
station's measured time is its own time and nothing else.
"""

import pytest

from simtrace.model import FactoryModel
from simtrace.tools.builders import (
    create_buffer,
    create_machine,
    create_sink,
    create_source,
)
from simtrace.tools.simulation import connect, run_simulation
from simtrace.tools.validation import verify_extreme_conditions
from simtrace.tools.validation.extreme_conditions import (
    ENTERED,
    LOWER,
    UPPER,
    bounds,
    part_stays,
    spec_at,
)


@pytest.fixture
def model() -> FactoryModel:
    return FactoryModel()


# =========================================================================
# builders for the topologies the cases below use
# =========================================================================


def serial(model, processing_delay="uniform(4, 6)", hold=0):
    """src -> b1 -> mac -> bath -> snk, the bath holding items for `hold`."""
    create_source("src", inter_arrival_time="exp(3)", blocking=True, model=model)
    create_machine("mac", processing_delay=processing_delay, model=model)
    create_sink("snk", model=model)
    create_buffer("b1", capacity=5, model=model)
    create_buffer("bath", capacity=5, delay=hold, model=model)
    connect("b1", "src", "mac", model=model)
    connect("bath", "mac", "snk", model=model)
    return model


def bypass(model):
    """One branch through a holding bath, one straight to the sink.

    The bath is connected first, so the single part takes the long branch and
    the floor covers it. In a full run the parts alternate, so half of them
    reach the sink without the bath.
    """
    create_source("src", inter_arrival_time=2, blocking=True, model=model)
    create_machine(
        "mac", processing_delay=1, out_edge_selection="ROUND_ROBIN", model=model
    )
    create_sink("snk", model=model)
    create_buffer("b1", capacity=5, model=model)
    create_buffer("bath", capacity=5, delay=20, model=model)
    create_buffer("short", capacity=5, model=model)
    connect("b1", "src", "mac", model=model)
    connect("bath", "mac", "snk", model=model)
    connect("short", "mac", "snk", model=model)
    return model


# =========================================================================
# bounds — the value a distribution is pushed to
# =========================================================================


@pytest.mark.parametrize(
    "value, low, high, capped",
    [
        (5, 5.0, 5.0, True),
        (2.5, 2.5, 2.5, True),
        ("uniform(4, 6)", 4.0, 6.0, True),
        ("normal(20, 3)", 11.0, 29.0, True),
        ("gauss(8, 1)", 5.0, 11.0, True),
        # A normal whose mean sits near zero is floored there, not run negative.
        ("normal(2, 3)", 0.0, 11.0, True),
    ],
)
def test_bounds_of_a_value(value, low, high, capped):
    assert bounds(value) == (pytest.approx(low), pytest.approx(high), capped)


def test_an_exponential_has_no_upper_bound():
    low, high, capped = bounds("exp(20)")

    assert low == 0.0
    # 20 * ln(100): the value it stays under 99 times in 100.
    assert high == pytest.approx(92.103, abs=1e-3)
    assert capped is False


# =========================================================================
# spec_at — the copy of the model at one bound
# =========================================================================


def test_the_copy_puts_every_delay_at_its_bound(model):
    serial(model, processing_delay="uniform(4, 6)", hold="normal(20, 3)")

    low, substitutions = spec_at(model.spec, LOWER)
    high, _ = spec_at(model.spec, UPPER)

    def by_id(spec):
        return {
            step["kwargs"]["id"]: step["kwargs"]
            for step in spec
            if "id" in step["kwargs"]
        }

    assert by_id(low)["mac"]["processing_delay"] == 4.0
    assert by_id(low)["bath"]["delay"] == 11.0
    assert by_id(high)["mac"]["processing_delay"] == 6.0
    assert by_id(high)["bath"]["delay"] == 29.0
    assert len(substitutions) == 2


def test_the_copy_leaves_the_arrival_time_alone(model):
    serial(model)

    low, _ = spec_at(model.spec, LOWER)

    # The parts are released by patching the sources, not through this value.
    assert low[0]["kwargs"]["inter_arrival_time"] == "exp(3)"


def test_the_unchanged_setting_returns_the_spec_as_it_is(model):
    serial(model)

    entered, substitutions = spec_at(model.spec, ENTERED)

    assert [step["kwargs"] for step in entered] == [
        step["kwargs"] for step in model.spec
    ]
    assert substitutions == []


# =========================================================================
# part_stays — reading a part's trail out of a run's events
# =========================================================================


def test_a_stay_runs_from_the_line_that_put_the_part_there():
    events = [
        {"kind": "put", "node": "src", "edge": "b1", "item": "i1", "time": 0.0},
        {"kind": "process_start", "node": "mac", "item": "i1", "time": 2.0},
        {"kind": "put", "node": "mac", "edge": "b2", "item": "i1", "time": 7.0},
        {"kind": "received", "node": "snk", "item": "Item(i1)", "time": 9.0},
    ]

    stays = part_stays(events)

    assert stays["i1"] == [("b1", 0.0, 2.0), ("mac", 2.0, 7.0), ("b2", 7.0, 9.0)]


# =========================================================================
# verify_extreme_conditions — the check itself
# =========================================================================


def test_every_station_takes_the_time_it_was_given(model):
    report = verify_extreme_conditions(400, model=serial(model, hold="normal(20, 3)"))

    stations = {row["id"]: row for row in report["stations"]}
    assert stations["mac"][LOWER] == pytest.approx(4.0, abs=0.011)
    assert stations["mac"][UPPER] == pytest.approx(6.0, abs=0.011)
    assert stations["bath"][LOWER] == pytest.approx(11.0, abs=0.011)
    assert stations["bath"][UPPER] == pytest.approx(29.0, abs=0.011)
    assert report["passed"] is True


def test_the_part_is_quickest_at_the_lower_bound(model):
    report = verify_extreme_conditions(400, model=serial(model, hold="normal(20, 3)"))

    single = report["single"]
    assert single[LOWER] < single[ENTERED] < single[UPPER]
    # 4 + 11 at the bottom, 6 + 29 at the top.
    assert single[LOWER] == pytest.approx(15.0, abs=0.011)
    assert single[UPPER] == pytest.approx(35.0, abs=0.011)


def test_a_time_entered_as_a_setup_time_is_reported(model):
    # The machine is given no processing time and 8 of setup instead, so the
    # part waits in the buffer in front of it, which has no holding time.
    create_source("src", inter_arrival_time=10, blocking=True, model=model)
    create_machine("mac", processing_delay=0, node_setup_time=8, model=model)
    create_sink("snk", model=model)
    create_buffer("b1", capacity=5, model=model)
    create_buffer("b2", capacity=5, model=model)
    connect("b1", "src", "mac", model=model)
    connect("b2", "mac", "snk", model=model)

    report = verify_extreme_conditions(200, model=model)

    assert report["passed"] is False
    finding = next(f for f in report["findings"] if f["check"] == "time_mismatch")
    assert finding["component"] == "b1"


def test_an_exponential_is_flagged_but_does_not_fail(model):
    report = verify_extreme_conditions(1000, model=serial(model, hold="exp(20)"))

    checks = {f["check"] for f in report["findings"]}
    assert checks == {"zero_possible", "no_upper_bound"}
    assert all(f["severity"] == "hint" for f in report["findings"])
    assert report["passed"] is True


def test_without_a_run_there_is_nothing_to_hold_against(model):
    report = verify_extreme_conditions(400, model=serial(model))

    assert report["run"] is None
    assert report["below_floor"] == []
    assert all(row["run"] is None for row in report["stations"])


def test_a_part_quicker_than_every_station_allows_is_reported(model):
    bypass(model)
    run_simulation(300, seed=1, model=model)

    report = verify_extreme_conditions(200, seed=1, model=model)

    assert report["passed"] is False
    assert report["below_floor"]
    # The part that beat the floor never went through the bath.
    route = report["below_floor"][0]["route"]
    assert "bath" not in route and "short" in route
    assert any(f["check"] == "below_floor" for f in report["findings"])


def test_the_run_is_measured_station_by_station(model):
    serial(model, hold="normal(20, 3)")
    run_simulation(300, seed=1, model=model)

    report = verify_extreme_conditions(400, seed=1, model=model)

    bath = next(row for row in report["stations"] if row["id"] == "bath")
    assert bath["run"]["n"] > 0
    assert 11.0 - 0.011 <= bath["run"]["fastest"] <= bath["run"]["slowest"] <= 29.011
    assert report["run"]["fastest"] >= report["single"][LOWER] - 0.011


def test_the_session_model_is_left_alone(model):
    serial(model)
    verify_extreme_conditions(200, model=model)

    assert model.env.now == 0
    assert model.events == []


def test_a_part_that_never_arrives_is_reported(model):
    # 400 of holding time, and only 100 to run in.
    report = verify_extreme_conditions(100, model=serial(model, hold=400))

    assert report["passed"] is False
    assert any(f["check"] == "part_stuck" for f in report["findings"])


def test_an_empty_model_is_rejected(model):
    with pytest.raises(ValueError, match="model is empty"):
        verify_extreme_conditions(100, model=model)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"until": 0},
        {"until": -1},
        {"until": 100, "parts_per_source": 0},
        {"until": 100, "parts_per_source": 1.5},
        {"until": 100, "quantile": 1},
        {"until": 100, "quantile": 0},
    ],
)
def test_bad_settings_are_rejected(model, kwargs):
    serial(model)
    with pytest.raises(ValueError):
        verify_extreme_conditions(model=model, **kwargs)
