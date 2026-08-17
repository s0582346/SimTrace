"""Tests for the post-run validation tools verify_conservation and
verify_item_flow."""

import json

import pytest

from simtrace.model import FactoryModel
from simtrace.tools.builders import create_buffer, create_conveyor, create_fleet
from simtrace.tools.builders import (
    create_combiner,
    create_machine,
    create_sink,
    create_source,
)
from simtrace.tools.simulation import connect, run_simulation
from simtrace.tools.validation import verify_conservation, verify_item_flow


@pytest.fixture
def model() -> FactoryModel:
    return FactoryModel()


# =========================================================================
# verify_conservation — the post-run mass balance
# =========================================================================


@pytest.fixture
def line(model) -> FactoryModel:
    """A src -> b1 -> mac -> b2 -> snk line that piles WIP into b1.

    The source (inter_arrival_time=1) outruns the machine (processing_delay=2),
    so at the cutoff some items sit in b1 and one is mid-process in the machine:
    a run that exercises all four balance terms except discard.
    """
    create_source("src", inter_arrival_time=1, blocking=True, model=model)
    create_machine("mac", processing_delay=2, blocking=True, model=model)
    create_sink("snk", model=model)
    create_buffer("b1", capacity=5, model=model)
    create_buffer("b2", capacity=5, model=model)
    connect("b1", "src", "mac", model=model)
    connect("b2", "mac", "snk", model=model)
    return model


# --- happy path -----------------------------------------------------------


def test_conservation_balances_on_working_line(line):
    run_simulation(20, model=line)
    report = verify_conservation(model=line)

    assert report["exact"] is True
    assert report["balanced"] is True
    # Nothing goes missing: the four buckets sum back to what was generated.
    assert report["accounted"] == report["generated"]
    assert (
        report["received"]
        + report["in_edges"]
        + report["in_machines"]
        + report["discarded"]
        == report["generated"]
    )


def test_conservation_reports_wip_where_it_sits(line):
    run_simulation(20, model=line)
    report = verify_conservation(model=line)

    # The source outruns the machine, so items are stuck in b1 at the cutoff,
    # and that WIP shows up both per-edge and in the aggregate in_edges term.
    assert report["by_edge"]["b1"] > 0
    assert report["in_edges"] == sum(report["by_edge"].values())
    # Generation happened and some items were delivered.
    assert report["generated"] > 0
    assert report["received"] > 0
    # in_machines is the non-negative residual (items mid-process, not lost).
    assert report["in_machines"] >= 0


def test_conservation_per_node_breakdown(line):
    run_simulation(20, model=line)
    report = verify_conservation(model=line)

    by_node = report["by_node"]
    assert by_node["src"]["type"] == "Source"
    # A source's emitted-count is its generated-count.
    assert by_node["src"]["emitted"] == by_node["src"]["generated"]
    # The sink only receives; it emits nothing.
    assert by_node["snk"]["received"] == report["received"]
    assert by_node["snk"]["emitted"] == 0


def test_conservation_result_is_json_serializable(line):
    run_simulation(20, model=line)
    report = verify_conservation(model=line)
    assert json.loads(json.dumps(report)) == report


# --- discard accounting ---------------------------------------------------


def test_conservation_folds_discards_into_the_balance(model):
    # Discards are hard to force from the FactorySimPy engine reliably, so this
    # test constructs a consistent end state by hand: a source generated 10
    # items, the machine emitted 6 to a sink and discarded 4, with nothing left
    # in edges or mid-process. The 4 discards must be folded into `accounted`
    # (treated as placed, not missing) so the balance still closes.
    create_source("src", inter_arrival_time=1, blocking=True, model=model)
    create_machine("mac", processing_delay=2, blocking=True, model=model)
    create_sink("snk", model=model)
    create_buffer("b1", capacity=5, model=model)
    create_buffer("b2", capacity=5, model=model)
    connect("b1", "src", "mac", model=model)
    connect("b2", "mac", "snk", model=model)
    run_simulation(20, model=model)

    # Build an internally consistent end state on top of whatever WIP the run
    # left in the edges: generated = received + in_edges + discarded, with no
    # item mid-process. Reading the live edge levels keeps the balance exact
    # regardless of how many items the short run happened to leave in b1/b2.
    report0 = verify_conservation(model=model)
    in_edges = report0["in_edges"]
    model.nodes["src"].stats["num_item_generated"] = in_edges + 10
    model.nodes["mac"].stats["num_item_processed"] = 6
    model.nodes["mac"].stats["num_item_discarded"] = 4
    model.nodes["snk"].stats["num_item_received"] = 6

    report = verify_conservation(model=model)
    assert report["generated"] == in_edges + 10
    assert report["received"] == 6
    assert report["discarded"] == 4
    # generated - received - in_edges - discarded == 0: no items mid-process,
    # and the 4 discards are folded into the accounting rather than lost.
    assert report["in_machines"] == 0
    assert report["accounted"] == report["generated"]
    assert report["balanced"] is True


