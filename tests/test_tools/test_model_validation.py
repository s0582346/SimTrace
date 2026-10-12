"""Tests for the static pre-run check validate_model."""

import pytest

from simtrace.model import FactoryModel
from simtrace.tools.builders import (
    create_buffer,
    create_combiner,
    create_conveyor,
    create_machine,
    create_sink,
    create_source,
    create_splitter,
)
from simtrace.tools.simulation import connect, run_simulation
from simtrace.tools.validation import validate_model


@pytest.fixture
def model() -> FactoryModel:
    return FactoryModel()


@pytest.fixture
def line(model) -> FactoryModel:
    """A clean src -> b1 -> mac -> b2 -> snk line with nothing to report."""
    create_source("src", inter_arrival_time=1, blocking=True, model=model)
    create_machine("mac", processing_delay=2, blocking=True, model=model)
    create_sink("snk", model=model)
    create_buffer("b1", capacity=5, model=model)
    create_buffer("b2", capacity=5, model=model)
    connect("b1", "src", "mac", model=model)
    connect("b2", "mac", "snk", model=model)
    return model


def checks(findings: list[dict]) -> set:
    return {finding["check"] for finding in findings}


def components(findings: list[dict], check: str) -> set:
    return {f["component"] for f in findings if f["check"] == check}


# --- happy path -----------------------------------------------------------


def test_clean_line_is_valid(line):
    report = validate_model(model=line)

    assert report["valid"] is True
    assert report["errors"] == []
    assert report["warnings"] == []
    assert report["checked"] == {"nodes": 3, "edges": 2}


def test_needs_no_run_and_survives_one(line):
    before = validate_model(model=line)
    run_simulation(20, model=line)
    after = validate_model(model=line)

    # Static: the verdict comes from the wiring, so running changes nothing.
    assert before == after


def test_empty_model_reports_empty_model(model):
    report = validate_model(model=model)

    assert report["valid"] is False
    assert checks(report["errors"]) == {"empty_model"}
    assert report["checked"] == {"nodes": 0, "edges": 0}


# --- errors ---------------------------------------------------------------


def test_unwired_source_fails_cardinality(model):
    create_source("src", blocking=True, model=model)
    create_sink("snk", model=model)
    create_buffer("b1", model=model)

    report = validate_model(model=model)

    assert report["valid"] is False
    assert "node_cardinality" in checks(report["errors"])
    # Both ends of the line are missing their edge, and so is the buffer.
    assert components(report["errors"], "node_cardinality") == {"src", "snk"}


def test_sink_with_an_out_edge_exceeds_its_maximum(model):
    create_source("src", blocking=True, model=model)
    create_sink("snk", model=model)
    create_machine("mac", processing_delay=1, model=model)
    create_buffer("b1", capacity=2, model=model)
    create_buffer("b2", capacity=2, model=model)
    connect("b1", "src", "snk", model=model)
    connect("b2", "snk", "mac", model=model)

    report = validate_model(model=model)

    assert "snk" in components(report["errors"], "node_cardinality")


def test_edge_created_but_never_connected(line):
    create_buffer("spare", capacity=3, model=line)

    report = validate_model(model=line)

    assert report["valid"] is False
    assert components(report["errors"], "orphan_edge") == {"spare"}


def test_combiner_quantities_must_match_in_edge_count(model):
    create_source("pal", flow_item_type="pallet", blocking=True, model=model)
    create_source("part", blocking=True, model=model)
    create_sink("snk", model=model)
    # Two in-edges are wired below, but only one count is declared here.
    create_combiner("cmb", target_quantity_of_each_item=[1], model=model)
    create_buffer("b_pal", capacity=2, model=model)
    create_buffer("b_part", capacity=2, model=model)
    create_buffer("b_out", capacity=2, model=model)
    connect("b_pal", "pal", "cmb", model=model)
    connect("b_part", "part", "cmb", model=model)
    connect("b_out", "cmb", "snk", model=model)

    report = validate_model(model=model)

    assert report["valid"] is False
    assert components(report["errors"], "combiner_quantities") == {"cmb"}


