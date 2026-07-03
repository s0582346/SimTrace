"""Direct, transport-free tests for the node builders."""

import pytest

from factorysimpy.nodes.combiner import Combiner
from factorysimpy.nodes.machine import Machine
from factorysimpy.nodes.sink import Sink
from factorysimpy.nodes.source import Source
from factorysimpy.nodes.splitter import Splitter

from simgen.model import FactoryModel
from simgen.tools.builders import (
    create_combiner,
    create_machine,
    create_sink,
    create_source,
    create_splitter,
)


@pytest.fixture
def model() -> FactoryModel:
    return FactoryModel()


def test_create_source_registers_node(model):
    summary = create_source("src1", inter_arrival_time=2.0, model=model)

    assert summary["id"] == "src1"
    assert summary["type"] == "Source"
    assert summary["inter_arrival_time"] == 2.0
    assert summary["in_edges"] == 0 and summary["out_edges"] == 0

    node = model.nodes["src1"]
    assert isinstance(node, Source)
    assert node.env is model.env
    assert node.in_edges is None  # Source starts with no in_edges
    assert node.out_edges is None  # wired later via connect


def test_summary_is_json_serializable(model):
    import json

    summary = create_source("src1", model=model)
    # Round-trips with no env/simpy objects leaking through.
    assert json.loads(json.dumps(summary)) == summary


def test_node_setup_time_applied(model):
    create_source("src1", node_setup_time=3, model=model)
    assert model.nodes["src1"].node_setup_time == 3


def test_duplicate_id_rejected(model):
    create_source("src1", model=model)
    with pytest.raises(ValueError, match="already exists"):
        create_source("src1", model=model)


def test_empty_id_rejected(model):
    with pytest.raises(ValueError, match="non-empty string"):
        create_source("", model=model)


def test_non_blocking_zero_interarrival_rejected(model):
    # Surfaced by Source itself.
    with pytest.raises(ValueError):
        create_source("src1", inter_arrival_time=0, blocking=False, model=model)


def test_blocking_allows_zero_interarrival(model):
    summary = create_source(
        "src1", inter_arrival_time=0, blocking=True, model=model
    )
    assert summary["blocking"] is True


def test_generator_interarrival_rejected_in_v1(model):
    def gen():
        while True:
            yield 1.0

    with pytest.raises(ValueError, match="constant int or float"):
        create_source("src1", inter_arrival_time=gen(), model=model)


def test_bool_interarrival_rejected(model):
    with pytest.raises(ValueError, match="constant int or float"):
        create_source("src1", inter_arrival_time=True, model=model)


def test_bad_flow_item_type_rejected(model):
    with pytest.raises(ValueError, match="item.*pallet"):
        create_source("src1", flow_item_type="widget", model=model)


def test_pallet_flow_item_type_accepted(model):
    summary = create_source("src1", flow_item_type="pallet", model=model)
    assert summary["flow_item_type"] == "pallet"


def test_create_sink_registers_node(model):
    summary = create_sink("snk1", model=model)

    assert summary["id"] == "snk1"
    assert summary["type"] == "Sink"
    assert summary["in_edges"] == 0 and summary["out_edges"] == 0

    node = model.nodes["snk1"]
    assert isinstance(node, Sink)
    assert node.env is model.env
    assert node.in_edges is None  # wired later via connect
    assert node.out_edges is None  # Sink is terminal — no out_edges


def test_sink_summary_is_json_serializable(model):
    import json

    summary = create_sink("snk1", model=model)
    assert json.loads(json.dumps(summary)) == summary


def test_sink_node_setup_time_applied(model):
    create_sink("snk1", node_setup_time=4, model=model)
    assert model.nodes["snk1"].node_setup_time == 4


def test_sink_duplicate_id_rejected(model):
    create_sink("snk1", model=model)
    with pytest.raises(ValueError, match="already exists"):
        create_sink("snk1", model=model)


def test_sink_empty_id_rejected(model):
    with pytest.raises(ValueError, match="non-empty string"):
        create_sink("", model=model)


def test_sink_bool_setup_time_rejected(model):
    with pytest.raises(ValueError, match="constant int or float"):
        create_sink("snk1", node_setup_time=True, model=model)


