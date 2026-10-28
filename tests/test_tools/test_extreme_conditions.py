"""Tests for the limit test: the bounds, the single part, and the floor.

Every expected time below is the value a station was built with. That is the
whole point of the check: with one part in the plant, nothing queues, so a
station's measured time is its own time and nothing else.
"""

import pytest

from simtrace.model import FactoryModel
from simtrace.tools.builders import (
    create_buffer,
    create_combiner,
    create_machine,
    create_sink,
    create_source,
)
from simtrace.tools.simulation import connect, run_simulation
from simtrace.tools.validation import verify_extreme_conditions
from simtrace.tools.validation.extreme_conditions import (
    combiner_release,
    ENTERED,
    LOWER,
    MAX_COMBINATIONS,
    NO_BREAKDOWNS,
    UPPER,
    ENDLESS,
    _check_combinations,
    _check_inputs,
    _check_stations,
    _station_rows,
    breakdowns_at_means,
    endless_buffers,
    one_branch,
    scaled_arrivals,
    stopped_station,
    targeted_plan,
    bounds,
    combination_masks,
    has_real_floor,
    input_plan,
    mix_is_fixed,
    part_stays,
    random_elements,
    ranged_stations,
    route_floor,
    saturated_arrivals,
    soft_reasons,
    spec_at,
    spec_with,
    zero_station_times,
    zero_time_route,
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


def setup_line(model, setup=8, processing=0, travel=0):
    """src -> b0 -> pre (taking `travel`) -> b1 -> mac (setup `setup`) -> b2 -> snk."""
    create_source("src", inter_arrival_time=10, blocking=True, model=model)
    create_machine("pre", processing_delay=travel, model=model)
    create_machine(
        "mac", processing_delay=processing, node_setup_time=setup, model=model
    )
    create_sink("snk", model=model)
    create_buffer("b0", capacity=5, model=model)
    create_buffer("b1", capacity=5, model=model)
    create_buffer("b2", capacity=5, model=model)
    connect("b0", "src", "pre", model=model)
    connect("b1", "pre", "mac", model=model)
    connect("b2", "mac", "snk", model=model)
    return model


def test_a_time_entered_as_a_setup_time_is_reported(model):
    # The machine is given no processing time and 8 of setup instead.
    report = verify_extreme_conditions(200, model=setup_line(model))

    hint = next(f for f in report["findings"] if f["check"] == "setup_longer_than_processing")
    assert hint["component"] == "mac"
    assert hint["severity"] == "hint"
    assert not any(f["check"] == "time_mismatch" for f in report["findings"])


def test_a_part_waiting_for_a_setup_is_shown_not_failed(model):
    # The part reaches the buffer in front of mac at 3, 5 before its setup of
    # 8 is over, so it waits 5 there.
    report = verify_extreme_conditions(200, model=setup_line(model, processing=2, travel=3))

    rows = {row["id"]: row for row in report["stations"]}
    assert rows["b1"][LOWER] == pytest.approx(5.0, abs=0.011)
    assert rows["b1"]["setup_wait"][LOWER] == pytest.approx(5.0, abs=0.011)
    assert rows["mac"][LOWER] == pytest.approx(2.0, abs=0.011)
    note = next(f for f in report["findings"] if f["check"] == "setup_wait")
    assert note["component"] == "b1"
    assert note["severity"] == "info"
    assert not any(f["check"] == "time_mismatch" for f in report["findings"])


def test_a_part_arriving_after_the_setup_does_not_wait(model):
    # The setup of 8 is over before the part arrives at 10.
    report = verify_extreme_conditions(200, model=setup_line(model, processing=2, travel=10))

    rows = {row["id"]: row for row in report["stations"]}
    assert rows["b1"][LOWER] == pytest.approx(0.0, abs=0.011)
    assert not any(f["check"] == "setup_wait" for f in report["findings"])


def test_a_breakdown_does_not_count_as_station_time(model):
    # A breakdown every 1 with 50 of repair would land in the part's stay at
    # mac in every copy that keeps it.
    create_source("src", inter_arrival_time=10, blocking=True, model=model)
    create_machine("mac", processing_delay="uniform(4, 6)", time_to_failure=1,
                   repair_time=50, model=model)
    create_sink("snk", model=model)
    create_buffer("b1", capacity=5, model=model)
    create_buffer("b2", capacity=5, model=model)
    connect("b1", "src", "mac", model=model)
    connect("b2", "mac", "snk", model=model)

    report = verify_extreme_conditions(400, model=model)

    rows = {row["id"]: row for row in report["stations"]}
    assert rows["mac"][LOWER] == pytest.approx(4.0, abs=0.011)
    assert rows["mac"][UPPER] == pytest.approx(6.0, abs=0.011)
    assert not any(f["check"] == "time_mismatch" for f in report["findings"])


def test_a_shift_calendar_does_not_count_as_station_time(model):
    # mac's shift starts at 100, so with the calendar the part would wait in b1
    # until then.
    from simtrace.tools.builders import create_shift_calendar

    create_shift_calendar("day", cycle=1000, windows=[[100, 900]], model=model)
    create_source("src", inter_arrival_time="exp(3)", blocking=True, model=model)
    create_machine("mac", processing_delay="uniform(4, 6)", calendar="day", model=model)
    create_sink("snk", model=model)
    create_buffer("b1", capacity=5, model=model)
    create_buffer("bath", capacity=5, model=model)
    connect("b1", "src", "mac", model=model)
    connect("bath", "mac", "snk", model=model)

    report = verify_extreme_conditions(400, model=model)

    rows = {row["id"]: row for row in report["stations"]}
    assert rows["b1"][LOWER] == pytest.approx(0.0, abs=0.011)
    assert rows["mac"][LOWER] == pytest.approx(4.0, abs=0.011)
    assert not any(f["check"] == "time_mismatch" for f in report["findings"])


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


def test_parts_of_two_sources_do_not_meet_at_a_shared_station(model):
    # Both sources feed one machine. Released together, one part would wait
    # behind the other in its buffer, which holds nothing.
    create_source("src_a", inter_arrival_time="exp(8)", blocking=True, model=model)
    create_source("src_b", inter_arrival_time="exp(12)", blocking=True, model=model)
    create_machine("mac", processing_delay="uniform(3, 4)", model=model)
    create_sink("snk", model=model)
    create_buffer("b_a", capacity=5, model=model)
    create_buffer("b_b", capacity=5, model=model)
    create_buffer("b_out", capacity=5, model=model)
    connect("b_a", "src_a", "mac", model=model)
    connect("b_b", "src_b", "mac", model=model)
    connect("b_out", "mac", "snk", model=model)

    report = verify_extreme_conditions(100, model=model)

    rows = {row["id"]: row for row in report["stations"]}
    assert rows["b_a"][LOWER] == pytest.approx(0)
    assert rows["b_b"][LOWER] == pytest.approx(0)
    assert rows["mac"]["parts"][LOWER] == 2
    assert not any(f["check"] == "time_mismatch" for f in report["findings"])


def test_a_part_sent_back_is_measured_per_visit(model):
    # A test station sends the part back once: it passes the machine and the
    # test twice. Each visit takes the station's own time, the sums do not.
    create_source("src", inter_arrival_time="exp(5)", blocking=True, model=model)
    create_machine("mac", processing_delay="uniform(3, 4)", model=model)
    create_machine("test", processing_delay="uniform(2, 3)",
                   route_shares={"b_out": 0.8, "b_back": 0.2}, model=model)
    create_sink("snk", model=model)
    for edge_id, src, dest in (("b_in", "src", "mac"), ("b_back", "test", "mac"),
                               ("b_test", "mac", "test"), ("b_out", "test", "snk")):
        create_buffer(edge_id, capacity=5, model=model)
        connect(edge_id, src, dest, model=model)
    rows = [("b_in", 0, 0), ("mac", 0, 4), ("b_test", 4, 4), ("test", 4, 7),
            ("b_back", 7, 7), ("mac", 7, 11), ("b_test", 11, 11), ("test", 11, 14),
            ("b_out", 14, 14)]
    passes = {setting: {"delivered": {"src": (rows, None)}, "edges": set(model.edges)}
              for setting in (LOWER, ENTERED, UPPER)}

    stations = {row["id"]: row for row in _station_rows(model.spec, passes, {}, 3.0, 0.99)}

    assert stations["mac"][UPPER] == pytest.approx(4)
    assert stations["test"][UPPER] == pytest.approx(3)
    assert stations["mac"]["parts"][UPPER] == 1
    upper = [f for f in _check_stations(list(stations.values()))
             if f["check"] == "time_mismatch" and f"{UPPER} setting" in f["message"]]
    assert upper == []


# =========================================================================
# combinations — every station at its lower or upper bound
# =========================================================================


def two_machines(model, first="uniform(4, 6)", second="uniform(1, 2)", breakdowns=False):
    """src -> b1 -> m1 -> b2 -> m2 -> b3 -> snk, arrivals every 3.

    With `breakdowns`, m1 breaks down on average every 120 for 10.
    """
    create_source("src", inter_arrival_time="exp(3)", blocking=True, model=model)
    failures = {"time_to_failure": "exp(120)", "repair_time": "exp(10)"} if breakdowns else {}
    create_machine("m1", processing_delay=first, model=model, **failures)
    create_machine("m2", processing_delay=second, model=model)
    create_sink("snk", model=model)
    create_buffer("b1", capacity=5, model=model)
    create_buffer("b2", capacity=5, model=model)
    create_buffer("b3", capacity=5, model=model)
    connect("b1", "src", "m1", model=model)
    connect("b2", "m1", "m2", model=model)
    connect("b3", "m2", "snk", model=model)
    return model


def test_only_stations_with_a_distribution_are_varied(model):
    serial(model, processing_delay="uniform(4, 6)", hold=5)

    assert ranged_stations(model.spec) == ["mac"]


def test_each_named_station_goes_to_its_own_bound(model):
    two_machines(model)

    spec = spec_with(model.spec, {"m1": UPPER, "m2": LOWER})
    by_id = {s["kwargs"]["id"]: s["kwargs"] for s in spec if "id" in s["kwargs"]}

    assert by_id["m1"]["processing_delay"] == 6.0
    assert by_id["m2"]["processing_delay"] == 1.0
    assert by_id["src"]["inter_arrival_time"] == "exp(3)"


def test_every_combination_runs_while_there_are_few_stations():
    masks, complete = combination_masks(3)

    assert masks == list(range(8))
    assert complete is True


def test_past_the_limit_only_the_corners_and_single_stations_run():
    count = MAX_COMBINATIONS.bit_length()
    masks, complete = combination_masks(count)
    every = (1 << count) - 1

    assert complete is False
    assert len(masks) == 2 * count + 2
    assert {0, every} <= set(masks)
    assert all(1 << i in masks and every ^ (1 << i) in masks for i in range(count))


def test_the_slowest_station_has_the_most_influence(model):
    report = verify_extreme_conditions(480, seed=3, model=two_machines(model))

    combos = report["combinations"]
    assert len(combos["runs"]) == 4 * combos["replications"]
    assert combos["complete"] is True
    assert combos["influence"][0]["component"] == "m1"
    assert report["passed"] is True


def test_a_station_that_never_limits_the_line_is_pointed_out(model):
    # m2 is far quicker than the arrivals at either bound.
    two_machines(model, first="uniform(4, 6)", second="uniform(0.1, 0.2)")

    report = verify_extreme_conditions(480, seed=3, model=model)

    hint = next(f for f in report["findings"] if f["check"] == "no_influence")
    assert hint["component"] == "m2"
    assert hint["severity"] == "hint"


def test_the_same_seed_gives_the_same_combinations(model):
    two_machines(model)

    first = verify_extreme_conditions(480, seed=3, model=model)
    second = verify_extreme_conditions(480, seed=3, model=model)

    def counts(report):
        return [run["received"] for run in report["combinations"]["runs"]]

    assert first["combinations"]["seed"] == 3
    assert counts(first) == counts(second)


def test_each_combination_records_how_each_station_spent_the_run(model):
    report = verify_extreme_conditions(480, seed=3, model=two_machines(model))

    run = report["combinations"]["runs"][0]
    assert set(run["shares"]) == {"m1", "m2"}
    for shares in run["shares"].values():
        assert set(shares) == {"working", "waiting", "blocked", "down"}
        assert 0.0 <= sum(shares.values()) <= 1.0 + 1e-9


def test_each_combination_records_what_each_station_processed(model):
    report = verify_extreme_conditions(480, seed=3, model=two_machines(model))

    for run in report["combinations"]["runs"]:
        assert set(run["processed"]) == {"m1", "m2"}
        # m2 finishes what m1 hands on, less what is still between them.
        assert run["processed"]["m1"] >= run["processed"]["m2"] >= run["received"]


def test_the_slow_station_works_more_than_the_quick_one(model):
    # m1 at 4 to 6 against arrivals every 3 is near full; m2 at 0.1 to 0.2 idles.
    two_machines(model, first="uniform(4, 6)", second="uniform(0.1, 0.2)")

    report = verify_extreme_conditions(480, seed=3, model=model)

    rows = {row["component"]: row for row in report["combinations"]["time_shares"]}
    assert rows["m1"]["working"]["mean"] > 0.9
    assert rows["m2"]["working"]["mean"] < 0.1
    assert rows["m1"]["runs"] == 4 * report["combinations"]["replications"]


@pytest.mark.parametrize(
    "value, real",
    [
        (5, True),
        ("uniform(4, 6)", True),
        ("normal(20, 3)", False),
        ("gauss(8, 1)", False),
        ("exp(20)", False),
    ],
)
def test_only_constants_and_uniform_have_a_real_lower_bound(value, real):
    assert has_real_floor(value) is real


def test_the_floor_of_a_route_adds_its_lower_bounds(model):
    serial(model, processing_delay="uniform(4, 6)", hold="normal(20, 3)")

    total, real = route_floor(model.spec, ["b1", "mac", "bath"])

    assert total == pytest.approx(15.0)
    assert real is False


def test_a_station_drawn_below_a_normal_bound_is_only_a_hint(model, monkeypatch):
    serial(model, hold="normal(20, 3)")
    run_simulation(300, seed=1, model=model)
    # Stand in for a rare draw: every stay in the bath reads as 7.
    import simtrace.tools.validation.extreme_conditions as ec

    real_stays = ec.part_stays

    def short_bath(events):
        return {
            item: [(p, a, a + 7.0 if p == "bath" else b) for p, a, b in rows]
            for item, rows in real_stays(events).items()
        }

    monkeypatch.setattr(ec, "part_stays", short_bath)
    report = verify_extreme_conditions(400, seed=1, model=model)

    finding = next(f for f in report["findings"] if f["check"] == "below_station_floor")
    assert finding["severity"] == "hint"
    assert finding["component"] == "bath"


def test_one_part_either_way_still_counts_as_no_influence():
    runs = [
        {"at": {"m1": LOWER, "m2": LOWER}, "failed": None, "received": 80, "missing": 0},
        {"at": {"m1": UPPER, "m2": LOWER}, "failed": None, "received": 79, "missing": 0},
        {"at": {"m1": LOWER, "m2": UPPER}, "failed": None, "received": 60, "missing": 0},
        {"at": {"m1": UPPER, "m2": UPPER}, "failed": None, "received": 60, "missing": 0},
    ]

    findings, _ = _check_combinations(["m1", "m2"], [0, 1, 2, 3], runs, 480)

    hinted = {f["component"] for f in findings if f["check"] == "no_influence"}
    assert hinted == {"m1"}


def test_more_output_from_a_longer_time_is_reported():
    runs = [
        {"at": {"m1": LOWER}, "failed": None, "received": 80, "missing": 0},
        {"at": {"m1": UPPER}, "failed": None, "received": 85, "missing": 0},
    ]

    findings, influence = _check_combinations(["m1"], [0, 1], runs, 480)

    finding = next(f for f in findings if f["check"] == "longer_time_more_output")
    assert finding["component"] == "m1"
    assert finding["severity"] == "error"
    assert influence[0]["mean"] == -5


def replicated(effects_m1):
    """Runs for one station over five replications: lower bound at 80 parts,
    upper bound at 80 minus each effect."""
    runs, masks = [], []
    for rep, effect in enumerate(effects_m1):
        runs.append({"at": {"m1": LOWER}, "failed": None, "received": 80,
                     "missing": 0, "replication": rep})
        masks.append(0)
        runs.append({"at": {"m1": UPPER}, "failed": None, "received": 80 - effect,
                     "missing": 0, "replication": rep})
        masks.append(1)
    return masks, runs


def test_noise_over_the_replications_is_not_more_output():
    masks, runs = replicated([4, -3, 2, -2, 1])

    findings, influence = _check_combinations(["m1"], masks, runs, 480)

    assert not any(f["check"] == "longer_time_more_output" for f in findings)
    assert influence[0]["replications"] == 5
    assert influence[0]["half_width"] > 0


def test_more_output_in_every_replication_is_reported():
    masks, runs = replicated([-5, -4, -6, -5, -5])

    findings, influence = _check_combinations(["m1"], masks, runs, 480)

    finding = next(f for f in findings if f["check"] == "longer_time_more_output")
    assert finding["component"] == "m1"
    assert influence[0]["mean"] == -5


def test_each_combination_runs_once_per_replication(model):
    # The route shares at check still draw in every combination.
    report = verify_extreme_conditions(120, seed=3, replications=3, model=two_types(model))

    combos = report["combinations"]
    assert combos["random"] == ["route shares at 'check'"]
    assert combos["replications"] == 3
    assert len(combos["runs"]) == 4 * 3
    seeds = {(tuple(r["at"].values()), r["seed"]) for r in combos["runs"]}
    assert len(seeds) == 12
    assert {r["seed"] for r in combos["runs"]} == {3, 1003, 2003}


def test_a_combination_that_raises_is_reported():
    runs = [
        {"at": {"m1": LOWER}, "failed": "AssertionError: boom", "received": None,
         "missing": None},
        {"at": {"m1": UPPER}, "failed": None, "received": 10, "missing": 0},
    ]

    findings, _ = _check_combinations(["m1"], [0, 1], runs, 480)

    assert any(f["check"] == "combination_failed" for f in findings)


def test_the_combination_copies_leave_the_session_model_alone(model):
    two_machines(model)
    verify_extreme_conditions(200, seed=1, model=model)

    assert model.env.now == 0
    assert model.events == []


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
        {"until": 100, "replications": 0},
        {"until": 100, "replications": 2.0},
        {"until": 100, "quantile": 1},
        {"until": 100, "quantile": 0},
    ],
)
def test_bad_settings_are_rejected(model, kwargs):
    serial(model)
    with pytest.raises(ValueError):
        verify_extreme_conditions(model=model, **kwargs)


