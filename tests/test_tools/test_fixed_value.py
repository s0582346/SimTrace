"""Tests for the fixed-value test: the mean substitution, the hand
calculation, and the check that holds the two against each other.

Every expected rate in `test_analytic.py`-style cases below was measured
against the simulator first: a deterministic line was run and its departures
counted over the second half of the run. The numbers are therefore not just
what the formula says but what FactorySimPy does.
"""

import pytest

from simtrace.model import FactoryModel
from simtrace.tools.builders import (
    create_buffer,
    create_combiner,
    create_conveyor,
    create_fleet,
    create_machine,
    create_sink,
    create_source,
    create_splitter,
)
from simtrace.tools.distributions import mean_of, spec_parts
from simtrace.tools.simulation import connect
from simtrace.tools.validation import validate_model, verify_fixed_value
from simtrace.tools.validation.analytic import expected_throughput
from simtrace.tools.validation.fixed_value import deterministic_spec


@pytest.fixture
def model() -> FactoryModel:
    return FactoryModel()


# =========================================================================
# builders for the topologies the cases below use
# =========================================================================


def serial(model, inter_arrival_time, processing_delay, work_capacity=1):
    """src -> b1 -> mac -> b2 -> snk."""
    create_source(
        "src", inter_arrival_time=inter_arrival_time, blocking=True, model=model
    )
    create_machine(
        "mac",
        processing_delay=processing_delay,
        work_capacity=work_capacity,
        blocking=True,
        model=model,
    )
    create_sink("snk", model=model)
    create_buffer("b1", capacity=5, model=model)
    create_buffer("b2", capacity=5, model=model)
    connect("b1", "src", "mac", model=model)
    connect("b2", "mac", "snk", model=model)
    return model


def parallel(model, selection, inter_arrival_time, first=3, second=3, upstream=1.0):
    """src -> insp, fanning out to two machines that both feed one sink."""
    create_source(
        "src", inter_arrival_time=inter_arrival_time, blocking=True, model=model
    )
    create_machine(
        "insp",
        processing_delay=upstream,
        blocking=True,
        out_edge_selection=selection,
        model=model,
    )
    create_machine("p1", processing_delay=first, blocking=True, model=model)
    create_machine("p2", processing_delay=second, blocking=True, model=model)
    create_sink("snk", model=model)
    for edge_id in ("b0", "b1", "b2", "b3", "b4"):
        create_buffer(edge_id, capacity=5, model=model)
    connect("b0", "src", "insp", model=model)
    connect("b1", "insp", "p1", model=model)
    connect("b2", "insp", "p2", model=model)
    connect("b3", "p1", "snk", model=model)
    connect("b4", "p2", "snk", model=model)
    return model


def packing(model, quantities, combine_delay, pallets, items, split_delay=None):
    """A pallet source and an item source into a combiner, optionally unpacked."""
    create_source(
        "pal",
        inter_arrival_time=pallets,
        flow_item_type="pallet",
        blocking=True,
        model=model,
    )
    create_source("itm", inter_arrival_time=items, blocking=True, model=model)
    create_combiner(
        "cb",
        target_quantity_of_each_item=quantities,
        processing_delay=combine_delay,
        model=model,
    )
    create_buffer("bp", capacity=5, model=model)
    create_buffer("bi", capacity=30, model=model)
    create_buffer("bo", capacity=5, model=model)
    connect("bp", "pal", "cb", model=model)
    connect("bi", "itm", "cb", model=model)
    if split_delay is None:
        create_sink("snk", model=model)
        connect("bo", "cb", "snk", model=model)
        return model
    create_splitter("sp", processing_delay=split_delay, model=model)
    create_sink("snk", model=model)
    create_buffer("b2", capacity=30, model=model)
    connect("bo", "cb", "sp", model=model)
    connect("b2", "sp", "snk", model=model)
    return model


# =========================================================================
# mean_of — the substitution every distribution goes through
# =========================================================================


@pytest.mark.parametrize(
    "spec, mean",
    [
        (5, 5.0),
        (2.5, 2.5),
        ("uniform(4, 6)", 5.0),
        ("uniform(0, 10)", 5.0),
        ("normal(20, 3)", 20.0),
        ("gauss(8, 1)", 8.0),
        ("exp(3)", 3.0),
    ],
)
def test_mean_of_returns_the_distribution_mean(spec, mean):
    assert mean_of("delay", spec) == pytest.approx(mean)