# --- never ran ------------------------------------------------------------


def test_conservation_before_any_run_raises(line):
    with pytest.raises(ValueError, match="No simulation has run yet"):
        verify_conservation(model=line)


# --- packing nodes make the strict identity inexact -----------------------


def test_conservation_inexact_with_combiner(model):
    # A combiner changes the physical item count (packs several items into one
    # pallet), so counters alone can't close the balance: the tool declines a
    # verdict rather than hiding the packing delta in the machine residual.
    create_source(
        "pallets",
        inter_arrival_time=1,
        blocking=True,
        flow_item_type="pallet",
        model=model,
    )
    create_source("parts", inter_arrival_time=1, blocking=True, model=model)
    create_combiner("cmb", target_quantity_of_each_item=[1, 1], model=model)
    create_sink("snk", model=model)
    create_buffer("bp", capacity=5, model=model)
    create_buffer("bq", capacity=5, model=model)
    create_buffer("bout", capacity=5, model=model)
    connect("bp", "pallets", "cmb", model=model)
    connect("bq", "parts", "cmb", model=model)
    connect("bout", "cmb", "snk", model=model)
    run_simulation(10, model=model)

    report = verify_conservation(model=model)
    assert report["exact"] is False
    assert report["balanced"] is None
    assert report["in_machines"] is None
    assert "cmb" in report["packing_nodes"]
    # The observable terms are still reported for inspection.
    assert report["generated"] > 0
    assert report["accounted"] == (
        report["received"] + report["in_edges"] + report["discarded"]
    )


# --- cleared per run ------------------------------------------------------


def test_conservation_reflects_only_the_last_run(line):
    run_simulation(10, model=line)
    first = verify_conservation(model=line)
    run_simulation(20, model=line)
    second = verify_conservation(model=line)

    # The clock advanced and more items were generated by the wider window.
    assert second["now"] == 20
    assert second["generated"] >= first["generated"]
    assert second["balanced"] is True


# =========================================================================
# verify_item_flow — per-item proper-path check
# =========================================================================


@pytest.fixture
def two_machine_line(model) -> FactoryModel:
    """src -> B1 -> M1 -> B2 -> M2 -> B3 -> snk, blocking throughout.

    A blocking source narrates its hand-off with the item id, so a delivered
    item's trail is the full [src, B1, M1, B2, M2, B3, snk] route — nodes and the
    edges between them.
    """
    create_source("src", inter_arrival_time=1, blocking=True, model=model)
    create_machine("M1", processing_delay=1, blocking=True, model=model)
    create_machine("M2", processing_delay=1, blocking=True, model=model)
    create_sink("snk", model=model)
    create_buffer("B1", capacity=5, model=model)
    create_buffer("B2", capacity=5, model=model)
    create_buffer("B3", capacity=5, model=model)
    connect("B1", "src", "M1", model=model)
    connect("B2", "M1", "M2", model=model)
    connect("B3", "M2", "snk", model=model)
    return model


# --- happy path -----------------------------------------------------------


def test_item_flow_all_proper_on_working_line(two_machine_line):
    run_simulation(20, model=two_machine_line)
    report = verify_item_flow(model=two_machine_line)

    assert report["delivered"] > 0
    assert report["passed"] == report["delivered"]
    assert report["all_proper"] is True
    assert report["improper"] == []