# =========================================================================
# extreme inputs — each case with what a real plant would do
# =========================================================================


def test_a_correct_line_meets_every_extreme_input(model):
    report = verify_extreme_conditions(480, seed=3, model=two_machines(model))

    cases = {row["case"]: row for row in report["inputs"]["cases"]}
    assert set(cases) == {
        "no_arrivals", "arrivals_x10", "endless_buffers", "minimal_buffers", "zero_times"
    }
    assert all(row["ok"] for row in cases.values())
    assert cases["no_arrivals"]["received"] == 0
    assert cases["arrivals_x10"]["received"] >= cases["arrivals_x10"]["as_built"]
    assert cases["minimal_buffers"]["received"] > 0
    assert cases["zero_times"]["received"] >= cases["zero_times"]["as_built"]
    assert report["passed"] is True


def test_ten_times_the_arrivals_divides_every_argument(model):
    create_source("a", inter_arrival_time="exp(3)", model=model)
    create_source("b", inter_arrival_time=2, model=model)
    create_source("c", inter_arrival_time="uniform(4, 6)", model=model)

    rewritten = {s["kwargs"]["id"]: s["kwargs"]["inter_arrival_time"]
                 for s in scaled_arrivals(model.spec, 10)}

    assert rewritten == {"a": "exp(0.3)", "b": 0.2, "c": "uniform(0.4, 0.6)"}