def test_source_and_sink_share_id_space(model):
    create_source("n1", model=model)
    with pytest.raises(ValueError, match="already exists"):
        create_sink("n1", model=model)


def test_create_machine_registers_node(model):
    summary = create_machine(
        "m1", work_capacity=2, processing_delay=1.5, model=model
    )

    assert summary["id"] == "m1"
    assert summary["type"] == "Machine"
    assert summary["work_capacity"] == 2
    assert summary["processing_delay"] == 1.5
    assert summary["blocking"] is True  # Machine default
    assert summary["in_edge_selection"] == "FIRST_AVAILABLE"
    assert summary["out_edge_selection"] == "FIRST_AVAILABLE"
    assert summary["in_edges"] == 0 and summary["out_edges"] == 0

    node = model.nodes["m1"]
    assert isinstance(node, Machine)
    assert node.env is model.env
    assert node.in_edges is None  # wired later via connect
    assert node.out_edges is None


def test_machine_summary_is_json_serializable(model):
    import json

    summary = create_machine("m1", model=model)
    assert json.loads(json.dumps(summary)) == summary


def test_machine_node_setup_time_applied(model):
    create_machine("m1", node_setup_time=2, model=model)
    assert model.nodes["m1"].node_setup_time == 2


def test_machine_duplicate_id_rejected(model):
    create_machine("m1", model=model)
    with pytest.raises(ValueError, match="already exists"):
        create_machine("m1", model=model)


def test_machine_empty_id_rejected(model):
    with pytest.raises(ValueError, match="non-empty string"):
        create_machine("", model=model)


def test_machine_zero_work_capacity_rejected(model):
    with pytest.raises(ValueError, match="positive int"):
        create_machine("m1", work_capacity=0, model=model)


def test_machine_float_work_capacity_rejected(model):
    with pytest.raises(ValueError, match="work_capacity must be an int"):
        create_machine("m1", work_capacity=2.0, model=model)


def test_machine_bool_work_capacity_rejected(model):
    with pytest.raises(ValueError, match="work_capacity must be an int"):
        create_machine("m1", work_capacity=True, model=model)


def test_machine_generator_processing_delay_rejected_in_v1(model):
    def gen():
        while True:
            yield 1.0

    with pytest.raises(ValueError, match="constant int or float"):
        create_machine("m1", processing_delay=gen(), model=model)


def test_machine_non_blocking_accepted(model):
    summary = create_machine("m1", blocking=False, model=model)
    assert summary["blocking"] is False


def test_machine_edge_selection_strategies_echoed(model):
    summary = create_machine(
        "m1",
        in_edge_selection="ROUND_ROBIN",
        out_edge_selection="RANDOM",
        model=model,
    )
    assert summary["in_edge_selection"] == "ROUND_ROBIN"
    assert summary["out_edge_selection"] == "RANDOM"


def test_create_splitter_registers_node(model):
    summary = create_splitter("sp1", processing_delay=0.5, model=model)

    assert summary["id"] == "sp1"
    assert summary["type"] == "Splitter"
    assert summary["mode"] == "UNPACK"  # default
    assert summary["split_quantity"] is None
    assert summary["processing_delay"] == 0.5
    assert summary["blocking"] is True  # default
    assert summary["in_edges"] == 0 and summary["out_edges"] == 0

    node = model.nodes["sp1"]
    assert isinstance(node, Splitter)
    assert node.env is model.env
    assert node.in_edges is None  # wired later via connect
    assert node.out_edges is None


def test_splitter_summary_is_json_serializable(model):
    import json

    summary = create_splitter("sp1", model=model)
    assert json.loads(json.dumps(summary)) == summary


def test_splitter_split_mode_with_quantity(model):
    summary = create_splitter("sp1", mode="SPLIT", split_quantity=3, model=model)
    assert summary["mode"] == "SPLIT"
    assert summary["split_quantity"] == 3
    assert model.nodes["sp1"].split_quantity == 3


def test_splitter_split_mode_requires_quantity(model):
    with pytest.raises(ValueError, match="split_quantity is required"):
        create_splitter("sp1", mode="SPLIT", model=model)