def test_combiner_quantities_pass_when_they_line_up(model):
    create_source("pal", flow_item_type="pallet", blocking=True, model=model)
    create_source("part", blocking=True, model=model)
    create_sink("snk", model=model)
    create_combiner("cmb", target_quantity_of_each_item=[1, 2], model=model)
    create_buffer("b_pal", capacity=2, model=model)
    create_buffer("b_part", capacity=2, model=model)
    create_buffer("b_out", capacity=2, model=model)
    connect("b_pal", "pal", "cmb", model=model)
    connect("b_part", "part", "cmb", model=model)
    connect("b_out", "cmb", "snk", model=model)

    report = validate_model(model=model)

    assert "combiner_quantities" not in checks(report["errors"])


def test_source_with_no_route_to_a_sink(model):
    create_source("src", blocking=True, model=model)
    create_machine("mac", processing_delay=1, blocking=True, model=model)
    create_sink("snk", model=model)
    create_buffer("b1", capacity=2, model=model)
    create_buffer("b2", capacity=2, model=model)
    # The second source's branch stops at a machine that leads nowhere.
    create_source("stray", blocking=True, model=model)
    create_machine("dead", processing_delay=1, blocking=True, model=model)
    create_buffer("b3", capacity=2, model=model)
    create_buffer("b4", capacity=2, model=model)
    create_machine("nowhere", processing_delay=1, blocking=True, model=model)
    connect("b1", "src", "mac", model=model)
    connect("b2", "mac", "snk", model=model)
    connect("b3", "stray", "dead", model=model)
    connect("b4", "dead", "nowhere", model=model)

    report = validate_model(model=model)

    assert report["valid"] is False
    reached = components(report["errors"], "reachability")
    assert "stray" in reached
    assert "dead" in reached
    # The clean branch is left alone.
    assert "src" not in reached
    assert "mac" not in reached


def test_model_without_a_sink(model):
    create_source("src", blocking=True, model=model)
    create_machine("mac", processing_delay=1, blocking=True, model=model)
    create_buffer("b1", capacity=2, model=model)
    connect("b1", "src", "mac", model=model)

    report = validate_model(model=model)

    assert report["valid"] is False
    assert None in components(report["errors"], "reachability")


def test_zero_interval_source_is_rejected_at_creation(model):
    # FactorySimPy refuses this pairing in Source.__init__, so no model can hold
    # one and validate_model never has to look for it.
    with pytest.raises(ValueError, match="non-zero inter_arrival_time"):
        create_source("src", inter_arrival_time=0, blocking=False, model=model)


# --- warnings -------------------------------------------------------------


def test_non_blocking_node_warns_about_silent_discard(model):
    create_source("src", inter_arrival_time=1, blocking=False, model=model)
    create_machine("mac", processing_delay=2, blocking=True, model=model)
    create_sink("snk", model=model)
    create_buffer("b1", capacity=1, model=model)
    create_buffer("b2", capacity=1, model=model)
    connect("b1", "src", "mac", model=model)
    connect("b2", "mac", "snk", model=model)

    report = validate_model(model=model)

    # A warning only: the model is runnable, it just loses items on the way.
    assert report["valid"] is True
    assert components(report["warnings"], "silent_discard") == {"src"}
    assert "b1" in report["warnings"][0]["message"]


def test_conveyor_item_length_disagreeing_with_its_source(model):
    create_source("src", item_length=2, blocking=True, model=model)
    create_machine("mac", processing_delay=1, blocking=True, model=model)
    create_sink("snk", model=model)
    create_conveyor(
        "belt", conveyor_length=10, speed=1, item_length=1, model=model
    )
    create_buffer("b2", capacity=2, model=model)
    connect("belt", "src", "mac", model=model)
    connect("b2", "mac", "snk", model=model)

    report = validate_model(model=model)

    assert report["valid"] is True
    assert components(report["warnings"], "conveyor_item_length") == {"belt"}


