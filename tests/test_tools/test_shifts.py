"""Tests for shift calendars (ShiftCalendar and Machine(calendar=...)).

The lines are built directly in FactorySimPy. Every calendar here has a cycle
of 20 and works from 0 to 10 unless a test says otherwise.
"""

import contextlib
import io
import re

import pytest
import simpy

from factorysimpy.edges.buffer import Buffer
from factorysimpy.nodes.machine import Machine
from factorysimpy.nodes.sink import Sink
from factorysimpy.nodes.source import Source
from factorysimpy.utils.shift_calendar import ShiftCalendar

_EVENT = re.compile(r"^T=(\d+\.\d+): m (breaks down|is repaired|shift starts|shift ends)$")


def _line(calendar, iat, processing, time_to_failure=None, repair_time=None,
          source_calendar=None):
    """src -> b_in -> m -> b_out -> snk."""
    env = simpy.Environment()
    src = Source(env, "src", inter_arrival_time=iat, blocking=True, calendar=source_calendar)
    b_in = Buffer(env, "b_in", capacity=100)
    m = Machine(env, "m", processing_delay=processing, time_to_failure=time_to_failure,
                repair_time=repair_time, calendar=calendar)
    b_out = Buffer(env, "b_out", capacity=100)
    snk = Sink(env, "snk")
    b_in.connect(src, m)
    b_out.connect(m, snk)
    return env, m, snk, src


