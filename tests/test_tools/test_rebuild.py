"""Tests for the build spec and build_from_spec.

The spec is what makes replications possible from an assembled session model:
every create_*/connect call records its raw arguments, and build_from_spec
replays them into a fresh model with a clock at 0.
"""

import pytest

from simtrace.model import FactoryModel
from simtrace.tools.builders import (
    create_buffer,
    create_conveyor,
    create_machine,
    create_sink,
    create_source,
)
from simtrace.tools.simulation import build_from_spec, connect, run_simulation


def _build_line(model: FactoryModel) -> FactoryModel:
    """src -> in_buf -> mach -> out_buf -> snk, with stochastic timings."""
    create_source("src", inter_arrival_time="exp(2)", blocking=True, model=model)
    create_machine("mach", processing_delay="uniform(1, 3)", model=model)
    create_sink("snk", model=model)
    create_buffer("in_buf", capacity=5, model=model)
    create_buffer("out_buf", capacity=5, model=model)
    connect("in_buf", "src", "mach", model=model)
    connect("out_buf", "mach", "snk", model=model)
    return model


# --- recording --------------------------------------------------------------


def test_spec_starts_empty():
    assert FactoryModel().spec == []


def test_spec_records_every_step_in_order():
    m = _build_line(FactoryModel())
    assert [s["op"] for s in m.spec] == [
        "create_source",
        "create_machine",
        "create_sink",
        "create_buffer",
        "create_buffer",
        "connect",
        "connect",
    ]


def test_spec_keeps_distribution_strings_not_samplers():
    # The node gets a sampler callable; the spec must keep the original string,
    # or the model can't be rebuilt (and the spec isn't JSON-serializable).
    m = FactoryModel()
    create_source("src", inter_arrival_time="exp(2)", model=m)
    assert m.spec[0]["kwargs"]["inter_arrival_time"] == "exp(2)"


def test_spec_records_defaults_not_just_passed_args():
    m = FactoryModel()
    create_machine("mach", model=m)
    kwargs = m.spec[0]["kwargs"]
    assert kwargs["work_capacity"] == 1
    assert kwargs["blocking"] is True
    assert kwargs["in_edge_selection"] == "FIRST_AVAILABLE"


def test_spec_omits_the_model_kwarg():
    # The replayer supplies its own model; a recorded one would be replayed into
    # the wrong graph.
    m = FactoryModel()
    create_sink("snk", model=m)
    assert "model" not in m.spec[0]["kwargs"]


def test_rejected_call_records_nothing():
    m = FactoryModel()
    create_sink("snk", model=m)
    with pytest.raises(ValueError):
        create_sink("snk", model=m)  # duplicate id
    assert len(m.spec) == 1


def test_combiner_spec_does_not_alias_caller_list():
    m = FactoryModel()
    quantities = [1, 2]
    from simtrace.tools.builders import create_combiner

    create_combiner("comb", target_quantity_of_each_item=quantities, model=m)
    quantities.append(99)
    assert m.spec[0]["kwargs"]["target_quantity_of_each_item"] == [1, 2]


# --- replaying --------------------------------------------------------------


def test_build_from_spec_reproduces_the_graph():
    original = _build_line(FactoryModel())
    rebuilt = build_from_spec(original.spec)
    assert set(rebuilt.nodes) == set(original.nodes)
    assert set(rebuilt.edges) == set(original.edges)


def test_rebuilt_model_is_wired():
    rebuilt = build_from_spec(_build_line(FactoryModel()).spec)
    assert rebuilt.edges["in_buf"].src_node.id == "src"
    assert rebuilt.edges["in_buf"].dest_node.id == "mach"
    assert rebuilt.edges["out_buf"].dest_node.id == "snk"


def test_rebuilt_model_is_independent_and_unrun():
    original = _build_line(FactoryModel())
    run_simulation(20, seed=1, model=original)

    rebuilt = build_from_spec(original.spec)
    # Fresh env: clock at 0 even though the source model has run.
    assert rebuilt.env.now == 0
    assert rebuilt.env is not original.env
    assert rebuilt.nodes["src"] is not original.nodes["src"]


def test_rebuilding_does_not_disturb_the_source_model():
    original = _build_line(FactoryModel())
    run_simulation(20, seed=1, model=original)
    before = original.env.now
    spec_len = len(original.spec)

    build_from_spec(original.spec)

    assert original.env.now == before
    assert len(original.spec) == spec_len  # replay recorded onto the new model


def test_rebuilt_model_runs_and_produces_output():
    rebuilt = build_from_spec(_build_line(FactoryModel()).spec)
    result = run_simulation(50, seed=3, model=rebuilt)
    assert result["now"] == 50
    assert result["nodes"]["snk"]["num_item_received"] > 0


def test_rebuilt_model_matches_the_original_run_under_one_seed():
    # Same graph + same seed => same run, which is what makes a replication
    # batch reproducible from a single seed base.
    a = build_from_spec(_build_line(FactoryModel()).spec)
    b = build_from_spec(_build_line(FactoryModel()).spec)
    ra = run_simulation(50, seed=11, model=a)
    rb = run_simulation(50, seed=11, model=b)
    assert ra["nodes"]["snk"]["num_item_received"] == rb["nodes"]["snk"]["num_item_received"]


def test_connect_order_is_preserved():
    # in_edge_selection="FIRST_AVAILABLE" drains in-edges in connect order, so
    # replay must keep it: the priority stream stays first.
    m = FactoryModel()
    create_source("hi", inter_arrival_time=2, model=m)
    create_source("lo", inter_arrival_time=2, model=m)
    create_machine("mach", model=m)
    create_sink("snk", model=m)
    create_buffer("hi_buf", capacity=5, model=m)
    create_buffer("lo_buf", capacity=5, model=m)
    create_buffer("out", capacity=5, model=m)
    connect("hi_buf", "hi", "mach", model=m)
    connect("lo_buf", "lo", "mach", model=m)
    connect("out", "mach", "snk", model=m)

    rebuilt = build_from_spec(m.spec)
    assert [e.id for e in rebuilt.nodes["mach"].in_edges] == ["hi_buf", "lo_buf"]


def test_conveyor_rebuilds():
    m = FactoryModel()
    create_source("src", inter_arrival_time=1, item_length=1, model=m)
    create_sink("snk", model=m)
    create_conveyor("belt", conveyor_length=10, speed=1, item_length=1, model=m)
    connect("belt", "src", "snk", model=m)

    rebuilt = build_from_spec(m.spec)
    assert rebuilt.edges["belt"].dest_node.id == "snk"


def test_empty_spec_rejected():
    with pytest.raises(ValueError, match="Cannot rebuild an empty model"):
        build_from_spec([])


def test_unknown_op_rejected():
    with pytest.raises(ValueError, match="Unknown build step"):
        build_from_spec([{"op": "create_teleporter", "kwargs": {"id": "x"}}])