def test_matching_conveyor_item_length_is_quiet(model):
    create_source("src", item_length=2, blocking=True, model=model)
    create_machine("mac", processing_delay=1, blocking=True, model=model)
    create_sink("snk", model=model)
    create_conveyor(
        "belt", conveyor_length=10, speed=1, item_length=2, model=model
    )
    create_buffer("b2", capacity=2, model=model)
    connect("belt", "src", "mac", model=model)
    connect("b2", "mac", "snk", model=model)

    report = validate_model(model=model)

    assert report["warnings"] == []


def test_unpack_splitter_with_no_pallets_upstream(model):
    create_source("src", blocking=True, model=model)
    create_splitter("spl", mode="UNPACK", model=model)
    create_sink("snk", model=model)
    create_buffer("b1", capacity=2, model=model)
    create_buffer("b2", capacity=2, model=model)
    connect("b1", "src", "spl", model=model)
    connect("b2", "spl", "snk", model=model)

    report = validate_model(model=model)

    assert report["valid"] is True
    assert components(report["warnings"], "pallet_mismatch") == {"spl"}


def test_unpack_splitter_fed_by_a_pallet_source_is_warned_about(model):
    # A Source emits an *empty* pallet — only a Combiner fills one — so this
    # splitter has nothing to unpack and emits just the container.
    create_source("src", flow_item_type="pallet", blocking=True, model=model)
    create_splitter("spl", mode="UNPACK", model=model)
    create_sink("snk", model=model)
    create_buffer("b1", capacity=2, model=model)
    create_buffer("b2", capacity=2, model=model)
    connect("b1", "src", "spl", model=model)
    connect("b2", "spl", "snk", model=model)

    report = validate_model(model=model)

    assert report["valid"] is True
    assert components(report["warnings"], "pallet_mismatch") == {"spl"}


def test_split_mode_splitter_is_an_error(model):
    # FactorySimPy's splitter always unpacks: it reads the incoming item's
    # contents, which only a Pallet has, so a SPLIT splitter cannot run.
    create_source("src", blocking=True, model=model)
    create_splitter("spl", mode="SPLIT", split_quantity=3, model=model)
    create_sink("snk", model=model)
    create_buffer("b1", capacity=2, model=model)
    create_buffer("b2", capacity=2, model=model)
    connect("b1", "src", "spl", model=model)
    connect("b2", "spl", "snk", model=model)

    report = validate_model(model=model)

    assert report["valid"] is False
    assert components(report["errors"], "splitter_mode") == {"spl"}
    assert "pallet_mismatch" not in checks(report["warnings"])


def test_combiner_whose_pallet_edge_carries_plain_items(model):
    # The plain-item edge is connected first, so it becomes the pallet edge.
    create_source("part", blocking=True, model=model)
    create_source("pal", flow_item_type="pallet", blocking=True, model=model)
    create_combiner("cmb", target_quantity_of_each_item=[1, 1], model=model)
    create_sink("snk", model=model)
    create_buffer("b_part", capacity=2, model=model)
    create_buffer("b_pal", capacity=2, model=model)
    create_buffer("b_out", capacity=2, model=model)
    connect("b_part", "part", "cmb", model=model)
    connect("b_pal", "pal", "cmb", model=model)
    connect("b_out", "cmb", "snk", model=model)

    report = validate_model(model=model)

    assert components(report["warnings"], "pallet_mismatch") == {"cmb"}