def test_endless_buffers_change_only_the_capacity(model):
    serial(model, hold=5)

    rewritten = [s["kwargs"] for s in endless_buffers(model.spec) if s["op"] == "create_buffer"]

    assert {kw["capacity"] for kw in rewritten} == {ENDLESS}
    assert {kw["delay"] for kw in rewritten} == {0, 5}


def input_run(case, seed=0, received=10, generated=10, blocked=None):
    return {"case": case, "seed": seed, "failed": None, "received": received,
            "generated": generated, "missing": 0, "blocked": blocked or {}}


def test_output_from_no_arrivals_is_reported():
    findings, rows = _check_inputs([input_run("no_arrivals", received=3, generated=3)])

    assert any(f["check"] == "input_no_arrivals_output" for f in findings)
    assert next(r for r in rows if r["case"] == "no_arrivals")["ok"] is False


def test_less_output_from_more_arrivals_is_reported():
    runs = [input_run("as_built", seed=s, received=100) for s in range(5)]
    runs += [input_run("arrivals_x10", seed=s, received=90 + s % 2) for s in range(5)]

    findings, _ = _check_inputs(runs)

    assert any(f["check"] == "input_more_arrivals_less_output" for f in findings)


def test_noise_with_more_arrivals_is_not_reported():
    runs = [input_run("as_built", seed=s, received=100) for s in range(5)]
    runs += [input_run("arrivals_x10", seed=s, received=r)
             for s, r in enumerate([96, 104, 99, 102, 97])]

    findings, _ = _check_inputs(runs)

    assert not any(f["check"] == "input_more_arrivals_less_output" for f in findings)