def test_mean_of_rejects_a_non_spec():
    with pytest.raises(ValueError, match="invalid distribution string"):
        mean_of("delay", "poisson(3)")


def test_mean_of_rejects_a_bool():
    # bool is an int subclass, so True would otherwise read as a delay of 1.
    with pytest.raises(ValueError, match="constant int/float"):
        mean_of("delay", True)


def test_spec_parts_splits_a_spec_and_ignores_a_constant():
    assert spec_parts("uniform(4, 6)") == ("uniform", 4.0, 6.0)
    assert spec_parts("exp(3)") == ("exp", 3.0, None)
    assert spec_parts(5) is None


# =========================================================================
# deterministic_spec — building the twin
# =========================================================================


def test_twin_replaces_every_distribution_with_its_mean(model):
    create_source("src", inter_arrival_time="exp(3)", model=model)
    create_machine("mac", processing_delay="uniform(4, 6)", model=model)
    create_buffer("b1", capacity=4, delay="normal(20, 3)", model=model)
    create_fleet("f1", capacity=2, delay="exp(2)", transit_delay=1, model=model)

    twin, substitutions, _ = deterministic_spec(model.spec)

    values = {step["kwargs"]["id"]: step["kwargs"] for step in twin}
    assert values["src"]["inter_arrival_time"] == 3.0
    assert values["mac"]["processing_delay"] == 5.0
    assert values["b1"]["delay"] == 20.0
    assert values["f1"]["delay"] == 2.0
    # A constant is already deterministic and is left alone.
    assert values["f1"]["transit_delay"] == 1
    assert len(substitutions) == 4


def test_twin_turns_random_edge_choice_into_taking_turns(model):
    create_machine(
        "mac",
        processing_delay=1,
        in_edge_selection="RANDOM",
        out_edge_selection="RANDOM",
        model=model,
    )
    twin, substitutions, _ = deterministic_spec(model.spec)

    kwargs = twin[0]["kwargs"]
    assert kwargs["in_edge_selection"] == "ROUND_ROBIN"
    assert kwargs["out_edge_selection"] == "ROUND_ROBIN"
    assert len(substitutions) == 2


def test_twin_leaves_first_available_alone(model):
    create_machine("mac", processing_delay=1, model=model)
    twin, substitutions, _ = deterministic_spec(model.spec)

    assert twin[0]["kwargs"]["out_edge_selection"] == "FIRST_AVAILABLE"
    assert substitutions == []


def test_twin_warns_when_a_normal_is_clamped_at_zero(model):
    # normal(2, 3) draws negatives, which the sampler clamps, so the running
    # model averages more than 2 and the calculated rate is optimistic.
    create_machine("mac", processing_delay="normal(2, 3)", model=model)
    _, _, warnings = deterministic_spec(model.spec)

    assert len(warnings) == 1
    assert "clamped at zero" in warnings[0]


def test_twin_does_not_warn_for_a_normal_well_clear_of_zero(model):
    create_machine("mac", processing_delay="normal(20, 3)", model=model)
    _, _, warnings = deterministic_spec(model.spec)

    assert warnings == []


def test_twin_preserves_connect_order(model):
    serial(model, 1, 1)
    twin, _, _ = deterministic_spec(model.spec)

    original = [step for step in model.spec if step["op"] == "connect"]
    replayed = [step for step in twin if step["op"] == "connect"]
    assert [step["kwargs"] for step in replayed] == [
        step["kwargs"] for step in original
    ]


# =========================================================================
# expected_throughput — the hand calculation
# =========================================================================


def test_the_machine_is_the_bottleneck(model):
    report = expected_throughput(serial(model, 3, 4))

    assert report["total"] == pytest.approx(0.25)
    assert report["bottlenecks"] == ["mac"]


def test_the_source_is_the_bottleneck(model):
    report = expected_throughput(serial(model, 5, 1))

    assert report["total"] == pytest.approx(0.20)
    assert report["bottlenecks"] == ["src"]


def test_workplaces_multiply_a_machine_rate(model):
    report = expected_throughput(serial(model, 1, 6, work_capacity=3))

    assert report["total"] == pytest.approx(0.5)


def test_a_distribution_is_read_as_its_mean(model):
    # uniform(2, 6) has mean 4, so this is the 1/4 line again.
    report = expected_throughput(serial(model, "exp(3)", "uniform(2, 6)"))

    assert report["total"] == pytest.approx(0.25)