def test_splitter_bad_mode_rejected(model):
    with pytest.raises(ValueError, match="UNPACK.*SPLIT"):
        create_splitter("sp1", mode="EXPLODE", model=model)


def test_splitter_zero_split_quantity_rejected(model):
    with pytest.raises(ValueError, match="positive int"):
        create_splitter("sp1", mode="SPLIT", split_quantity=0, model=model)


def test_splitter_float_split_quantity_rejected(model):
    with pytest.raises(ValueError, match="split_quantity must be an int"):
        create_splitter("sp1", mode="SPLIT", split_quantity=2.0, model=model)


def test_splitter_bool_split_quantity_rejected(model):
    with pytest.raises(ValueError, match="split_quantity must be an int"):
        create_splitter("sp1", mode="SPLIT", split_quantity=True, model=model)


def test_splitter_unpack_ignores_quantity(model):
    # split_quantity is not validated in UNPACK mode (it's ignored).
    summary = create_splitter("sp1", mode="UNPACK", model=model)
    assert summary["split_quantity"] is None


def test_splitter_duplicate_id_rejected(model):
    create_splitter("sp1", model=model)
    with pytest.raises(ValueError, match="already exists"):
        create_splitter("sp1", model=model)


def test_splitter_generator_processing_delay_rejected_in_v1(model):
    def gen():
        while True:
            yield 1.0

    with pytest.raises(ValueError, match="constant int or float"):
        create_splitter("sp1", processing_delay=gen(), model=model)


def test_create_combiner_registers_node(model):
    summary = create_combiner(
        "cb1", target_quantity_of_each_item=[1, 2], model=model
    )

    assert summary["id"] == "cb1"
    assert summary["type"] == "Combiner"
    assert summary["target_quantity_of_each_item"] == [1, 2]
    assert summary["blocking"] is True  # default
    assert summary["out_edge_selection"] == "FIRST_AVAILABLE"
    assert summary["in_edges"] == 0 and summary["out_edges"] == 0

    node = model.nodes["cb1"]
    assert isinstance(node, Combiner)
    assert node.env is model.env
    assert node.in_edges is None  # wired later via connect
    assert node.out_edges is None


def test_combiner_summary_is_json_serializable(model):
    import json

    summary = create_combiner("cb1", model=model)
    assert json.loads(json.dumps(summary)) == summary


def test_combiner_default_quantity(model):
    summary = create_combiner("cb1", model=model)
    assert summary["target_quantity_of_each_item"] == [1]


def test_combiner_node_setup_time_applied(model):
    create_combiner("cb1", node_setup_time=2, model=model)
    assert model.nodes["cb1"].node_setup_time == 2


def test_combiner_duplicate_id_rejected(model):
    create_combiner("cb1", model=model)
    with pytest.raises(ValueError, match="already exists"):
        create_combiner("cb1", model=model)


def test_combiner_empty_quantity_list_rejected(model):
    with pytest.raises(ValueError, match="non-empty list"):
        create_combiner("cb1", target_quantity_of_each_item=[], model=model)


def test_combiner_non_list_quantity_rejected(model):
    with pytest.raises(ValueError, match="non-empty list"):
        create_combiner("cb1", target_quantity_of_each_item=3, model=model)


def test_combiner_zero_quantity_entry_rejected(model):
    with pytest.raises(ValueError, match="positive ints"):
        create_combiner("cb1", target_quantity_of_each_item=[1, 0], model=model)


def test_combiner_float_quantity_entry_rejected(model):
    with pytest.raises(ValueError, match="positive ints"):
        create_combiner("cb1", target_quantity_of_each_item=[1, 2.0], model=model)


def test_combiner_bool_quantity_entry_rejected(model):
    with pytest.raises(ValueError, match="positive ints"):
        create_combiner(
            "cb1", target_quantity_of_each_item=[True], model=model
        )


def test_combiner_generator_processing_delay_rejected_in_v1(model):
    def gen():
        while True:
            yield 1.0

    with pytest.raises(ValueError, match="constant int or float"):
        create_combiner("cb1", processing_delay=gen(), model=model)
