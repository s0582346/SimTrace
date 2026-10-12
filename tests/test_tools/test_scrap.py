"""Tests for random routing (create_machine(route_shares)) and scrap Sinks."""

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


def _inspection_line(
    model: FactoryModel, shares: dict, price: int = 10, iat: float = 1
) -> FactoryModel:
    """src -> b_in -> inspect -> b_ship -> ship  and  -> b_scrap -> scrap."""
    create_source("src", inter_arrival_time=iat, blocking=True,
                  part_type="part", attributes={"price": price}, model=model)
    create_buffer("b_in", capacity=10, model=model)
    create_machine("inspect", processing_delay=0, route_shares=shares,
                   model=model)
    create_sink("ship", model=model)
    create_sink("scrap", scrap=True, model=model)
    create_buffer("b_ship", capacity=10, model=model)
    create_buffer("b_scrap", capacity=10, model=model)
    connect("b_in", "src", "inspect", model=model)
    connect("b_ship", "inspect", "ship", model=model)
    connect("b_scrap", "inspect", "scrap", model=model)
    return model


def _ends(model: FactoryModel) -> dict[str, str]:
    """Item id -> last node of its trail."""
    return {item_id: trail[-1] for item_id, trail in model.item_paths.items()}


# --- create_machine(route_shares) ----------------------------------------------


def test_machine_echoes_and_records_shares(model):
    shares = {"b_ship": 0.9, "b_scrap": 0.1}
    summary = create_machine("m", route_shares=shares, model=model)

    assert summary["route_shares"] == shares
    assert model.nodes["m"].route_shares == shares
    assert callable(model.nodes["m"].out_edge_selection)
    assert model.spec[-1]["kwargs"]["route_shares"] == shares


def test_machine_without_shares_is_unchanged(model):
    summary = create_machine("m", model=model)

    assert summary["route_shares"] is None
    assert model.nodes["m"].out_edge_selection == "FIRST_AVAILABLE"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"route_shares": {}},
        {"route_shares": ["b_a"]},
        {"route_shares": {"": 1.0}},
        {"route_shares": {"b_a": True}},
        {"route_shares": {"b_a": "0.5", "b_b": 0.5}},
        {"route_shares": {"b_a": -0.1, "b_b": 1.1}},
        {"route_shares": {"b_a": 0.5, "b_b": 0.4}},
        {"route_shares": {"b_a": 0.5, "b_b": 0.5},
         "route_by": "family", "routes": {"A": "b_a"}},
        {"route_shares": {"b_a": 0.5, "b_b": 0.5},
         "out_edge_selection": "RANDOM"},
    ],
)
def test_machine_rejects_bad_shares(model, kwargs):
    with pytest.raises(ValueError):
        create_machine("m", model=model, **kwargs)
    assert "m" not in model.nodes


def test_shares_that_add_up_after_rounding_are_accepted(model):
    create_machine("m", route_shares={"a": 0.1, "b": 0.2, "c": 0.7},
                   model=model)

    assert model.nodes["m"].route_shares == {"a": 0.1, "b": 0.2, "c": 0.7}


# --- create_sink(scrap) ------------------------------------------------------


def test_sink_scrap_flag_is_echoed_and_recorded(model):
    summary = create_sink("scrap", scrap=True, model=model)

    assert summary["scrap"] is True
    assert model.nodes["scrap"].scrap is True
    assert model.spec[-1]["kwargs"]["scrap"] is True


def test_sink_is_not_scrap_by_default(model):
    assert create_sink("ship", model=model)["scrap"] is False


def test_sink_rejects_non_bool_scrap(model):
    with pytest.raises(ValueError):
        create_sink("scrap", scrap="yes", model=model)


# --- routing during a run ----------------------------------------------------


def test_items_split_roughly_by_share(model):
    _inspection_line(model, {"b_ship": 0.8, "b_scrap": 0.2})
    result = run_simulation(4000, seed=3, model=model)

    shipped = result["nodes"]["ship"]["num_item_received"]
    scrapped = result["nodes"]["scrap"]["num_item_received"]
    assert shipped + scrapped > 3900
    assert scrapped / (shipped + scrapped) == pytest.approx(0.2, abs=0.03)


def test_full_share_sends_everything_one_way(model):
    _inspection_line(model, {"b_ship": 1.0, "b_scrap": 0.0})
    result = run_simulation(200, seed=1, model=model)

    assert result["nodes"]["scrap"]["num_item_received"] == 0
    assert result["nodes"]["ship"]["num_item_received"] > 0


def test_same_seed_gives_same_split(model):
    _inspection_line(model, {"b_ship": 0.7, "b_scrap": 0.3})
    run_simulation(300, seed=5, model=model)

    replica = build_from_spec(model.spec)
    run_simulation(300, seed=5, model=replica)

    assert _ends(replica) == _ends(model)


def test_rework_loop_sends_some_items_back(model):
    # src -> b_in -> mill -> b_check -> check -> b_ship -> ship
    #                  ^                    |
    #                  +---- b_rework ------+
    create_source("src", inter_arrival_time=5, blocking=True, model=model)
    create_machine("mill", processing_delay=1, model=model)
    create_machine("check", processing_delay=0.5,
                   route_shares={"b_ship": 0.7, "b_rework": 0.3}, model=model)
    create_sink("ship", model=model)
    for edge_id in ("b_in", "b_check", "b_ship", "b_rework"):
        create_buffer(edge_id, capacity=10, model=model)
    connect("b_in", "src", "mill", model=model)
    connect("b_rework", "check", "mill", model=model)
    connect("b_check", "mill", "check", model=model)
    connect("b_ship", "check", "ship", model=model)

    result = run_simulation(1000, seed=2, model=model)

    trails = model.item_paths.values()
    reworked = [t for t in trails if t.count("mill") > 1]
    assert reworked
    assert all(t[-1] == "ship" for t in reworked if t[-1] in model.nodes)
    assert result["nodes"]["ship"]["num_item_received"] > 150


# --- per-type report ---------------------------------------------------------


def test_report_counts_scrap_apart_from_completed(model):
    _inspection_line(model, {"b_ship": 0.8, "b_scrap": 0.2}, price=10)
    result = run_simulation(500, seed=4, model=model)

    report = result["part_types"]["part"]
    assert report["completed"] == result["nodes"]["ship"]["num_item_received"]
    assert report["scrapped"] == result["nodes"]["scrap"]["num_item_received"]
    assert report["scrapped"] > 0
    assert report["completed_attribute_sums"] == {
        "price": 10 * report["completed"]
    }


def test_report_without_scrap_sink_has_zero_scrapped(model):
    create_source("src", inter_arrival_time=1, blocking=True,
                  part_type="part", model=model)
    create_buffer("b", capacity=10, model=model)
    create_sink("ship", model=model)
    connect("b", "src", "ship", model=model)

    result = run_simulation(20, seed=1, model=model)

    assert result["part_types"]["part"]["scrapped"] == 0
    assert result["part_types"]["part"]["completed"] > 0