def test_combiner_with_its_pallet_edge_first_is_quiet(model):
    create_source("pal", flow_item_type="pallet", blocking=True, model=model)
    create_source("part", blocking=True, model=model)
    create_combiner("cmb", target_quantity_of_each_item=[1, 1], model=model)
    create_sink("snk", model=model)
    create_buffer("b_pal", capacity=2, model=model)
    create_buffer("b_part", capacity=2, model=model)
    create_buffer("b_out", capacity=2, model=model)
    connect("b_pal", "pal", "cmb", model=model)
    connect("b_part", "part", "cmb", model=model)
    connect("b_out", "cmb", "snk", model=model)

    report = validate_model(model=model)

    assert "pallet_mismatch" not in checks(report["warnings"])


# --- branch symmetry ------------------------------------------------------


def parallel_line(model, second_branch_rejoins: bool) -> FactoryModel:
    """One machine feeding two identical stations that then merge again.

    With `second_branch_rejoins` False the second station is wired straight to
    the sink, so items taking it skip the shared station downstream.
    """
    create_source("src", inter_arrival_time=1, blocking=True, model=model)
    create_machine("split", processing_delay=1, out_edge_selection="ROUND_ROBIN",
                   model=model)
    create_machine("left", processing_delay=2, model=model)
    create_machine("right", processing_delay=2, model=model)
    create_machine("shared", processing_delay=1, model=model)
    create_sink("snk", model=model)
    for edge in ("b_in", "b_left", "b_right", "b_left_shared", "b_out"):
        create_buffer(edge, capacity=5, model=model)
    connect("b_in", "src", "split", model=model)
    connect("b_left", "split", "left", model=model)
    connect("b_right", "split", "right", model=model)
    connect("b_left_shared", "left", "shared", model=model)
    if second_branch_rejoins:
        create_buffer("b_right_shared", capacity=5, model=model)
        connect("b_right_shared", "right", "shared", model=model)
    else:
        create_buffer("b_bypass", capacity=5, model=model)
        connect("b_bypass", "right", "snk", model=model)
    connect("b_out", "shared", "snk", model=model)
    return model


def test_parallel_branches_that_rejoin_are_quiet(model):
    parallel_line(model, second_branch_rejoins=True)

    report = validate_model(model=model)

    assert "branch_symmetry" not in checks(report["warnings"])


def test_a_branch_that_skips_a_shared_station_is_warned_about(model):
    parallel_line(model, second_branch_rejoins=False)

    report = validate_model(model=model)

    assert components(report["warnings"], "branch_symmetry") == {"split"}
    message = next(
        f["message"] for f in report["warnings"] if f["check"] == "branch_symmetry"
    )
    assert "'left'" in message and "'right'" in message and "'shared'" in message


def test_the_skipped_branch_leaves_the_throughput_alone():
    """Why this needs its own check: the item count does not move.

    The bypassed station is not the bottleneck, so over a run of 400 both
    wirings deliver 395 items, give or take the one that the shorter route
    gets to the sink before the clock stops. Nothing in a throughput figure
    can show the difference.
    """
    good = run_simulation(400, model=parallel_line(
        FactoryModel(), second_branch_rejoins=True))
    bad = run_simulation(400, model=parallel_line(
        FactoryModel(), second_branch_rejoins=False))

    delivered = good["nodes"]["snk"]["num_item_received"]
    bypassed = bad["nodes"]["snk"]["num_item_received"]
    assert abs(delivered - bypassed) <= 1


def test_a_splitter_may_send_its_branches_different_ways(model):
    create_source("src", flow_item_type="pallet", inter_arrival_time=5,
                  blocking=True, model=model)
    create_splitter("unpack", processing_delay=1, model=model)
    create_machine("finish", processing_delay=1, model=model)
    create_sink("parts", model=model)
    create_sink("empties", model=model)
    for edge in ("b_in", "b_parts", "b_empties", "b_out"):
        create_buffer(edge, capacity=5, model=model)
    connect("b_in", "src", "unpack", model=model)
    connect("b_parts", "unpack", "finish", model=model)
    connect("b_out", "finish", "parts", model=model)
    connect("b_empties", "unpack", "empties", model=model)

    report = validate_model(model=model)

    assert "branch_symmetry" not in checks(report["warnings"])