def test_a_station_blocked_behind_endless_buffers_is_reported():
    findings, _ = _check_inputs([input_run("endless_buffers", blocked={"m1": 0.2})])

    finding = next(f for f in findings if f["check"] == "input_blocked_with_endless_buffers")
    assert finding["component"] == "m1"


# =========================================================================
# targeted extremes — one setting past its bounds, for the reader to judge
# =========================================================================


def two_types(model):
    """Two sources, one route per type, a test that ships or scraps.

    src_a -> sort -> m_a -> check -> good | bad (scrap)
    src_b -> sort -> m_b -> check
    """
    create_source("src_a", inter_arrival_time=4, blocking=True, part_type="a",
                  attributes={"kind": "a"}, model=model)
    create_source("src_b", inter_arrival_time=6, blocking=True, part_type="b",
                  attributes={"kind": "b"}, model=model)
    create_machine("sort", processing_delay=0.5, route_by="kind",
                   routes={"a": "to_a", "b": "to_b"}, model=model)
    create_machine("m_a", processing_delay="uniform(1, 2)", model=model)
    create_machine("m_b", processing_delay="uniform(1, 2)", model=model)
    create_machine("check", processing_delay=0.5,
                   route_shares={"to_good": 0.9, "to_bad": 0.1}, model=model)
    create_sink("good", model=model)
    create_sink("bad", scrap=True, model=model)
    for edge, src, dest in (
        ("in_a", "src_a", "sort"), ("in_b", "src_b", "sort"),
        ("to_a", "sort", "m_a"), ("to_b", "sort", "m_b"),
        ("from_a", "m_a", "check"), ("from_b", "m_b", "check"),
        ("to_good", "check", "good"), ("to_bad", "check", "bad"),
    ):
        create_buffer(edge, capacity=5, model=model)
        connect(edge, src, dest, model=model)
    return model


