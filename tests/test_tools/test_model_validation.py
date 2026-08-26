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


def test_unpack_splitter_fed_by_a_pallet_source_is_quiet(model):
    create_source("src", flow_item_type="pallet", blocking=True, model=model)
    create_splitter("spl", mode="UNPACK", model=model)
    create_sink("snk", model=model)
    create_buffer("b1", capacity=2, model=model)
    create_buffer("b2", capacity=2, model=model)
    connect("b1", "src", "spl", model=model)
    connect("b2", "spl", "snk", model=model)

    report = validate_model(model=model)

    assert "pallet_mismatch" not in checks(report["warnings"])


def test_split_mode_splitter_needs_no_pallet(model):
    create_source("src", blocking=True, model=model)
    create_splitter("spl", mode="SPLIT", split_quantity=3, model=model)
    create_sink("snk", model=model)
    create_buffer("b1", capacity=2, model=model)
    create_buffer("b2", capacity=2, model=model)
    connect("b1", "src", "spl", model=model)
    connect("b2", "spl", "snk", model=model)

    report = validate_model(model=model)

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


# --- report shape ---------------------------------------------------------


def test_every_finding_names_its_check_and_what_to_do(model):
    create_source("src", blocking=True, model=model)
    create_buffer("spare", model=model)

    report = validate_model(model=model)

    for finding in report["errors"] + report["warnings"]:
        assert set(finding) == {"check", "severity", "component", "message"}
        assert finding["severity"] in ("error", "warning")
        assert finding["message"]