def test_two_streams_meeting_at_a_shared_station_are_quiet(model):
    """Fan-in is not fan-out: two sources with their own routes are fine."""
    create_source("std", inter_arrival_time=2, blocking=True, model=model)
    create_source("prem", inter_arrival_time=3, blocking=True, model=model)
    create_machine("mill_std", processing_delay=2, model=model)
    create_machine("mill_prem", processing_delay=2, model=model)
    create_machine("assembly", processing_delay=1, model=model)
    create_sink("snk", model=model)
    for edge in ("b_std", "b_prem", "b_std_asm", "b_out"):
        create_buffer(edge, capacity=5, model=model)
    create_buffer("oven", capacity=5, delay=10, model=model)
    connect("b_std", "std", "mill_std", model=model)
    connect("b_prem", "prem", "mill_prem", model=model)
    connect("b_std_asm", "mill_std", "assembly", model=model)
    connect("oven", "mill_prem", "assembly", model=model)
    connect("b_out", "assembly", "snk", model=model)

    report = validate_model(model=model)

    assert "branch_symmetry" not in checks(report["warnings"])


# --- report shape ---------------------------------------------------------


def test_every_finding_names_its_check_and_what_to_do(model):
    create_source("src", blocking=True, model=model)
    create_buffer("spare", model=model)

    report = validate_model(model=model)

    for finding in report["errors"] + report["warnings"]:
        assert set(finding) == {"check", "severity", "component", "message"}
        assert finding["severity"] in ("error", "warning")
        assert finding["message"]


# --- routing ----------------------------------------------------------------


def sorting_line(model, routes: dict, families=("A", "B")) -> FactoryModel:
    """One typed Source per family into a sorting machine; one sink per edge.

    Every edge id named in `routes` is wired from the sorter to its own sink.
    """
    for family in families:
        src_id = f"src_{family}"
        create_source(src_id, inter_arrival_time=2, blocking=True,
                      part_type=src_id, attributes={"family": family},
                      model=model)
        create_buffer(f"b_{src_id}", capacity=5, model=model)
    create_machine("sort", route_by="family", routes=routes, model=model)
    for family in families:
        connect(f"b_src_{family}", f"src_{family}", "sort", model=model)
    for edge_id in dict.fromkeys(routes.values()):
        create_buffer(edge_id, capacity=5, model=model)
        create_sink(f"snk_{edge_id}", model=model)
        connect(edge_id, "sort", f"snk_{edge_id}", model=model)
    return model


def test_routing_machine_that_fits_is_quiet(model):
    sorting_line(model, {"A": "b_a", "B": "b_b"})

    report = validate_model(model=model)

    assert report["valid"]
    assert report["errors"] == [] and report["warnings"] == []


def family_sources(model, into: str, families=("A", "B")) -> None:
    """One typed Source per family, each wired through a buffer into `into`."""
    for family in families:
        src_id = f"src_{family}"
        create_source(src_id, inter_arrival_time=2, blocking=True,
                      part_type=src_id, attributes={"family": family},
                      model=model)
        create_buffer(f"b_{src_id}", capacity=5, model=model)
        connect(f"b_{src_id}", src_id, into, model=model)


def wire(model, edge_id: str, src_id: str, dest_id: str) -> None:
    create_buffer(edge_id, capacity=5, model=model)
    connect(edge_id, src_id, dest_id, model=model)


def test_routing_machine_is_exempt_from_branch_symmetry(model):
    # sort -> b_a -> paint -> snk_a and sort -> b_b -> snk_b: only A is painted.
    create_machine("sort", route_by="family",
                   routes={"A": "b_a", "B": "b_b"}, model=model)
    create_machine("paint", processing_delay=1, model=model)
    create_sink("snk_a", model=model)
    create_sink("snk_b", model=model)
    family_sources(model, into="sort")
    wire(model, "b_a", "sort", "paint")
    wire(model, "b_p", "paint", "snk_a")
    wire(model, "b_b", "sort", "snk_b")

    report = validate_model(model=model)

    assert report["valid"]
    assert report["warnings"] == []