def test_the_plan_stops_every_station_and_tries_every_branch_and_source(model):
    plan = targeted_plan(two_types(model).spec)

    assert [t for c, t, _ in plan if c == "station_stopped"] == ["sort", "m_a", "m_b", "check"]
    assert [(t, e) for c, t, e in plan if c == "one_branch"] == [
        ("check", "to_good"), ("check", "to_bad")]
    assert [t for c, t, _ in plan if c == "one_source"] == ["src_a", "src_b"]


def test_a_single_source_gets_no_source_case(model):
    plan = targeted_plan(serial(model).spec)

    assert {case for case, _, _ in plan} == {"station_stopped"}


def test_a_stopped_station_holds_its_part_past_the_run(model):
    rewritten = stopped_station(serial(model).spec, "mac", 100)

    mac = next(s["kwargs"] for s in rewritten if s["kwargs"].get("id") == "mac")
    assert mac["processing_delay"] == 1000


def test_one_branch_gets_every_part(model):
    rewritten = one_branch(two_types(model).spec, "check", "to_bad")

    check = next(s["kwargs"] for s in rewritten if s["kwargs"].get("id") == "check")
    assert check["route_shares"] == {"to_good": 0.0, "to_bad": 1.0}


def test_the_targeted_runs_read_as_the_wiring_says(model):
    report = verify_extreme_conditions(120, seed=2, model=two_types(model))

    runs = {(r["case"], r["target"], r["edge"]): r for r in report["targeted"]["runs"]}
    # The station every part passes: nothing reaches a sink.
    assert runs[("station_stopped", "check", None)]["received"] == 0
    # A station on one type's route: the other type still arrives, by its own route.
    stopped_a = runs[("station_stopped", "m_a", None)]
    assert stopped_a["received"] > 0
    assert all("m_a" not in r["route"] for r in stopped_a["routes"])
    # Every part to the scrap branch: all counted as scrapped.
    scrap = runs[("one_branch", "check", "to_bad")]
    assert scrap["sinks"]["good"] == 0
    assert sum(t["scrapped"] for t in scrap["types"].values()) == scrap["received"] > 0
    # One source alone: only its own station works.
    alone = runs[("one_source", "src_a", None)]
    assert alone["processed"]["m_a"] > 0
    assert alone["processed"]["m_b"] == 0
    assert report["targeted"]["as_built"]["received"] > 0
    assert "targeted extremes" in report["summary"]