def test_a_holding_buffer_is_a_station(model):
    create_source("src", inter_arrival_time=1, blocking=True, model=model)
    create_machine("mac", processing_delay=1, blocking=True, model=model)
    create_sink("snk", model=model)
    create_buffer("b1", capacity=5, model=model)
    create_buffer("bath", capacity=12, delay=20, model=model)
    connect("b1", "src", "mac", model=model)
    connect("bath", "mac", "snk", model=model)

    report = expected_throughput(model)

    assert report["total"] == pytest.approx(0.6)
    assert report["bottlenecks"] == ["bath"]


def test_a_conveyor_is_rated_by_speed_over_item_length(model):
    create_source("src", inter_arrival_time=0.1, blocking=True, model=model)
    create_sink("snk", model=model)
    create_conveyor("belt", conveyor_length=10, speed=2, item_length=4, model=model)
    connect("belt", "src", "snk", model=model)

    report = expected_throughput(model)

    assert report["total"] == pytest.approx(0.5)
    assert report["bottlenecks"] == ["belt"]


def test_taking_turns_lets_the_slowest_branch_set_the_pace(model):
    # p1 does one in 2, p2 one in 6. A fixed even split runs both at p2's pace.
    report = expected_throughput(
        parallel(model, "ROUND_ROBIN", 0.5, first=2, second=6, upstream=0.5)
    )

    assert report["total"] == pytest.approx(1 / 3)


def test_first_available_pools_the_branches(model):
    # The same two machines, but items go wherever there is room: 1/2 + 1/6.
    report = expected_throughput(
        parallel(model, "FIRST_AVAILABLE", 0.5, first=2, second=6, upstream=0.5)
    )

    assert report["total"] == pytest.approx(2 / 3)


def test_two_equal_branches_agree_under_either_rule(model):
    turns = expected_throughput(parallel(model, "ROUND_ROBIN", 1))
    pooled = expected_throughput(parallel(FactoryModel(), "FIRST_AVAILABLE", 1))

    assert turns["total"] == pytest.approx(2 / 3)
    assert pooled["total"] == pytest.approx(2 / 3)


def test_a_combiner_is_limited_by_its_own_cycle(model):
    report = expected_throughput(packing(model, [1, 4], 8, 2, 1))

    assert report["total"] == pytest.approx(0.125)
    assert report["bottlenecks"] == ["cb"]


def test_a_combiner_is_limited_by_the_supply_it_packs(model):
    # One item every 3 and four to a pallet: a pallet every 12 at best.
    report = expected_throughput(packing(model, [1, 4], 8, 2, 3))

    assert report["total"] == pytest.approx(1 / 12)
    assert report["bottlenecks"] == ["itm"]


def test_a_splitter_emits_the_packed_items_and_the_container(model):
    # A pallet every 8, holding four items, plus the empty container: 5/8.
    report = expected_throughput(packing(model, [1, 4], 8, 2, 1, split_delay=1))

    assert report["total"] == pytest.approx(0.625)


def test_the_pack_count_follows_the_combiner_upstream(model):
    report = expected_throughput(packing(model, [1, 2], 8, 2, 1, split_delay=1))

    assert report["total"] == pytest.approx(0.375)


# --- what the calculation refuses to answer -------------------------------


def test_a_fleet_makes_the_calculation_inapplicable(model):
    create_source("src", inter_arrival_time=2, blocking=True, model=model)
    create_sink("snk", model=model)
    create_fleet("fl", capacity=4, delay=3, transit_delay=5, model=model)
    connect("fl", "src", "snk", model=model)

    report = expected_throughput(model)

    assert report["applicable"] is False
    assert [b["reason"] for b in report["blockers"]] == ["fleet_on_route"]


def test_an_unconnected_edge_makes_the_calculation_inapplicable(model):
    serial(model, 1, 1)
    create_buffer("loose", capacity=1, model=model)

    report = expected_throughput(model)

    assert report["applicable"] is False
    assert "edge_not_connected" in [b["reason"] for b in report["blockers"]]


