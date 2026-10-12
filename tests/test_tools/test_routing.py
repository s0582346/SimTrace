"""Tests for conditional routing: create_machine(route_by, routes)."""

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


def _sorting_line(model: FactoryModel, routes: dict, sources=None) -> FactoryModel:
    """Typed sources merge into one sorting machine with one sink per edge.

    `sources` maps source id -> attributes (all with part_type = source id).
    Every edge id named in `routes` gets its own sink "snk_<edge id>".
    """
    sources = sources or {
        "std": {"family": "A"},
        "prem": {"family": "B"},
    }
    for src_id, attributes in sources.items():
        create_source(src_id, inter_arrival_time=2, blocking=True,
                      part_type=src_id, attributes=attributes, model=model)
        create_buffer(f"b_{src_id}", capacity=10, model=model)
    create_machine("sort", processing_delay=0.1, route_by="family",
                   routes=routes, model=model)
    for src_id in sources:
        connect(f"b_{src_id}", src_id, "sort", model=model)
    for edge_id in dict.fromkeys(routes.values()):
        create_buffer(edge_id, capacity=10, model=model)
        create_sink(f"snk_{edge_id}", model=model)
        connect(edge_id, "sort", f"snk_{edge_id}", model=model)
    return model


def _ends(model: FactoryModel) -> dict[str, str]:
    """Item id -> last node of its trail."""
    return {item_id: trail[-1] for item_id, trail in model.item_paths.items()}


# --- create_machine(route_by, routes) -------------------------------------------


def test_machine_echoes_and_records_routes(model):
    routes = {"A": "b_a", "B": "b_b"}
    summary = create_machine("m", route_by="family", routes=routes, model=model)

    assert summary["route_by"] == "family"
    assert summary["routes"] == routes
    assert model.nodes["m"].routes == routes
    assert callable(model.nodes["m"].out_edge_selection)
    assert model.spec[-1]["kwargs"]["route_by"] == "family"
    assert model.spec[-1]["kwargs"]["routes"] == routes


def test_machine_without_routes_is_unchanged(model):
    summary = create_machine("m", model=model)

    assert summary["route_by"] is None and summary["routes"] is None
    assert model.nodes["m"].out_edge_selection == "FIRST_AVAILABLE"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"route_by": "family"},
        {"routes": {"A": "b_a"}},
        {"route_by": "", "routes": {"A": "b_a"}},
        {"route_by": "family", "routes": {}},
        {"route_by": "family", "routes": ["b_a"]},
        {"route_by": "family", "routes": {"": "b_a"}},
        {"route_by": "family", "routes": {"A": ""}},
        {"route_by": "family", "routes": {"A": 3}},
        {"route_by": "family", "routes": {"A": "b_a"},
         "out_edge_selection": "ROUND_ROBIN"},
    ],
)
def test_machine_rejects_bad_routes(model, kwargs):
    with pytest.raises(ValueError):
        create_machine("m", model=model, **kwargs)
    assert "m" not in model.nodes


# --- routing during a run -------------------------------------------------------


def test_each_family_goes_to_its_own_edge(model):
    _sorting_line(model, {"A": "b_a", "B": "b_b"})
    result = run_simulation(30, seed=1, model=model)

    ends = _ends(model)
    for item_id, payload in model.item_attributes.items():
        if item_id in ends:
            expected = "snk_b_a" if payload["family"] == "A" else "snk_b_b"
            assert ends[item_id] == expected
    received = result["nodes"]
    assert received["snk_b_a"]["num_item_received"] > 0
    assert received["snk_b_b"]["num_item_received"] > 0


def test_route_by_part_type(model):
    for src_id in ("std", "prem"):
        create_source(src_id, inter_arrival_time=2, blocking=True,
                      part_type=src_id, model=model)
        create_buffer(f"b_{src_id}", capacity=10, model=model)
    create_machine("sort", route_by="part_type",
                   routes={"std": "b_std_out", "prem": "b_prem_out"}, model=model)
    for src_id in ("std", "prem"):
        connect(f"b_{src_id}", src_id, "sort", model=model)
        create_buffer(f"b_{src_id}_out", capacity=10, model=model)
        create_sink(f"snk_{src_id}", model=model)
        connect(f"b_{src_id}_out", "sort", f"snk_{src_id}", model=model)

    run_simulation(30, seed=1, model=model)

    for item_id, end in _ends(model).items():
        assert end == f"snk_{model.item_attributes[item_id]['part_type']}"


