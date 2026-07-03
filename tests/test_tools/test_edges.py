"""Direct, transport-free tests for the edge builders."""

import pytest

from factorysimpy.edges.buffer import Buffer
from factorysimpy.edges.continuous_conveyor import ConveyorBelt
from factorysimpy.edges.fleet import Fleet

from simgen.model import FactoryModel
from simgen.tools.builders import (
    create_buffer,
    create_conveyor,
    create_fleet,
)
from simgen.tools.builders import create_source


@pytest.fixture
def model() -> FactoryModel:
    return FactoryModel()


# --- Buffer ---------------------------------------------------------------


def test_create_buffer_registers_edge(model):
    summary = create_buffer("buf1", capacity=4, delay=0.5, mode="LIFO", model=model)

    assert summary["id"] == "buf1"
    assert summary["type"] == "Buffer"
    assert summary["capacity"] == 4
    assert summary["delay"] == 0.5
    assert summary["mode"] == "LIFO"
    assert summary["src"] is None and summary["dest"] is None

    edge = model.edges["buf1"]
    assert isinstance(edge, Buffer)
    assert edge.env is model.env
    assert edge.src_node is None  # wired later via connect
    assert edge.dest_node is None


def test_buffer_summary_is_json_serializable(model):
    import json

    summary = create_buffer("buf1", model=model)
    assert json.loads(json.dumps(summary)) == summary


def test_buffer_defaults(model):
    summary = create_buffer("buf1", model=model)
    assert summary["capacity"] == 1
    assert summary["delay"] == 0
    assert summary["mode"] == "FIFO"


def test_buffer_duplicate_id_rejected(model):
    create_buffer("buf1", model=model)
    with pytest.raises(ValueError, match="already exists"):
        create_buffer("buf1", model=model)


def test_buffer_empty_id_rejected(model):
    with pytest.raises(ValueError, match="non-empty string"):
        create_buffer("", model=model)


def test_buffer_zero_capacity_rejected(model):
    with pytest.raises(ValueError, match="positive int"):
        create_buffer("buf1", capacity=0, model=model)


def test_buffer_float_capacity_rejected(model):
    with pytest.raises(ValueError, match="capacity must be an int"):
        create_buffer("buf1", capacity=2.0, model=model)


def test_buffer_bool_capacity_rejected(model):
    with pytest.raises(ValueError, match="capacity must be an int"):
        create_buffer("buf1", capacity=True, model=model)


def test_buffer_bad_mode_rejected(model):
    with pytest.raises(ValueError, match="FIFO.*LIFO"):
        create_buffer("buf1", mode="STACK", model=model)


def test_buffer_generator_delay_rejected_in_v1(model):
    def gen():
        while True:
            yield 1.0

    with pytest.raises(ValueError, match="constant int or float"):
        create_buffer("buf1", delay=gen(), model=model)


# --- Conveyor -------------------------------------------------------------


def test_create_conveyor_registers_edge(model):
    summary = create_conveyor(
        "cv1", conveyor_length=5, speed=2, item_length=0.5, model=model
    )

    assert summary["id"] == "cv1"
    assert summary["type"] == "ConveyorBelt"
    assert summary["conveyor_length"] == 5
    assert summary["speed"] == 2
    assert summary["item_length"] == 0.5
    assert summary["accumulating"] is False
    # derived: int(ceil(5) / 0.5) == 10
    assert summary["capacity"] == 10
    assert summary["src"] is None and summary["dest"] is None

    edge = model.edges["cv1"]
    assert isinstance(edge, ConveyorBelt)
    assert edge.env is model.env
    assert edge.src_node is None
    assert edge.dest_node is None


def test_conveyor_summary_is_json_serializable(model):
    import json

    summary = create_conveyor(
        "cv1", conveyor_length=5, speed=2, item_length=0.5, model=model
    )
    assert json.loads(json.dumps(summary)) == summary


def test_conveyor_accumulating_echoed(model):
    summary = create_conveyor(
        "cv1",
        conveyor_length=10,
        speed=2,
        item_length=1,
        accumulating=True,
        model=model,
    )
    assert summary["accumulating"] is True
    assert summary["capacity"] == 10


def test_conveyor_capacity_floored_to_zero_rejected(model):
    # item_length larger than the belt -> derived capacity 0.
    with pytest.raises(ValueError, match="capacity must be >= 1"):
        create_conveyor(
            "cv1", conveyor_length=2, speed=1, item_length=5, model=model
        )


def test_conveyor_zero_length_rejected(model):
    with pytest.raises(ValueError, match="conveyor_length must be > 0"):
        create_conveyor(
            "cv1", conveyor_length=0, speed=1, item_length=1, model=model
        )


def test_conveyor_zero_speed_rejected(model):
    with pytest.raises(ValueError, match="speed must be > 0"):
        create_conveyor(
            "cv1", conveyor_length=5, speed=0, item_length=1, model=model
        )


def test_conveyor_non_bool_accumulating_rejected(model):
    with pytest.raises(ValueError, match="accumulating must be a bool"):
        create_conveyor(
            "cv1",
            conveyor_length=5,
            speed=1,
            item_length=1,
            accumulating=1,
            model=model,
        )


def test_conveyor_duplicate_id_rejected(model):
    create_conveyor("cv1", conveyor_length=5, speed=1, item_length=1, model=model)
    with pytest.raises(ValueError, match="already exists"):
        create_conveyor(
            "cv1", conveyor_length=5, speed=1, item_length=1, model=model
        )


# --- Fleet ----------------------------------------------------------------


def test_create_fleet_registers_edge(model):
    summary = create_fleet("flt1", capacity=3, delay=2, transit_delay=5, model=model)

    assert summary["id"] == "flt1"
    assert summary["type"] == "Fleet"
    assert summary["capacity"] == 3
    assert summary["delay"] == 2
    assert summary["transit_delay"] == 5
    assert summary["src"] is None and summary["dest"] is None

    edge = model.edges["flt1"]
    assert isinstance(edge, Fleet)
    assert edge.env is model.env
    assert edge.src_node is None
    assert edge.dest_node is None


def test_fleet_summary_is_json_serializable(model):
    import json

    summary = create_fleet("flt1", model=model)
    assert json.loads(json.dumps(summary)) == summary


def test_fleet_defaults(model):
    summary = create_fleet("flt1", model=model)
    assert summary["capacity"] == 1
    assert summary["delay"] == 1
    assert summary["transit_delay"] == 0


def test_fleet_zero_capacity_rejected(model):
    with pytest.raises(ValueError, match="positive int"):
        create_fleet("flt1", capacity=0, model=model)


def test_fleet_bool_capacity_rejected(model):
    with pytest.raises(ValueError, match="capacity must be an int"):
        create_fleet("flt1", capacity=True, model=model)


def test_fleet_generator_transit_delay_rejected_in_v1(model):
    def gen():
        while True:
            yield 1.0

    with pytest.raises(ValueError, match="constant int or float"):
        create_fleet("flt1", transit_delay=gen(), model=model)


# --- Shared id namespace --------------------------------------------------


def test_node_and_edge_share_id_space(model):
    create_source("x1", model=model)
    with pytest.raises(ValueError, match="already exists"):
        create_buffer("x1", model=model)


def test_edge_and_node_share_id_space(model):
    create_buffer("x1", model=model)
    with pytest.raises(ValueError, match="already exists"):
        create_source("x1", model=model)
