"""Direct tests for the simulation lifecycle tools (connect/get_model/run)."""

import json

import pytest

from simtrace import model as model_module
from simtrace.model import FactoryModel
from simtrace.tools.builders import create_buffer
from simtrace.tools.builders import create_sink, create_source
from simtrace.tools.simulation import connect, get_model, reset_model, run_simulation


@pytest.fixture
def model() -> FactoryModel:
    return FactoryModel()


@pytest.fixture
def wired(model) -> FactoryModel:
    """A minimal runnable line: source -> buffer -> sink."""
    create_source("src", inter_arrival_time=1, blocking=True, model=model)
    create_sink("snk", model=model)
    create_buffer("buf", capacity=4, model=model)
    return model


# --- connect --------------------------------------------------------------


def test_connect_wires_edge_between_nodes(wired):
    summary = connect("buf", "src", "snk", model=wired)

    assert summary == {
        "edge": "buf",
        "type": "Buffer",
        "src": "src",
        "dest": "snk",
    }

    edge = wired.edges["buf"]
    assert edge.src_node is wired.nodes["src"]
    assert edge.dest_node is wired.nodes["snk"]
    # The nodes now reference the edge.
    assert edge in wired.nodes["src"].out_edges
    assert edge in wired.nodes["snk"].in_edges


def test_connect_unknown_edge_rejected(wired):
    with pytest.raises(ValueError, match="No edge with id 'nope'"):
        connect("nope", "src", "snk", model=wired)


def test_connect_unknown_src_rejected(wired):
    with pytest.raises(ValueError, match="No node with id 'ghost'"):
        connect("buf", "ghost", "snk", model=wired)


def test_connect_unknown_dest_rejected(wired):
    with pytest.raises(ValueError, match="No node with id 'ghost'"):
        connect("buf", "src", "ghost", model=wired)


def test_connect_already_connected_rejected(wired):
    connect("buf", "src", "snk", model=wired)
    with pytest.raises(ValueError, match="already connected"):
        connect("buf", "src", "snk", model=wired)


def test_connect_summary_is_json_serializable(wired):
    summary = connect("buf", "src", "snk", model=wired)
    assert json.loads(json.dumps(summary)) == summary


# --- get_model ------------------------------------------------------------


def test_get_model_empty():
    assert get_model(model=FactoryModel()) == {"nodes": [], "edges": []}


def test_get_model_reports_counts_before_wiring(wired):
    snapshot = get_model(model=wired)

    nodes = {n["id"]: n for n in snapshot["nodes"]}
    assert nodes["src"]["type"] == "Source"
    assert nodes["src"]["in_edges"] == 0 and nodes["src"]["out_edges"] == 0

    edges = {e["id"]: e for e in snapshot["edges"]}
    assert edges["buf"]["type"] == "Buffer"
    assert edges["buf"]["src"] is None and edges["buf"]["dest"] is None


def test_get_model_reflects_wiring(wired):
    connect("buf", "src", "snk", model=wired)
    snapshot = get_model(model=wired)

    nodes = {n["id"]: n for n in snapshot["nodes"]}
    assert nodes["src"]["out_edges"] == 1
    assert nodes["snk"]["in_edges"] == 1

    edges = {e["id"]: e for e in snapshot["edges"]}
    assert edges["buf"]["src"] == "src" and edges["buf"]["dest"] == "snk"


def test_get_model_is_json_serializable(wired):
    connect("buf", "src", "snk", model=wired)
    snapshot = get_model(model=wired)
    assert json.loads(json.dumps(snapshot)) == snapshot


# --- run_simulation -------------------------------------------------------


def test_run_simulation_executes_line(wired):
    connect("buf", "src", "snk", model=wired)
    result = run_simulation(10, model=wired)

    assert result["until"] == 10
    assert result["now"] == 10
    # Items actually flowed from source through the buffer to the sink.
    assert result["nodes"]["snk"]["num_item_received"] > 0


def test_run_simulation_result_is_json_serializable(wired):
    connect("buf", "src", "snk", model=wired)
    result = run_simulation(10, model=wired)
    assert json.loads(json.dumps(result)) == result