# =========================================================================
# saturation and randomness in the combinations
# =========================================================================


def test_one_source_keeps_its_mix_when_kept_busy(model):
    assert mix_is_fixed(two_machines(model).spec) is True


def test_two_sources_at_a_shared_station_can_shift_the_mix(model):
    assert mix_is_fixed(two_types(model).spec) is False


def test_two_separate_lines_keep_their_mix(model):
    for name in ("a", "b"):
        create_source(f"src_{name}", inter_arrival_time=4, blocking=True, model=model)
        create_machine(f"m_{name}", processing_delay=1, model=model)
        create_sink(f"snk_{name}", model=model)
        create_buffer(f"in_{name}", capacity=5, model=model)
        create_buffer(f"out_{name}", capacity=5, model=model)
        connect(f"in_{name}", f"src_{name}", f"m_{name}", model=model)
        connect(f"out_{name}", f"m_{name}", f"snk_{name}", model=model)

    assert mix_is_fixed(model.spec) is True


def assembly(model, bypass=False):
    """src_a and src_b meet first at a combiner; with `bypass`, src_b also feeds m2."""
    create_source("src_a", inter_arrival_time=4, blocking=True, model=model)
    create_source("src_b", inter_arrival_time=4, blocking=True, model=model)
    create_combiner("join", processing_delay=1, model=model)
    create_machine("m2", processing_delay="uniform(1, 2)", model=model)
    create_sink("snk", model=model)
    wiring = [("a_in", "src_a", "join"), ("b_in", "src_b", "join"),
              ("mid", "join", "m2"), ("out", "m2", "snk")]
    if bypass:
        wiring.append(("b_by", "src_b", "m2"))
    for edge, src, dest in wiring:
        create_buffer(edge, capacity=5, model=model)
        connect(edge, src, dest, model=model)
    return model


