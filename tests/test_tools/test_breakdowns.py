"""Tests for machine breakdowns (create_machine(time_to_failure, repair_time))."""

import pytest

from simtrace.model import FactoryModel
from simtrace.tools.builders import (
    create_buffer,
    create_machine,
    create_sink,
    create_source,
)
from simtrace.tools.simulation import build_from_spec, connect, run_simulation
from simtrace.tools.telemetry import _classify


@pytest.fixture
def model() -> FactoryModel:
    return FactoryModel()


def _line(
    model: FactoryModel,
    iat: float,
    processing: float,
    time_to_failure,
    repair_time,
    work_capacity: int = 1,
) -> FactoryModel:
    """src -> b_in -> m -> b_out -> snk."""
    create_source("src", inter_arrival_time=iat, blocking=True, model=model)
    create_buffer("b_in", capacity=100, model=model)
    create_machine("m", processing_delay=processing, work_capacity=work_capacity,
                   time_to_failure=time_to_failure, repair_time=repair_time,
                   model=model)
    create_buffer("b_out", capacity=100, model=model)
    create_sink("snk", model=model)
    connect("b_in", "src", "m", model=model)
    connect("b_out", "m", "snk", model=model)
    return model


def _times(model: FactoryModel, kind: str, node: str) -> list[float]:
    return [e["time"] for e in model.events
            if e["kind"] == kind and e.get("node") == node]


# --- create_machine ------------------------------------------------------------


def test_machine_echoes_and_records_breakdowns(model):
    summary = create_machine("m", time_to_failure="exp(480)",
                             repair_time="uniform(30, 33)", model=model)

    assert summary["time_to_failure"] == "exp(480)"
    assert summary["repair_time"] == "uniform(30, 33)"
    kwargs = model.spec[-1]["kwargs"]
    assert kwargs["time_to_failure"] == "exp(480)"
    assert kwargs["repair_time"] == "uniform(30, 33)"
    assert model.nodes["m"].stats["num_breakdowns"] == 0


def test_machine_without_breakdowns_has_no_breakdown_stats(model):
    summary = create_machine("m", model=model)

    assert summary["time_to_failure"] is None
    stats = model.nodes["m"].stats
    assert "num_breakdowns" not in stats
    assert "time_down" not in stats
    assert "BREAKDOWN_STATE" not in stats["total_time_spent_in_states"]


@pytest.mark.parametrize(
    "time_to_failure, repair_time",
    [(10, None), (None, 2)],
)
def test_breakdown_times_come_together(model, time_to_failure, repair_time):
    with pytest.raises(ValueError, match="together"):
        create_machine("m", time_to_failure=time_to_failure,
                       repair_time=repair_time, model=model)


@pytest.mark.parametrize(
    "time_to_failure, repair_time, message",
    [
        (0, 2, "time_to_failure must be > 0"),
        (-1, 2, "time_to_failure must be > 0"),
        (10, -1, "repair_time must be >= 0"),
        ("exp(0)", 2, "time_to_failure"),
        (10, "lognormal(1, 2)", "repair_time"),
        (True, 2, "time_to_failure"),
    ],
)
def test_bad_breakdown_times_are_rejected(model, time_to_failure, repair_time,
                                          message):
    with pytest.raises(ValueError, match=message):
        create_machine("m", time_to_failure=time_to_failure,
                       repair_time=repair_time, model=model)


# --- run -----------------------------------------------------------------------


def test_breakdowns_follow_the_simulation_clock(model):
    # No item ever arrives, so the machine is idle; it breaks down anyway.
    _line(model, iat=100, processing=5, time_to_failure=3, repair_time=2)
    stats = run_simulation(until=20, model=model)["nodes"]["m"]

    assert _times(model, "breakdown", "m") == [3, 8, 13, 18]
    assert _times(model, "repaired", "m") == [5, 10, 15]
    assert stats["num_breakdowns"] == 4
    # The breakdown at 18 is still running at 20 and counts with 2.
    assert stats["time_down"] == 8