def test_same_layout_without_routing_still_warns(model):
    create_machine("split", model=model)
    create_machine("paint", processing_delay=1, model=model)
    create_sink("snk_a", model=model)
    create_sink("snk_b", model=model)
    family_sources(model, into="split")
    wire(model, "b_a", "split", "paint")
    wire(model, "b_p", "paint", "snk_a")
    wire(model, "b_b", "split", "snk_b")

    report = validate_model(model=model)

    assert components(report["warnings"], "branch_symmetry") == {"split"}


def test_route_to_an_edge_the_machine_is_not_wired_to(model):
    sorting_line(model, {"A": "b_a", "B": "b_b"})
    model.nodes["sort"].routes["B"] = "b_src_A"

    report = validate_model(model=model)

    assert components(report["errors"], "route_target") == {"sort"}
    assert not report["valid"]


def test_unmatched_family_without_fallback(model):
    sorting_line(model, {"A": "b_a"})

    report = validate_model(model=model)

    unmatched = [f for f in report["errors"] if f["check"] == "route_unmatched"]
    assert len(unmatched) == 1
    assert "src_B" in unmatched[0]["message"]
    assert "family='B'" in unmatched[0]["message"]


def test_fallback_covers_every_other_family(model):
    sorting_line(model, {"A": "b_a", "*": "b_rest"}, families=("A", "B", "C"))

    report = validate_model(model=model)

    assert "route_unmatched" not in checks(report["errors"])


def test_untyped_source_reaching_a_routing_machine(model):
    sorting_line(model, {"A": "b_a", "B": "b_b"})
    create_source("src_plain", inter_arrival_time=2, blocking=True, model=model)
    create_buffer("b_plain", capacity=5, model=model)
    connect("b_plain", "src_plain", "sort", model=model)

    report = validate_model(model=model)

    unmatched = [f for f in report["errors"] if f["check"] == "route_unmatched"]
    assert len(unmatched) == 1
    assert "src_plain" in unmatched[0]["message"]
    assert "no part type" in unmatched[0]["message"]


def test_out_edge_no_route_names_is_warned_about(model):
    sorting_line(model, {"A": "b_a", "B": "b_b"})
    create_buffer("b_spare", capacity=5, model=model)
    create_sink("snk_spare", model=model)
    connect("b_spare", "sort", "snk_spare", model=model)

    report = validate_model(model=model)

    unused = [f for f in report["warnings"] if f["check"] == "route_unused_edge"]
    assert len(unused) == 1
    assert "b_spare" in unused[0]["message"]


def two_sorters(model, first_routes: dict) -> FactoryModel:
    """sort -> b_a -> sort2 -> b_a2 -> snk_a, and sort -> b_b -> snk_b.

    sort2 only has a route for family A.
    """
    create_machine("sort", route_by="family", routes=first_routes, model=model)
    create_machine("sort2", route_by="family", routes={"A": "b_a2"}, model=model)
    create_sink("snk_a", model=model)
    create_sink("snk_b", model=model)
    family_sources(model, into="sort")
    wire(model, "b_a", "sort", "sort2")
    wire(model, "b_a2", "sort2", "snk_a")
    wire(model, "b_b", "sort", "snk_b")
    return model


def test_second_sorter_sees_only_what_the_first_sends_it(model):
    two_sorters(model, {"A": "b_a", "B": "b_b"})

    report = validate_model(model=model)

    assert report["valid"]


def test_second_sorter_gets_a_family_it_has_no_route_for(model):
    two_sorters(model, {"A": "b_a", "B": "b_a"})

    report = validate_model(model=model)

    unmatched = [f for f in report["errors"] if f["check"] == "route_unmatched"]
    assert [f["component"] for f in unmatched] == ["sort2"]
    assert "src_B" in unmatched[0]["message"]
