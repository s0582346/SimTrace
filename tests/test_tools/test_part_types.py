"""Tests for part types: create_source(part_type, attributes), stamping, report."""

import pytest

from simtrace.model import FactoryModel
from simtrace.tools.builders import (
    create_buffer,
    create_machine,
    create_sink,
    create_source,
)
from simtrace.tools.simulation import build_from_spec, connect, run_simulation


@pytest.fixture
def model() -> FactoryModel:
    return FactoryModel()


def _two_type_line(model: FactoryModel) -> FactoryModel:
    """Two typed sources merge into one machine and one sink."""
    create_source("src_std", inter_arrival_time=2, blocking=True,
                  part_type="standard",
                  attributes={"family": "A", "price": 50, "urgent": False},
                  model=model)
    create_source("src_prem", inter_arrival_time=5, blocking=True,
                  part_type="premium",
                  attributes={"family": "B", "price": 160},
                  model=model)
    create_machine("mach", processing_delay=0.5, model=model)
    create_sink("snk", model=model)
    for edge_id in ("b_std", "b_prem", "b_out"):
        create_buffer(edge_id, capacity=10, model=model)
    connect("b_std", "src_std", "mach", model=model)
    connect("b_prem", "src_prem", "mach", model=model)
    connect("b_out", "mach", "snk", model=model)
    return model


# --- create_source(part_type, attributes) ---------------------------------------


def test_source_echoes_and_records_part_type(model):
    summary = create_source("src", part_type="p",
                            attributes={"family": "A", "value": 3}, model=model)

    assert summary["part_type"] == "p"
    assert summary["attributes"] == {"family": "A", "value": 3}
    assert model.nodes["src"].part_type == "p"
    assert model.spec[-1]["kwargs"]["part_type"] == "p"
    assert model.spec[-1]["kwargs"]["attributes"] == {"family": "A", "value": 3}


def test_attributes_are_stored_unchanged(model):
    attributes = {"family": "B", "price": 160, "fragile": True}
    create_source("src", part_type="p", attributes=attributes, model=model)

    assert model.nodes["src"].part_attributes == attributes


def test_untyped_source_has_no_part_type(model):
    summary = create_source("src", model=model)

    assert summary["part_type"] is None
    assert model.nodes["src"].part_type is None


@pytest.mark.parametrize(
    "part_type, attributes",
    [
        (None, {"family": "A"}),
        ("", None),
        ("p", {"part_type": "x"}),
        ("p", {"": 1}),
        ("p", {"price": [1, 2]}),
        ("p", {"price": "uniform(120, 200)"}),
        ("p", {"price": "exp(5)"}),
        ("p", ["family"]),
    ],
)
def test_source_rejects_bad_part_type_input(model, part_type, attributes):
    with pytest.raises(ValueError):
        create_source("src", part_type=part_type, attributes=attributes,
                      model=model)
    assert "src" not in model.nodes


# --- stamping and report ------------------------------------------------------


def test_items_carry_their_type_and_attributes(model):
    _two_type_line(model)
    run_simulation(40, seed=1, model=model)

    payloads = model.item_attributes
    assert payloads
    for item_id, payload in payloads.items():
        if item_id.startswith("item_src_std_"):
            assert payload == {"part_type": "standard", "family": "A",
                               "price": 50, "urgent": False}
        else:
            assert item_id.startswith("item_src_prem_")
            assert payload == {"part_type": "premium", "family": "B",
                               "price": 160}


def test_items_get_their_own_payload(model):
    _two_type_line(model)
    run_simulation(40, seed=1, model=model)

    payloads = list(model.item_attributes.values())
    assert len({id(p) for p in payloads}) == len(payloads)


def test_report_counts_per_type(model):
    _two_type_line(model)
    result = run_simulation(41, seed=1, model=model)

    report = result["part_types"]
    nodes = result["nodes"]
    completed = report["standard"]["completed"] + report["premium"]["completed"]
    assert completed == nodes["snk"]["num_item_received"]
    assert report["standard"]["generated"] == nodes["src_std"]["num_item_generated"]
    assert report["premium"]["generated"] == nodes["src_prem"]["num_item_generated"]
    assert report["standard"]["completed_attribute_sums"] == {
        "price": 50 * report["standard"]["completed"]
    }
    assert report["premium"]["completed_attribute_sums"] == {
        "price": 160 * report["premium"]["completed"]
    }
    assert "urgent" not in report["standard"]["completed_attribute_sums"]
    assert "family" not in report["premium"]["completed_attribute_sums"]


def test_sources_sharing_a_type_are_counted_together(model):
    for src_id in ("src_a", "src_b"):
        create_source(src_id, inter_arrival_time=2, blocking=True,
                      part_type="p", attributes={"price": 1}, model=model)
    create_sink("snk", model=model)
    for src_id in ("src_a", "src_b"):
        create_buffer(f"b_{src_id}", capacity=10, model=model)
        connect(f"b_{src_id}", src_id, "snk", model=model)

    result = run_simulation(11, model=model)

    nodes = result["nodes"]
    generated = (nodes["src_a"]["num_item_generated"]
                 + nodes["src_b"]["num_item_generated"])
    assert list(result["part_types"]) == ["p"]
    assert result["part_types"]["p"]["generated"] == generated


def test_untyped_model_has_no_report(model):
    create_source("src", inter_arrival_time=1, blocking=True, model=model)
    create_sink("snk", model=model)
    create_buffer("b", capacity=5, model=model)
    connect("b", "src", "snk", model=model)

    result = run_simulation(5, model=model)

    assert "part_types" not in result
    assert model.item_attributes == {}


def test_rebuild_keeps_part_types(model):
    _two_type_line(model)
    first = run_simulation(40, seed=3, model=model)

    replica = build_from_spec(model.spec)
    second = run_simulation(40, seed=3, model=replica)

    assert second["part_types"] == first["part_types"]
