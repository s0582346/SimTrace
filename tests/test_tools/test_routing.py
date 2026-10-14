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


# --- list routes: type picks the edges, the first free one takes the item -------


def _alu_steel_line(
    model: FactoryModel,
    alu_every: float,
    m1_delay: float,
    m2_delay: float,
    routes: dict | None = None,
) -> FactoryModel:
    """Alu and steel sources into one sorter; alu may use m1 or m2, steel m3.

    Each station m1, m2, m3 has its own buffer of capacity 1 and its own sink
    "snk_<station>".
    """
    for src_id, every in (("alu", alu_every), ("steel", 20)):
        create_source(src_id, inter_arrival_time=every, blocking=True,
                      part_type=src_id, model=model)
        create_buffer(f"b_{src_id}", capacity=50, model=model)
    create_machine(
        "sort", processing_delay=0.1, route_by="part_type",
        routes=routes or {"alu": ["b_m1", "b_m2"], "steel": "b_m3"},
        model=model,
    )
    for src_id in ("alu", "steel"):
        connect(f"b_{src_id}", src_id, "sort", model=model)
    for station, delay in (("m1", m1_delay), ("m2", m2_delay), ("m3", 1)):
        create_buffer(f"b_{station}", capacity=1, model=model)
        create_machine(station, processing_delay=delay, model=model)
        create_buffer(f"b_{station}_out", capacity=1, model=model)
        create_sink(f"snk_{station}", model=model)
        connect(f"b_{station}", "sort", station, model=model)
        connect(f"b_{station}_out", station, f"snk_{station}", model=model)
    return model


def _delivered(model: FactoryModel) -> dict[str, list[str]]:
    """Part type -> the sinks its delivered items ended at."""
    by_type: dict[str, list[str]] = {}
    for item_id, end in _ends(model).items():
        if end.startswith("snk_"):
            part_type = model.item_attributes[item_id]["part_type"]
            by_type.setdefault(part_type, []).append(end)
    return by_type


def test_list_route_is_echoed_recorded_and_copied(model):
    routes = {"alu": ["b_m1", "b_m2"], "*": "b_m3"}
    summary = create_machine("m", route_by="part_type", routes=routes, model=model)
    routes["alu"].append("b_extra")

    assert model.nodes["m"].routes == {"alu": ["b_m1", "b_m2"], "*": "b_m3"}
    assert summary["routes"]["alu"][:2] == ["b_m1", "b_m2"]
    assert model.spec[-1]["kwargs"]["route_by"] == "part_type"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"routes": {"alu": []}},
        {"routes": {"alu": ["b_m1", "b_m1"]}},
        {"routes": {"alu": ["b_m1", ""]}},
        {"routes": {"alu": ["b_m1", 3]}},
        {"routes": {"alu": ["b_m1", "b_m2"]}, "blocking": False},
    ],
)
def test_machine_rejects_bad_list_routes(model, kwargs):
    with pytest.raises(ValueError):
        create_machine("m", route_by="part_type", model=model, **kwargs)
    assert "m" not in model.nodes


def test_single_edge_route_still_picks_one_index(model):
    _alu_steel_line(model, alu_every=5, m1_delay=1, m2_delay=1)
    select = model.nodes["sort"].out_edge_selection

    class Item:
        id = "x"

    alu, steel = Item(), Item()
    alu.payload = {"part_type": "alu"}
    steel.payload = {"part_type": "steel"}

    assert select(steel) == 2
    assert select(alu) == [0, 1]


def test_first_listed_edge_wins_while_it_is_free(model):
    # Alu arrives every 10, m1 needs 1: m1 is always free again in time.
    _alu_steel_line(model, alu_every=10, m1_delay=1, m2_delay=1)
    run_simulation(200, seed=1, model=model)

    delivered = _delivered(model)
    assert len(delivered["alu"]) > 10
    assert set(delivered["alu"]) == {"snk_m1"}


def test_full_first_edge_sends_alu_to_the_second(model):
    # Alu arrives every 1, m1 needs 10: b_m1 is full most of the time.
    _alu_steel_line(model, alu_every=1, m1_delay=10, m2_delay=1)
    run_simulation(200, seed=1, model=model)

    delivered = _delivered(model)
    assert delivered["alu"].count("snk_m1") > 5
    assert delivered["alu"].count("snk_m2") > 5


def test_when_both_are_full_the_one_that_frees_first_takes_it(model):
    # Both alu stations are overloaded. m2 is five times faster, so it frees
    # up about five times as often and gets about five times as many items.
    # Waiting at m1 only would send most items to m1 instead.
    _alu_steel_line(model, alu_every=0.5, m1_delay=10, m2_delay=2)
    run_simulation(400, seed=1, model=model)

    delivered = _delivered(model)
    to_m1 = delivered["alu"].count("snk_m1")
    to_m2 = delivered["alu"].count("snk_m2")
    assert to_m1 > 10
    assert to_m2 >= 3 * to_m1


def test_type_never_enters_an_edge_outside_its_list(model):
    # m1 stays full and m3 is mostly empty; alu still never uses m3, and
    # steel never uses m1 or m2.
    _alu_steel_line(model, alu_every=1, m1_delay=10, m2_delay=1)
    run_simulation(400, seed=1, model=model)

    delivered = _delivered(model)
    assert delivered["alu"].count("snk_m1") > 10
    assert "snk_m3" not in delivered["alu"]
    assert len(delivered["steel"]) > 10
    assert set(delivered["steel"]) == {"snk_m3"}


def test_list_route_under_fallback(model):
    _alu_steel_line(model, alu_every=1, m1_delay=10, m2_delay=1,
                    routes={"steel": "b_m3", "*": ["b_m1", "b_m2"]})
    run_simulation(200, seed=1, model=model)

    delivered = _delivered(model)
    assert set(delivered["alu"]) == {"snk_m1", "snk_m2"}
    assert set(delivered["steel"]) == {"snk_m3"}


def test_rebuild_keeps_list_routes(model):
    _alu_steel_line(model, alu_every=1, m1_delay=10, m2_delay=1)
    first = run_simulation(200, seed=3, model=model)

    replica = build_from_spec(model.spec)
    second = run_simulation(200, seed=3, model=replica)

    assert second["nodes"] == first["nodes"]
    assert _ends(replica) == _ends(model)