def test_sources_that_meet_first_at_a_combiner_keep_their_mix(model):
    assert mix_is_fixed(assembly(model).spec) is True


def test_a_source_that_also_bypasses_the_combiner_can_shift_the_mix(model):
    assert mix_is_fixed(assembly(model, bypass=True).spec) is False


def test_a_source_releases_what_its_combiner_takes_from_its_edge(model):
    create_source("parts", inter_arrival_time=2, blocking=True, model=model)
    create_source("pallets", inter_arrival_time=8, blocking=True, model=model)
    create_machine("checking", processing_delay=1, model=model)
    create_combiner("packing", target_quantity_of_each_item=[1, 4], model=model)
    create_sink("snk", model=model)
    for edge, src, dest in [("b_pallets", "pallets", "packing"), ("b_in", "parts", "checking"),
                            ("b_checked", "checking", "packing"), ("b_out", "packing", "snk")]:
        create_buffer(edge, capacity=5, model=model)
        connect(edge, src, dest, model=model)

    assert combiner_release(model.spec) == {"parts": 4, "pallets": 1}
    result = verify_extreme_conditions(100, seed=1, model=model)
    assert result["settings"]["released"] == {"parts": 4, "pallets": 1}
    assert not [f for f in result["findings"] if f["check"] == "part_stuck"]


def test_a_route_with_no_time_at_the_lower_bounds_is_found(model):
    assert zero_time_route(serial(model, processing_delay="exp(5)").spec) is True


def test_a_route_with_time_at_the_lower_bounds_is_not_zero(model):
    assert zero_time_route(serial(model, processing_delay="uniform(4, 6)").spec) is False


def test_kept_busy_every_source_waits_and_releases_at_once(model):
    rewritten = saturated_arrivals(two_types(model).spec)

    sources = [s["kwargs"] for s in rewritten if s["op"] == "create_source"]
    assert {(kw["inter_arrival_time"], kw["blocking"]) for kw in sources} == {(0, True)}


def test_what_still_draws_is_listed(model):
    assert random_elements(two_types(model).spec) == ["route shares at 'check'"]
    assert random_elements(two_machines(FactoryModel()).spec) == [
        "arrivals at 'src' (exp(3))"
    ]


def test_constant_breakdowns_do_not_draw(model):
    create_machine("m", processing_delay=1, time_to_failure=100, repair_time=5, model=model)

    assert random_elements(model.spec) == []


def test_a_saturated_line_without_randomness_runs_once_per_combination(model):
    report = verify_extreme_conditions(480, seed=3, model=two_machines(model))

    combos = report["combinations"]
    assert combos["arrivals"] == "saturated"
    assert combos["random"] == []
    assert combos["replications"] == 1
    assert len(combos["runs"]) == 4
    # The check run at another seed delivers the same.
    assert combos["repeat"]["received"] == combos["runs"][0]["received"]
    assert not any(f["check"] == "combination_not_repeatable" for f in report["findings"])
    # Kept busy, m1 at 4 limits the line in the first combination: about 480 / 4.
    assert 110 <= combos["runs"][0]["received"] <= 120


def test_a_shared_station_keeps_the_arrivals_as_entered(model):
    report = verify_extreme_conditions(120, seed=2, model=two_types(model))

    assert report["combinations"]["arrivals"] == "as entered"
    types = {row["type"] for row in report["combinations"]["types"]}
    assert types == {"a", "b"}


def test_a_zero_time_route_keeps_the_arrivals_as_entered(model):
    report = verify_extreme_conditions(
        200, seed=1, model=serial(model, processing_delay="exp(5)")
    )

    assert report["combinations"]["arrivals"] == "as entered"
    assert "no time" in report["combinations"]["arrivals_why"]