def test_fallback_catches_other_values(model):
    _sorting_line(
        model,
        {"A": "b_a", "*": "b_rest"},
        sources={"std": {"family": "A"}, "prem": {"family": "B"},
                 "hot": {"family": "C"}},
    )
    run_simulation(30, seed=1, model=model)

    ends = _ends(model)
    for item_id, payload in model.item_attributes.items():
        if item_id in ends:
            expected = "snk_b_a" if payload["family"] == "A" else "snk_b_rest"
            assert ends[item_id] == expected


def test_non_string_value_uses_fallback(model):
    _sorting_line(
        model,
        {"True": "b_a", "*": "b_rest"},
        sources={"std": {"family": True}},
    )
    run_simulation(10, seed=1, model=model)

    assert set(_ends(model).values()) == {"snk_b_rest"}


def test_unmatched_value_without_fallback_stops_the_run(model):
    _sorting_line(model, {"A": "b_a"})

    with pytest.raises(ValueError, match="no route matches"):
        run_simulation(30, seed=1, model=model)


def test_untyped_item_without_fallback_stops_the_run(model):
    create_source("src", inter_arrival_time=2, blocking=True, model=model)
    create_buffer("b_in", capacity=10, model=model)
    create_machine("sort", route_by="family", routes={"A": "b_a"}, model=model)
    create_buffer("b_a", capacity=10, model=model)
    create_sink("snk", model=model)
    connect("b_in", "src", "sort", model=model)
    connect("b_a", "sort", "snk", model=model)

    with pytest.raises(ValueError, match="family=None"):
        run_simulation(10, seed=1, model=model)


def test_route_to_an_edge_that_is_not_an_out_edge_stops_the_run(model):
    _sorting_line(model, {"A": "b_a", "B": "b_b"})
    # The selector reads the same dict, so this retargets B to a stray edge.
    model.nodes["sort"].routes["B"] = "b_elsewhere"

    with pytest.raises(ValueError, match="not one of its out-edges"):
        run_simulation(30, seed=1, model=model)


def test_rebuild_keeps_routes(model):
    _sorting_line(model, {"A": "b_a", "B": "b_b"})
    first = run_simulation(30, seed=2, model=model)

    replica = build_from_spec(model.spec)
    second = run_simulation(30, seed=2, model=replica)

    assert second["nodes"] == first["nodes"]
    assert _ends(replica) == _ends(model)


def test_parallel_workers_route_their_own_item(model):
    for src_id, family in (("std", "A"), ("prem", "B")):
        create_source(src_id, inter_arrival_time="exp(1)", blocking=True,
                      part_type=src_id, attributes={"family": family},
                      model=model)
        create_buffer(f"b_{src_id}", capacity=20, model=model)
    create_machine("sort", work_capacity=3, processing_delay="uniform(0.5, 3)",
                   route_by="family", routes={"A": "b_a", "B": "b_b"},
                   model=model)
    for src_id in ("std", "prem"):
        connect(f"b_{src_id}", src_id, "sort", model=model)
    for edge_id in ("b_a", "b_b"):
        create_buffer(edge_id, capacity=20, model=model)
        create_sink(f"snk_{edge_id}", model=model)
        connect(edge_id, "sort", f"snk_{edge_id}", model=model)

    run_simulation(100, seed=4, model=model)

    ends = _ends(model)
    checked = 0
    for item_id, payload in model.item_attributes.items():
        if ends.get(item_id, "").startswith("snk_"):
            expected = "snk_b_a" if payload["family"] == "A" else "snk_b_b"
            assert ends[item_id] == expected
            checked += 1
    assert checked > 50