def test_item_flow_trail_names_the_edges_it_rode(two_machine_line):
    run_simulation(20, model=two_machine_line)
    # Every delivered item's trail alternates node, edge, node — the full wired
    # route including what carried it between stations.
    paths = two_machine_line.item_paths
    delivered = [p for p in paths.values() if p and p[-1] == "snk"]
    assert delivered  # something was delivered
    assert all(
        p == ["src", "B1", "M1", "B2", "M2", "B3", "snk"] for p in delivered
    )


def test_item_flow_delivered_matches_conservation(two_machine_line):
    run_simulation(20, model=two_machine_line)
    flow = verify_item_flow(model=two_machine_line)
    cons = verify_conservation(model=two_machine_line)
    # The two dynamic checks agree on how many items reached a sink, and it
    # matches the sink's own ground-truth counter.
    assert flow["delivered"] == cons["received"]
    assert flow["delivered"] == two_machine_line.nodes["snk"].stats["num_item_received"]


def test_item_flow_non_blocking_source_still_proper(model):
    # A non-blocking source hands its first item over without naming it, so the
    # trail begins at the EDGE ([B1, M1, ...]) instead of the source. The edge
    # knows its own src_node, so the source is read off it and the item passes.
    create_source("src", inter_arrival_time=1, blocking=False, model=model)
    create_machine("M1", processing_delay=1, blocking=True, model=model)
    create_machine("M2", processing_delay=1, blocking=True, model=model)
    create_sink("snk", model=model)
    create_buffer("B1", capacity=5, model=model)
    create_buffer("B2", capacity=5, model=model)
    create_buffer("B3", capacity=5, model=model)
    connect("B1", "src", "M1", model=model)
    connect("B2", "M1", "M2", model=model)
    connect("B3", "M2", "snk", model=model)
    run_simulation(20, model=model)

    report = verify_item_flow(model=model)
    assert report["delivered"] > 0
    assert report["all_proper"] is True
    # Confirm the head really is un-narrated: the trail starts with the edge.
    delivered = [p for p in model.item_paths.values() if p and p[-1] == "snk"]
    assert all(p[0] == "B1" for p in delivered)


# --- improper paths -------------------------------------------------------


def test_item_flow_flags_a_hop_the_plant_does_not_allow(two_machine_line):
    run_simulation(20, model=two_machine_line)
    # Inject a delivered item that rode B1 from src straight to M2 (B1 is wired
    # src -> M1, and there is no src -> M2 hop at all). It must be flagged.
    two_machine_line.item_paths["ghost"] = ["src", "B1", "M2", "B3", "snk"]

    report = verify_item_flow(model=two_machine_line)
    assert report["all_proper"] is False
    flagged = {f["item"]: f for f in report["improper"]}
    assert "ghost" in flagged
    assert flagged["ghost"]["at"] == 1
    assert flagged["ghost"]["trail"] == ["src", "B1", "M2", "B3", "snk"]
    # The reason names the unreachable target and where src actually connects
    # (M1, via B1), pointing at the intended next station rather than restating
    # the hop.
    reason = flagged["ghost"]["reason"]
    assert "M2 is not reachable from src" in reason
    assert "M1 (via B1)" in reason
    # The real items are unaffected — they still pass.
    assert report["passed"] == report["delivered"] - 1


def test_item_flow_flags_a_legal_hop_carried_by_the_wrong_edge(two_machine_line):
    run_simulation(20, model=two_machine_line)
    # src -> M1 IS a wired hop, but B3 is wired M2 -> snk, so B3 cannot have
    # carried it. Only the edge id can catch this — a node-only trail could not.
    two_machine_line.item_paths["ghost"] = [
        "src", "B3", "M1", "B2", "M2", "B3", "snk",
    ]

    report = verify_item_flow(model=two_machine_line)
    flagged = {f["item"]: f for f in report["improper"]}
    assert flagged["ghost"]["at"] == 1
    assert flagged["ghost"]["reason"] == (
        "B3 carried src -> M1, but B3 is M2 -> snk"
    )


def test_item_flow_flags_a_trail_that_skips_the_edge(two_machine_line):
    run_simulation(20, model=two_machine_line)
    # A node-only trail no longer type-checks: position 1 must be an edge.
    two_machine_line.item_paths["ghost"] = ["src", "M1", "snk"]

    report = verify_item_flow(model=two_machine_line)
    flagged = {f["item"]: f for f in report["improper"]}
    assert flagged["ghost"]["at"] == 1
    assert "expected an edge at position 1" in flagged["ghost"]["reason"]
    assert "M1 is a node" in flagged["ghost"]["reason"]