def test_more_output_is_only_a_hint_where_the_model_can_shift_work():
    runs = [
        {"at": {"m1": LOWER}, "failed": None, "received": 80, "missing": 0},
        {"at": {"m1": UPPER}, "failed": None, "received": 85, "missing": 0},
    ]

    findings, _ = _check_combinations(
        ["m1"], [0, 1], runs, 480, soft=["routing by 'kind' at 'sort'"]
    )

    finding = next(f for f in findings if f["check"] == "longer_time_more_output")
    assert finding["severity"] == "hint"
    assert "routing by 'kind'" in finding["message"]


def test_a_line_limited_by_its_arrivals_is_named_as_such():
    runs = [
        {"at": {"m1": level}, "failed": None, "received": 80, "missing": 0,
         "sources_blocked": {"src": 0.0}}
        for level in (LOWER, UPPER)
    ]

    findings, _ = _check_combinations(["m1"], [0, 1], runs, 480)

    hint = next(f for f in findings if f["check"] == "no_influence")
    assert "arrivals limit the line" in hint["message"]


def test_soft_reasons_name_priority_routing_and_combiners(model):
    found = soft_reasons(two_types(model).spec)

    assert "a priority between the in-edges of 'check'" in found
    assert "routing by 'kind' at 'sort'" in found
    assert soft_reasons(two_machines(FactoryModel()).spec) == []
    assert "the combiner 'join'" in soft_reasons(assembly(FactoryModel()).spec)


# =========================================================================
# the new extreme inputs
# =========================================================================


def test_no_breakdowns_runs_only_where_a_machine_breaks_down(model):
    assert NO_BREAKDOWNS not in input_plan(two_machines(model).spec)
    assert NO_BREAKDOWNS in input_plan(two_machines(FactoryModel(), breakdowns=True).spec)


def test_zero_times_keeps_buffer_delays(model):
    serial(model, processing_delay="uniform(4, 6)", hold=5)

    by_id = {s["kwargs"]["id"]: s["kwargs"] for s in zero_station_times(model.spec)
             if "id" in s["kwargs"]}

    assert by_id["mac"]["processing_delay"] == 0
    assert by_id["bath"]["delay"] == 5


def test_a_line_that_locks_up_with_one_place_per_buffer_is_reported():
    findings, rows = _check_inputs([input_run("minimal_buffers", received=0, generated=3)])

    assert any(f["check"] == "input_minimal_buffers_no_output" for f in findings)
    assert rows[0]["ok"] is False


def test_breakdowns_left_after_taking_them_out_are_reported():
    runs = [input_run("as_built", seed=s, received=100) for s in range(5)]
    runs += [{**input_run("no_breakdowns", seed=s, received=110), "breakdowns": 1}
             for s in range(5)]

    findings, rows = _check_inputs(runs)

    assert any(f["check"] == "input_breakdowns_left" for f in findings)
    assert next(r for r in rows if r["case"] == "no_breakdowns")["ok"] is False


def test_less_output_with_zero_station_times_is_reported():
    runs = [input_run("as_built", seed=s, received=100) for s in range(5)]
    runs += [input_run("zero_times", seed=s, received=80 + s % 2) for s in range(5)]

    findings, _ = _check_inputs(runs)

    assert any(f["check"] == "input_zero_times_less_output" for f in findings)


def test_a_line_with_breakdowns_meets_no_breakdowns(model):
    report = verify_extreme_conditions(
        480, seed=3, model=two_machines(model, breakdowns=True)
    )

    row = next(r for r in report["inputs"]["cases"] if r["case"] == "no_breakdowns")
    assert row["ok"] is True
    assert row["breakdowns"] == 0


def test_breakdowns_go_to_their_means(model):
    create_machine("m", processing_delay=1, time_to_failure="exp(220)",
                   repair_time="uniform(15, 25)", model=model)
    create_machine("n", processing_delay=1, time_to_failure=100, repair_time="normal(8, 2)",
                   model=model)

    by_id = {s["kwargs"]["id"]: s["kwargs"] for s in breakdowns_at_means(model.spec)}

    assert (by_id["m"]["time_to_failure"], by_id["m"]["repair_time"]) == (220, 20)
    assert (by_id["n"]["time_to_failure"], by_id["n"]["repair_time"]) == (100, 8)
    assert random_elements(breakdowns_at_means(model.spec)) == []


def test_a_line_with_breakdowns_runs_once_per_combination_when_kept_full(model):
    report = verify_extreme_conditions(
        480, seed=3, model=two_machines(model, breakdowns=True)
    )

    combos = report["combinations"]
    assert combos["breakdowns_at_means"] is True
    assert combos["arrivals"] == "saturated"
    assert combos["random"] == []
    assert len(combos["runs"]) == 4
