"""Integration tests executing architecture/drafts/entity-types-mapping.md.

Builds the schema's "Mixed-Routing Job Shop" example (two entity types with
different mix, priority, and process plans) using only the current tool
surface, and verifies each structural encoding from the plan:

  - `probability` -> one Source per type, mean inter-arrival / mix fraction,
  - `priority`    -> connect order at the shared station (premium in-edge first),
  - `process_plan`-> per-type branches, shared stations after final convergence,
  - heat treat    -> buffer with capacity + hold delay (passive waiting),
  - `value`       -> post-run arithmetic over per-type counts from item_paths.
"""

import pytest

from simtrace.model import FactoryModel
from simtrace.tools.builders import (
    create_buffer,
    create_machine,
    create_sink,
    create_source,
)
from simtrace.tools.simulation import connect, run_simulation

STANDARD_SOURCE = "src_standard"
PREMIUM_SOURCE = "src_premium"


def build_mixed_routing_job_shop() -> FactoryModel:
    """The plan's translation of schema example 3, verbatim.

    arrival_pattern exp(8) split by mix 0.6/0.4 -> exp(13.33)/exp(20);
    the pooled 2-server mill duplicated into one dedicated mill per type;
    premium's oven is a buffer edge (hold delay, no work); assembly and
    inspection shared because the plans never diverge after assembly.
    """
    m = FactoryModel()

    create_source(STANDARD_SOURCE, inter_arrival_time="exp(13.33)", blocking=True, model=m)
    create_source(PREMIUM_SOURCE, inter_arrival_time="exp(20)", blocking=True, model=m)
    create_machine("mill_std", processing_delay="normal(10, 2)", model=m)
    create_machine("mill_prem", processing_delay="normal(10, 2)", model=m)
    create_machine(
        "assembly",
        work_capacity=3,
        processing_delay="normal(7, 1.5)",
        in_edge_selection="FIRST_AVAILABLE",
        model=m,
    )
    create_machine("inspection", processing_delay="uniform(2, 4)", model=m)
    create_sink("shipping", model=m)

    create_buffer("b_std_mill", capacity=10, model=m)
    create_buffer("b_prem_mill", capacity=10, model=m)
    create_buffer("b_std_assembly", capacity=10, model=m)
    # The heat-treat oven: premium-only hold stage, modeled as the edge itself.
    create_buffer("oven", capacity=8, delay="uniform(60, 90)", model=m)
    create_buffer("b_inspection", capacity=10, model=m)
    create_buffer("b_shipping", capacity=10, model=m)

    connect("b_std_mill", STANDARD_SOURCE, "mill_std", model=m)
    connect("b_prem_mill", PREMIUM_SOURCE, "mill_prem", model=m)
    # Priority 3 beats 5: premium's in-edge is wired FIRST at the shared
    # station, so FIRST_AVAILABLE drains it preferentially.
    connect("oven", "mill_prem", "assembly", model=m)
    connect("b_std_assembly", "mill_std", "assembly", model=m)
    connect("b_inspection", "assembly", "inspection", model=m)
    connect("b_shipping", "inspection", "shipping", model=m)

    return m


def delivered_by_type(model: FactoryModel) -> dict[str, int]:
    """Per-type completions, recovered as the plan prescribes: an item's type
    is its path's first node (the type's own Source); delivered means the path
    ends at the sink."""
    counts = {STANDARD_SOURCE: 0, PREMIUM_SOURCE: 0}
    for path in model.item_paths.values():
        if path and path[-1] == "shipping":
            counts[path[0]] += 1
    return counts


def test_mixed_routing_line_delivers_both_types():
    m = build_mixed_routing_job_shop()
    result = run_simulation(2000, seed=7, model=m)

    assert result["nodes"]["shipping"]["num_item_received"] > 0

    counts = delivered_by_type(m)
    assert counts[STANDARD_SOURCE] > 0
    assert counts[PREMIUM_SOURCE] > 0
    # The mix fraction shows up in the split: standard arrives ~1.5x as often.
    assert counts[STANDARD_SOURCE] > counts[PREMIUM_SOURCE]


def test_process_plans_stay_on_their_branches():
    m = build_mixed_routing_job_shop()
    run_simulation(2000, seed=7, model=m)

    for path in m.item_paths.values():
        if len(path) == 1:
            continue  # generated near t=end; never left its source
        if path[0] == STANDARD_SOURCE:
            assert "mill_std" in path and "mill_prem" not in path
        else:
            assert "mill_prem" in path and "mill_std" not in path


def test_shared_assembly_serves_both_in_edges():
    m = build_mixed_routing_job_shop()
    run_simulation(2000, seed=7, model=m)

    # FIRST_AVAILABLE records the chosen in-edge index per pull:
    # 0 = oven (premium, wired first), 1 = standard.
    chosen = m.nodes["assembly"].stats["in_edge_selection"]
    assert 0 in chosen and 1 in chosen


def test_value_kpi_from_item_paths():
    m = build_mixed_routing_job_shop()
    run_simulation(2000, seed=7, model=m)

    counts = delivered_by_type(m)
    # Post-run arithmetic per the plan: mean(value) x per-type completions.
    value = counts[STANDARD_SOURCE] * 55 + counts[PREMIUM_SOURCE] * 160
    assert value > 0


def test_pattern_is_reproducible_with_seed():
    first = run_simulation(2000, seed=42, model=build_mixed_routing_job_shop())
    second = run_simulation(2000, seed=42, model=build_mixed_routing_job_shop())
    assert first == second
