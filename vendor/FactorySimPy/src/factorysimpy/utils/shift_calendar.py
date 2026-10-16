import math


class ShiftCalendar:
    """
    Working windows that repeat every `cycle` time units.

    Parameters:
        cycle (int, float): Length of one cycle, e.g. 1440 for a day in minutes or 10080 for a week.
        windows (list of [start, end]): Working windows inside one cycle, 0 <= start < end <= cycle,
            in ascending order and not overlapping. Outside the windows a station is off.
            Windows that touch are joined into one.

    Time 0 of the simulation is the start of the first cycle. A window includes its start and
    excludes its end.
    """

    def __init__(self, cycle, windows):
        if isinstance(cycle, bool) or not isinstance(cycle, (int, float)) or not cycle > 0:
            raise ValueError(f"cycle must be a number > 0 (got {cycle!r}).")
        if not windows:
            raise ValueError("windows must hold at least one [start, end] pair.")
        joined = []
        for window in windows:
            if len(window) != 2:
                raise ValueError(f"each window must be a [start, end] pair (got {window!r}).")
            start, end = window
            for value in (start, end):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"window bounds must be numbers (got {window!r}).")
            if not 0 <= start < end <= cycle:
                raise ValueError(
                    f"each window needs 0 <= start < end <= cycle (got {window!r}, cycle {cycle})."
                )
            if joined and start < joined[-1][1]:
                raise ValueError(
                    f"windows must be in ascending order and not overlap (got {window!r} after {joined[-1]!r})."
                )
            if joined and start == joined[-1][1]:
                joined[-1][1] = end
            else:
                joined.append([start, end])
        self.cycle = cycle
        self.windows = joined
        self.always = len(joined) == 1 and joined[0] == [0, cycle]

    def is_working(self, t):
        """True if `t` lies inside a working window."""
        if self.always:
            return True
        position = t - math.floor(t / self.cycle) * self.cycle
        return any(start <= position < end for start, end in self.windows)

    def next_change(self, t):
        """First time after `t` at which `is_working` changes, or None if it never changes."""
        if self.always:
            return None
        working = self.is_working(t)
        base = math.floor(t / self.cycle) * self.cycle
        # Two cycles always hold the next change, a window can run past the cycle end.
        for k in range(3):
            for start, end in self.windows:
                for bound in (start, end):
                    moment = base + k * self.cycle + bound
                    if moment > t and self.is_working(moment) != working:
                        return moment
        return None

    def add_working_time(self, t, duration):
        """Time at which `duration` units of working time, counted from `t`, are used up."""
        while True:
            if not self.is_working(t):
                t = self.next_change(t)
            end = self.next_change(t)
            if end is None or duration <= end - t:
                return t + duration
            duration -= end - t
            t = end

    def working_time_between(self, a, b):
        """Working time inside the interval from `a` to `b`."""
        total = 0.0
        t = a
        while t < b:
            change = self.next_change(t)
            stop = b if change is None else min(change, b)
            if self.is_working(t):
                total += stop - t
            t = stop
        return total