def test_a_split_mode_splitter_makes_the_calculation_inapplicable(model):
    create_source("src", inter_arrival_time=1, blocking=True, model=model)
    create_splitter("sp", mode="SPLIT", split_quantity=3, model=model)
    create_sink("snk", model=model)
    create_buffer("b1", capacity=5, model=model)
    create_buffer("b2", capacity=5, model=model)
    connect("b1", "src", "sp", model=model)
    connect("b2", "sp", "snk", model=model)

    report = expected_throughput(model)

    assert report["applicable"] is False
    assert "splitter_split_mode" in [b["reason"] for b in report["blockers"]]


def test_a_splitter_with_no_pallet_upstream_is_inapplicable(model):
    create_source("src", inter_arrival_time=1, blocking=True, model=model)
    create_splitter("sp", processing_delay=1, model=model)
    create_sink("snk", model=model)
    create_buffer("b1", capacity=5, model=model)
    create_buffer("b2", capacity=5, model=model)
    connect("b1", "src", "sp", model=model)
    connect("b2", "sp", "snk", model=model)

    report = expected_throughput(model)

    assert report["applicable"] is False
    assert "splitter_pack_count" in [b["reason"] for b in report["blockers"]]


def test_a_loop_in_the_wiring_is_inapplicable(model):
    create_source("src", inter_arrival_time=1, blocking=True, model=model)
    create_machine("m1", processing_delay=1, blocking=True, model=model)
    create_machine("m2", processing_delay=1, blocking=True, model=model)
    create_sink("snk", model=model)
    for edge_id in ("b0", "b1", "b2", "b3"):
        create_buffer(edge_id, capacity=5, model=model)
    connect("b0", "src", "m1", model=model)
    connect("b1", "m1", "m2", model=model)
    connect("b2", "m2", "m1", model=model)  # back to m1: a rework loop
    connect("b3", "m2", "snk", model=model)

    report = expected_throughput(model)

    assert report["applicable"] is False
    assert "loop_in_wiring" in [b["reason"] for b in report["blockers"]]


# --- the station table ----------------------------------------------------


def test_the_station_table_shows_the_arithmetic(model):
    report = expected_throughput(serial(model, 3, 4, work_capacity=2))

    rows = {row["component"]: row for row in report["stations"]}
    assert rows["mac"]["basis"] == "2 workplace(s) / 4"
    assert rows["mac"]["rate"] == pytest.approx(0.5)
    assert rows["src"]["basis"] == "1 item / 3"
    # A buffer with no holding time places no limit of its own.
    assert rows["b1"]["rate"] is None


# =========================================================================
# verify_fixed_value — the check itself
# =========================================================================


def test_a_correct_line_passes(model):
    report = verify_fixed_value(400, model=serial(model, 3, 4))

    assert report["applicable"] is True
    assert report["passed"] is True
    assert report["comparison"]["snk"]["expected"] == pytest.approx(0.25)
    assert report["comparison"]["snk"]["measured"] == pytest.approx(0.25)


def test_the_measured_rate_ignores_the_filling_of_the_line(model):
    # Over the whole run the count is short by the fill; over the window it is
    # not. Both are reported so the difference is visible.
    report = verify_fixed_value(400, model=serial(model, 3, 4))

    measured = report["measured"]["per_sink"]["snk"]
    assert measured["rate"] == pytest.approx(0.25)
    assert measured["total"] / 400 < 0.25


def test_a_stochastic_model_is_checked_through_its_twin(model):
    # exp(3) arrivals and uniform(4, 6) processing: means 3 and 5, so 1/5.
    report = verify_fixed_value(1000, model=serial(model, "exp(3)", "uniform(4, 6)"))

    assert report["passed"] is True
    assert report["comparison"]["snk"]["expected"] == pytest.approx(0.2)
    assert len(report["substitutions"]) == 2


def test_parallel_stations_pass(model):
    report = verify_fixed_value(600, model=parallel(model, "ROUND_ROBIN", 1))

    assert report["passed"] is True
    assert report["comparison"]["snk"]["expected"] == pytest.approx(2 / 3)


def test_the_tolerance_is_never_tighter_than_one_item(model):
    # A short window holds few items, so the allowance has to widen past the
    # 1% asked for or a single boundary item would read as a failure.
    report = verify_fixed_value(80, warmup=40, tolerance=0.01, model=serial(model, 3, 4))

    entry = report["comparison"]["snk"]
    assert entry["tolerance"] > 0.01
    assert entry["tolerance"] == pytest.approx(1 / (40 * 0.25))