def _run(env, until):
    """Run to `until` and return the machine's events as (time, kind)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        env.run(until=until)
    events = []
    for line in out.getvalue().splitlines():
        match = _EVENT.match(line)
        if match:
            events.append((float(match.group(1)), match.group(2)))
    return events


def _received_by(calendar, iat, processing, until):
    env, _, snk, _ = _line(calendar, iat, processing)
    _run(env, until)
    return snk.stats["num_item_received"]


# --- ShiftCalendar --------------------------------------------------------------


def test_window_includes_start_and_excludes_end():
    cal = ShiftCalendar(20, [[0, 10]])
    assert cal.is_working(0)
    assert cal.is_working(9.9)
    assert not cal.is_working(10)
    assert cal.is_working(20)
    assert not cal.is_working(35)


def test_next_change_steps_through_the_cycles():
    cal = ShiftCalendar(20, [[0, 10]])
    assert cal.next_change(0) == 10
    assert cal.next_change(10) == 20
    assert cal.next_change(25) == 30


def test_touching_windows_are_joined():
    cal = ShiftCalendar(20, [[0, 5], [5, 10]])
    assert cal.windows == [[0, 10]]
    assert cal.next_change(2) == 10


def test_window_running_into_the_next_cycle():
    # Works 15-20 and 0-5, so one stretch from 15 to 25.
    cal = ShiftCalendar(20, [[0, 5], [15, 20]])
    assert cal.next_change(16) == 25


def test_calendar_that_always_works_never_changes():
    cal = ShiftCalendar(20, [[0, 20]])
    assert cal.next_change(3) is None
    assert cal.add_working_time(3, 50) == 53


def test_add_working_time_skips_the_gaps():
    cal = ShiftCalendar(20, [[0, 10]])
    assert cal.add_working_time(2, 5) == 7
    # 8-10 gives 2, 20-22 gives the other 2.
    assert cal.add_working_time(8, 4) == 22
    # Starting in a gap waits for the window.
    assert cal.add_working_time(12, 3) == 23
    # 25 units: 5-10, 20-30, 40-50.
    assert cal.add_working_time(5, 25) == 50


def test_working_time_between():
    cal = ShiftCalendar(20, [[0, 10]])
    assert cal.working_time_between(8, 22) == 4
    assert cal.working_time_between(0, 40) == 20
    assert cal.working_time_between(12, 18) == 0


@pytest.mark.parametrize(
    "cycle, windows, message",
    [
        (0, [[0, 10]], "cycle must be"),
        (True, [[0, 10]], "cycle must be"),
        (20, [], "at least one"),
        (20, [[0, 10, 15]], "pair"),
        (20, [[5, 5]], "start < end"),
        (20, [[0, 25]], "start < end <= cycle"),
        (20, [[-1, 5]], "0 <= start"),
        (20, [[10, 15], [0, 5]], "ascending"),
        (20, [[0, 10], [5, 15]], "ascending"),
        (20, [["0", 10]], "numbers"),
    ],
)
def test_bad_calendars_are_rejected(cycle, windows, message):
    with pytest.raises(ValueError, match=message):
        ShiftCalendar(cycle, windows)


# --- Machine with a calendar ----------------------------------------------------


def test_job_pauses_at_shift_end_and_finishes_in_the_next_window():
    # Items arrive from t=1, every job takes 5.
    #   job 1: 1-6
    #   job 2: 6-10 (4 done), off, 20-21 -> leaves at 21
    #   job 3: 21-26
    #   job 4: 26-30 (4 done), off, 40-41 -> leaves at 41
    cal = ShiftCalendar(20, [[0, 10]])
    assert _received_by(cal, 1, 5, 20.5) == 1
    assert _received_by(cal, 1, 5, 21.5) == 2
    assert _received_by(cal, 1, 5, 26.5) == 3
    assert _received_by(cal, 1, 5, 40.5) == 3
    assert _received_by(cal, 1, 5, 41.5) == 4


def test_no_new_job_starts_outside_a_window():
    # The first item arrives at 12, off shift. It starts at 20 and leaves at 25.
    cal = ShiftCalendar(20, [[0, 10]])
    assert _received_by(cal, 12, 5, 24.9) == 0
    assert _received_by(cal, 12, 5, 25.1) == 1


def test_calendar_that_starts_off():
    # Works 5-10. The item from t=1 waits until 5 and leaves at 7, the next at 9.
    cal = ShiftCalendar(20, [[5, 10]])
    env, _, snk, _ = _line(cal, 1, 2)
    events = _run(env, 8)
    assert events == [(0.0, "shift ends"), (5.0, "shift starts")]
    assert snk.stats["num_item_received"] == 1


def test_off_shift_time_is_its_own_state():
    cal = ShiftCalendar(20, [[0, 10]])
    env, m, _, _ = _line(cal, 1, 5)
    _run(env, 40)
    m.update_final_state_time(env.now)
    states = m.stats["total_time_spent_in_states"]
    assert states["OFF_SHIFT_STATE"] == 20
    assert states["IDLE_STATE"] + states["ATLEAST_ONE_PROCESSING_STATE"] == pytest.approx(20)


def test_machine_without_calendar_has_no_off_shift_state():
    env, m, _, _ = _line(None, 1, 5)
    _run(env, 10)
    assert "OFF_SHIFT_STATE" not in m.stats["total_time_spent_in_states"]


def test_breakdowns_count_working_time_only():
    # Time to failure 8, repair 4, both in working time.
    #   fails at 8, repairs 8-10 and 20-22
    #   next failure after 8 more working units: 22-30, so at 30, as the shift ends
    #   repairs 40-44
    cal = ShiftCalendar(20, [[0, 10]])
    env, m, _, _ = _line(cal, 100, 1, time_to_failure=8, repair_time=4)
    events = _run(env, 45)
    breakdowns = [t for t, kind in events if kind == "breaks down"]
    repairs = [t for t, kind in events if kind == "is repaired"]
    assert breakdowns == [8, 30]
    assert repairs == [22, 44]
    assert m.stats["num_breakdowns"] == 2
    assert m.stats["time_down"] == 8


def test_breakdown_and_shift_end_at_the_same_time_while_busy():
    # Same timeline as above, but the machine is busy at 30 when both stops come.
    cal = ShiftCalendar(20, [[0, 10]])
    env, m, snk, _ = _line(cal, 1, 5, time_to_failure=8, repair_time=4)
    events = _run(env, 60)
    assert [t for t, kind in events if kind == "breaks down"] == [8, 30]
    assert snk.stats["num_item_received"] > 0


# --- Source with a calendar -----------------------------------------------------


def _generated_by(iat, until):
    cal = ShiftCalendar(20, [[0, 10]])
    env, _, _, src = _line(None, iat, 0.1, source_calendar=cal)
    _run(env, until)
    return src.stats["num_item_generated"]


def test_source_arrivals_count_working_time_only():
    # One item every 4 units of working time:
    #   4, 8, then 8-10 and 20-22 -> 22, 26, 30 (at the window end), then 40-44 -> 44
    assert _generated_by(4, 21) == 2
    assert _generated_by(4, 23) == 3
    assert _generated_by(4, 29) == 4
    assert _generated_by(4, 31) == 5
    assert _generated_by(4, 43) == 5
    assert _generated_by(4, 45) == 6


def test_source_off_shift_time_is_its_own_state():
    cal = ShiftCalendar(20, [[0, 10]])
    env, _, _, src = _line(None, 4, 0.1, source_calendar=cal)
    _run(env, 40)
    src.update_final_state_time(env.now)
    assert src.stats["total_time_spent_in_states"]["OFF_SHIFT_STATE"] == 20


def test_source_without_calendar_has_no_off_shift_state():
    env, _, _, src = _line(None, 4, 0.1)
    _run(env, 10)
    assert "OFF_SHIFT_STATE" not in src.stats["total_time_spent_in_states"]