def test_run_simulation_zero_until_rejected(wired):
    with pytest.raises(ValueError, match="until must be > 0"):
        run_simulation(0, model=wired)


def test_run_simulation_bool_until_rejected(wired):
    with pytest.raises(ValueError, match="constant int or float"):
        run_simulation(True, model=wired)


def test_run_simulation_unconnected_source_raises(model):
    # A Source with no out_edge fails FactorySimPy's run-time assertion.
    create_source("src", inter_arrival_time=1, blocking=True, model=model)
    with pytest.raises(AssertionError):
        run_simulation(5, model=model)


# --- run_simulation seeding ------------------------------------------------


def _stochastic_line() -> FactoryModel:
    """A freshly built line whose arrivals are random draws."""
    m = FactoryModel()
    create_source("src", inter_arrival_time="exp(1)", blocking=True, model=m)
    create_sink("snk", model=m)
    create_buffer("buf", capacity=4, model=m)
    connect("buf", "src", "snk", model=m)
    return m


def test_run_simulation_same_seed_reproduces_run():
    first = run_simulation(50, seed=42, model=_stochastic_line())
    second = run_simulation(50, seed=42, model=_stochastic_line())
    assert first == second
    assert first["nodes"]["snk"]["num_item_received"] > 0


def test_run_simulation_different_seeds_diverge():
    first = run_simulation(50, seed=1, model=_stochastic_line())
    second = run_simulation(50, seed=2, model=_stochastic_line())
    # Arrival draws differ, so the sinks' receive counts/stats differ.
    assert first["nodes"] != second["nodes"]


def test_run_simulation_seed_echoed_in_result(wired):
    connect("buf", "src", "snk", model=wired)
    result = run_simulation(10, seed=7, model=wired)
    assert result["seed"] == 7


def test_run_simulation_seed_defaults_to_none(wired):
    connect("buf", "src", "snk", model=wired)
    result = run_simulation(10, model=wired)
    assert result["seed"] is None


def test_run_simulation_bool_seed_rejected(wired):
    with pytest.raises(ValueError, match="seed must be an int"):
        run_simulation(10, seed=True, model=wired)


def test_run_simulation_float_seed_rejected(wired):
    with pytest.raises(ValueError, match="seed must be an int"):
        run_simulation(10, seed=1.5, model=wired)


# --- reset_model ----------------------------------------------------------


@pytest.fixture
def fresh_session():
    """Give the test a clean module-level session model, then restore it."""
    saved = model_module._model
    model_module._model = FactoryModel()
    try:
        yield model_module._model
    finally:
        model_module._model = saved


def test_reset_model_clears_nodes_and_edges(fresh_session):
    session = fresh_session
    create_source("src", inter_arrival_time=1, blocking=True, model=session)
    create_sink("snk", model=session)
    create_buffer("buf", capacity=4, model=session)
    connect("buf", "src", "snk", model=session)

    summary = reset_model()

    assert summary == {"cleared_nodes": 2, "cleared_edges": 1, "now": 0}
    # The session model is now a fresh, empty one.
    assert get_model() == {"nodes": [], "edges": []}


def test_reset_model_restarts_the_clock(fresh_session):
    session = fresh_session
    create_source("src", inter_arrival_time=1, blocking=True, model=session)
    create_sink("snk", model=session)
    create_buffer("buf", capacity=4, model=session)
    connect("buf", "src", "snk", model=session)
    run_simulation(50, model=session)
    assert session.env.now == 50

    reset_model()

    # A new env means the clock is back at 0, so a fresh run to 480 is valid.
    assert model_module.get_model().env.now == 0


def test_reset_model_on_empty_session(fresh_session):
    assert reset_model() == {"cleared_nodes": 0, "cleared_edges": 0, "now": 0}


def test_reset_model_summary_is_json_serializable(fresh_session):
    create_source("src", inter_arrival_time=1, blocking=True, model=fresh_session)
    summary = reset_model()
    assert json.loads(json.dumps(summary)) == summary