def test_a_short_run_is_flagged(model):
    report = verify_fixed_value(30, model=serial(model, 3, 4))

    assert any("after the warm-up point" in w for w in report["warnings"])


def test_the_session_model_is_left_alone(model):
    serial(model, 3, 4)
    verify_fixed_value(200, model=model)

    # The twin ran, not this model: its clock never moved and no events were
    # captured, so the other verify_* tools still see what they saw.
    assert model.env.now == 0
    assert model.events == []


def test_a_fleet_reports_no_expected_value(model):
    create_source("src", inter_arrival_time=2, blocking=True, model=model)
    create_sink("snk", model=model)
    create_fleet("fl", capacity=4, delay=3, transit_delay=5, model=model)
    connect("fl", "src", "snk", model=model)

    report = verify_fixed_value(400, model=model)

    assert report["applicable"] is False
    assert report["passed"] is False
    assert "no expected value can be stated" in report["summary"]


def test_a_twin_that_raises_is_reported_not_propagated(model):
    # FactorySimPy raises from inside the run when a conveyor feeds a sink.
    create_source("src", inter_arrival_time=1, item_length=1, blocking=True, model=model)
    create_sink("snk", model=model)
    create_conveyor("belt", conveyor_length=10, speed=2, item_length=1, model=model)
    connect("belt", "src", "snk", model=model)

    report = verify_fixed_value(100, model=model)

    assert report["applicable"] is False
    assert report["passed"] is False
    assert [b["reason"] for b in report["blockers"]] == ["twin_run_failed"]


def test_an_empty_model_is_rejected(model):
    with pytest.raises(ValueError, match="model is empty"):
        verify_fixed_value(100, model=model)


@pytest.mark.parametrize("until, warmup", [(0, None), (-1, None), (100, 100), (100, -1)])
def test_bad_settings_are_rejected(model, until, warmup):
    serial(model, 3, 4)
    with pytest.raises(ValueError):
        verify_fixed_value(until, warmup=warmup, model=model)


# --- the second finding: the deterministic ceiling ------------------------


def test_a_replication_mean_below_the_ceiling_holds(model):
    report = verify_fixed_value(
        400,
        replication_means={"snk.num_item_received": 80.0},
        model=serial(model, 3, 4),
    )

    assert report["upper_bound"]["snk"]["holds"] is True
    assert report["passed"] is True


def test_a_replication_mean_above_the_ceiling_fails(model):
    # The deterministic line delivers about 98 in 400; a stochastic mean of 900
    # cannot happen, so the model that produced it is not this one.
    report = verify_fixed_value(
        400,
        replication_means={"snk.num_item_received": 900.0},
        model=serial(model, 3, 4),
    )

    assert report["upper_bound"]["snk"]["holds"] is False
    assert report["passed"] is False
    assert "ABOVE THE CEILING" in report["summary"]


def test_a_bare_sink_id_is_accepted_as_a_key(model):
    report = verify_fixed_value(
        400, replication_means={"snk": 80.0}, model=serial(model, 3, 4)
    )

    assert report["upper_bound"]["snk"]["holds"] is True


# =========================================================================
# what validate_model now says about splitters
# =========================================================================


def test_split_mode_is_an_error_before_the_run(model):
    create_source("src", inter_arrival_time=1, blocking=True, model=model)
    create_splitter("sp", mode="SPLIT", split_quantity=3, model=model)
    create_sink("snk", model=model)
    create_buffer("b1", capacity=5, model=model)
    create_buffer("b2", capacity=5, model=model)
    connect("b1", "src", "sp", model=model)
    connect("b2", "sp", "snk", model=model)

    report = validate_model(model=model)

    assert report["valid"] is False
    assert "splitter_mode" in [finding["check"] for finding in report["errors"]]


def test_a_splitter_fed_empty_pallets_is_a_warning(model):
    create_source(
        "pal", inter_arrival_time=1, flow_item_type="pallet", blocking=True, model=model
    )
    create_splitter("sp", processing_delay=1, model=model)
    create_sink("snk", model=model)
    create_buffer("b1", capacity=5, model=model)
    create_buffer("b2", capacity=5, model=model)
    connect("b1", "pal", "sp", model=model)
    connect("b2", "sp", "snk", model=model)

    report = validate_model(model=model)

    messages = [finding["message"] for finding in report["warnings"]]
    assert any("those are empty" in message for message in messages)