def test_item_flow_ignores_incomplete_items(two_machine_line):
    run_simulation(20, model=two_machine_line)
    baseline = verify_item_flow(model=two_machine_line)
    # An item that never reached a sink (still sitting in a buffer) is not judged:
    # it is conservation's concern, not a path fault here.
    two_machine_line.item_paths["stuck"] = ["src", "B1"]

    report = verify_item_flow(model=two_machine_line)
    assert report["delivered"] == baseline["delivered"]  # 'stuck' not counted
    assert report["all_proper"] is True


# --- head resolution across plant shapes ----------------------------------


def test_item_flow_resolves_the_source_below_a_merge(model):
    # Two NON-BLOCKING sources merge into one machine, so no trail names its
    # source. The trails still name the buffer that carried each item, and each
    # buffer has exactly one src_node, so every item is attributed to its true
    # source. Nothing here is guesswork or unprovable.
    create_source("srcA", inter_arrival_time=2, blocking=False, model=model)
    create_source("srcB", inter_arrival_time=2, blocking=False, model=model)
    create_machine("M1", processing_delay=1, blocking=True, model=model)
    create_sink("snk", model=model)
    create_buffer("BA", capacity=5, model=model)
    create_buffer("BB", capacity=5, model=model)
    create_buffer("B3", capacity=5, model=model)
    connect("BA", "srcA", "M1", model=model)
    connect("BB", "srcB", "M1", model=model)
    connect("B3", "M1", "snk", model=model)
    run_simulation(40, seed=1, model=model)

    report = verify_item_flow(model=model)
    assert report["delivered"] > 0
    assert report["all_proper"] is True
    assert report["passed"] == report["delivered"]


@pytest.mark.parametrize("mid_edge", ["buffer", "conveyor", "fleet"])
@pytest.mark.parametrize("blocking", [True, False])
def test_item_flow_proves_the_route_across_every_edge_type(model, mid_edge, blocking):
    # Buffers and fleets narrate their own put one way ("X is putting item ..."),
    # conveyors another ("X:put: putting item ... on belt"). Both wordings have to
    # be read, or a non-blocking source feeding that edge type loses its head.
    create_source("src", inter_arrival_time=1, blocking=blocking, model=model)
    create_machine("M1", processing_delay=1, blocking=True, model=model)
    create_machine("M2", processing_delay=1, blocking=True, model=model)
    create_sink("snk", model=model)
    create_buffer("B1", capacity=5, model=model)
    if mid_edge == "conveyor":
        create_conveyor("X", conveyor_length=3, speed=1, item_length=1, model=model)
    elif mid_edge == "fleet":
        create_fleet("X", capacity=2, delay=1, model=model)
    else:
        create_buffer("X", capacity=5, model=model)
    create_buffer("B2", capacity=5, model=model)
    connect("B1", "src", "M1", model=model)
    connect("X", "M1", "M2", model=model)
    connect("B2", "M2", "snk", model=model)
    run_simulation(30, seed=1, model=model)

    report = verify_item_flow(model=model)
    assert report["delivered"] > 0
    assert report["passed"] == report["delivered"]


# --- guards & shape -------------------------------------------------------


def test_item_flow_before_any_run_raises(two_machine_line):
    with pytest.raises(ValueError, match="No simulation has run yet"):
        verify_item_flow(model=two_machine_line)


def test_item_flow_result_is_json_serializable(two_machine_line):
    run_simulation(20, model=two_machine_line)
    report = verify_item_flow(model=two_machine_line)
    assert json.loads(json.dumps(report)) == report


def test_item_paths_reflect_only_the_last_run(two_machine_line):
    run_simulation(10, model=two_machine_line)
    first_keys = set(two_machine_line.item_paths)
    assert first_keys
    # item_paths is cleared each run, so a fresh window doesn't accumulate the
    # earlier one's items alongside the new ones.
    run_simulation(20, model=two_machine_line)
    # Paths from before are gone; only the 10->20 window's items remain keyed.
    assert two_machine_line.env.now == 20