def test_job_finishes_its_remaining_time_after_the_repair(model):
    # Items arrive from t=1. Each job takes 5; breakdowns at 4, 10, 16 last 2.
    #   job 1: 1-4 (3 done), down, 6-8 (2 left)   -> leaves at 8
    #   job 2: 8-10 (2 done), down, 12-15 (3 left) -> leaves at 15
    #   job 3: 15-16 (1 done), down, 18-22         -> still in work at 20
    _line(model, iat=1, processing=5, time_to_failure=4, repair_time=2)
    stats = run_simulation(until=20, model=model)["nodes"]["m"]

    assert _times(model, "put", "m") == [8, 15]
    assert stats["num_item_processed"] == 2
    assert stats["num_breakdowns"] == 3
    assert stats["time_down"] == 6
    states = stats["total_time_spent_in_states"]
    # Up to the last state change at 18: 1 idle, 11 processing, 6 down.
    assert states["BREAKDOWN_STATE"] == 6
    assert states["ATLEAST_ONE_PROCESSING_STATE"] == 11
    assert states["IDLE_STATE"] == 1


def test_no_item_is_pulled_while_down(model):
    # Down from 4 to 7. The item arriving at 5 waits in b_in until the repair.
    _line(model, iat=5, processing=1, time_to_failure=4, repair_time=3)
    run_simulation(until=9, model=model)

    assert _times(model, "breakdown", "m") == [4]
    assert _times(model, "repaired", "m") == [7]
    assert _times(model, "process_start", "m")[0] == 7
    assert _times(model, "put", "m") == [8]


def test_breakdown_stops_every_job_of_a_multi_slot_machine(model):
    # Two slots. Items at 1 and 2 start on arrival; down from 3 to 13, next
    # breakdown at 16.
    _line(model, iat=1, processing=3.5, time_to_failure=3, repair_time=10,
          work_capacity=2)
    run_simulation(until=16, model=model)

    # 1.5 and 2.5 time units were left; both finish after the repair.
    assert _times(model, "put", "m")[:2] == [14.5, 15.5]


def test_blocked_item_leaves_during_a_breakdown(model):
    # m hands over to a slow m2 through a one-place buffer. At 4 m holds a
    # finished item, blocked. m is down from 4.5 to 14.5; m2 frees the buffer
    # at 5 and the blocked item leaves then.
    create_source("src", inter_arrival_time=1, blocking=True, model=model)
    create_buffer("b_in", capacity=100, model=model)
    create_machine("m", processing_delay=1, time_to_failure=4.5,
                   repair_time=10, model=model)
    create_buffer("b_mid", capacity=1, model=model)
    create_machine("m2", processing_delay=3, model=model)
    create_buffer("b_out", capacity=100, model=model)
    create_sink("snk", model=model)
    connect("b_in", "src", "m", model=model)
    connect("b_mid", "m", "m2", model=model)
    connect("b_out", "m2", "snk", model=model)
    run_simulation(until=14, model=model)

    puts_while_down = [t for t in _times(model, "put", "m") if 4.5 <= t < 14.5]
    starts_while_down = [t for t in _times(model, "process_start", "m")
                         if 4.5 <= t < 14.5]
    assert puts_while_down == [5]
    assert starts_while_down == []


def test_stochastic_breakdowns_are_reproducible_with_a_seed(model):
    def run() -> dict:
        built = _line(FactoryModel(), iat="exp(2)", processing="exp(1.5)",
                      time_to_failure="exp(20)", repair_time="uniform(2, 4)")
        return run_simulation(until=500, seed=7, model=built)["nodes"]["m"]

    first, second = run(), run()
    assert first["num_breakdowns"] > 0
    assert first["num_breakdowns"] == second["num_breakdowns"]
    assert first["time_down"] == second["time_down"]


def test_rebuilt_model_keeps_breakdowns(model):
    _line(model, iat=100, processing=5, time_to_failure=3, repair_time=2)
    copy = build_from_spec(model.spec)
    stats = run_simulation(until=20, model=copy)["nodes"]["m"]

    assert stats["num_breakdowns"] == 4
    assert stats["time_down"] == 8


# --- telemetry -----------------------------------------------------------------


def test_breakdown_lines_are_classified():
    assert _classify("m breaks down") == ("breakdown", {"sim.node": "m"})
    assert _classify("m is repaired") == ("repaired", {"sim.node": "m"})
