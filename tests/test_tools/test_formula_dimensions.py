"""Dimension test of formulas in the code, by changing units.

Two rules of dimensional analysis, both checked by writing the inputs in
another unit:

1. A result with a dimension scales the way its dimension says, and a result
   without one does not change at all.
2. A non-linear function (int, ceil, floor, round, log, exp) is applied only to
   a dimensionless argument. Otherwise the result depends on the unit. The
   first rule alone passes such a formula, because its result has no dimension.
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
from simtrace.tools.simulation import connect, run_simulation


@pytest.mark.parametrize("length, item", [(6, 0.3), (8, 0.4), (6.5, 0.3), (1, 0.1), (3, 0.7)])
@pytest.mark.parametrize("unit_factor", [100, 1000])
def test_places_on_a_belt_have_no_dimension(length, item, unit_factor):
    """Belt length / part length is a count: the same in m, cm and mm."""
    in_m = create_conveyor("a", conveyor_length=length, speed=1, item_length=item,
                           model=FactoryModel())["capacity"]
    in_smaller = create_conveyor(
        "a", conveyor_length=length * unit_factor, speed=1,
        item_length=item * unit_factor, model=FactoryModel())["capacity"]
    assert in_m == in_smaller


def belt_time(length, item, speed):
    """Mean time from source to sink on a line whose only delay is one belt."""
    m = FactoryModel()
    create_source("src", inter_arrival_time=5, blocking=True, item_length=item, model=m)
    create_buffer("b1", capacity=2, model=m)
    create_machine("m1", processing_delay=0, model=m)
    create_conveyor("belt", conveyor_length=length, speed=speed, item_length=item, model=m)
    create_machine("m2", processing_delay=0, model=m)
    create_buffer("b2", capacity=2, model=m)
    create_sink("dock", model=m)
    for edge, src, dest in (("b1", "src", "m1"), ("belt", "m1", "m2"), ("b2", "m2", "dock")):
        connect(edge, src, dest, model=m)
    dock = run_simulation(40, seed=1, model=m)["nodes"]["dock"]
    return dock["total_cycle_time"] / dock["num_item_received"]


@pytest.mark.parametrize("length, item, speed", [(6, 0.3, 6), (6.5, 0.3, 6.5), (8, 0.4, 12)])
def test_travel_time_on_a_belt_is_length_over_speed(length, item, speed):
    """[length] / [length/time] is a time, from the belt length and not from the places."""
    assert belt_time(length, item, speed) == pytest.approx(length / speed)


def test_travel_time_does_not_change_when_lengths_and_speed_change_unit():
    """Metres and metres per minute against centimetres and centimetres per minute."""
    assert belt_time(6.5, 0.3, 6.5) == pytest.approx(belt_time(650, 30, 650))
