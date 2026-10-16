"""Shift calendars: working windows that machines and sources follow.

`create_shift_calendar(id, cycle, windows)` registers a FactorySimPy
`ShiftCalendar` under an id. `create_machine(calendar=<id>)` and
`create_source(calendar=<id>)` look it up with `resolve_calendar`, so several
stations can share one calendar.
"""

from __future__ import annotations

from factorysimpy.utils.shift_calendar import ShiftCalendar

from simtrace.model import FactoryModel, get_model


def create_shift_calendar(
    id: str,
    cycle: float,
    windows: list[list[float]],
    *,
    model: FactoryModel | None = None,
) -> dict:
    """Create a shift calendar and register it in the shared model.

    The windows repeat every `cycle` time units, starting at time 0. Outside
    the windows a station that uses the calendar is off.
    Args:
        id: unique calendar identifier.
        cycle: length of one cycle in model time units, e.g. 1440 for a day
            or 10080 for a week when the model counts in minutes. Must be > 0.
        windows: working windows inside one cycle as [start, end] pairs, with
            0 <= start < end <= cycle, in ascending order and not overlapping.
            A window includes its start and excludes its end. Windows that
            touch are joined into one.

    Returns a summary dict with the joined windows.
    """
    model = model if model is not None else get_model()

    if not isinstance(id, str) or not id:
        raise ValueError("id must be a non-empty string.")
    if id in model.calendars:
        raise ValueError(f"A shift calendar with id '{id}' already exists.")
    if not isinstance(windows, (list, tuple)):
        raise ValueError("windows must be a list of [start, end] pairs.")
    for window in windows:
        if not isinstance(window, (list, tuple)):
            raise ValueError(f"each window must be a [start, end] pair (got {window!r}).")

    calendar = ShiftCalendar(cycle, windows)

    model.calendars[id] = calendar
    model.record(
        "create_shift_calendar",
        # Copy so a later mutation of the caller's lists can't rewrite history.
        {"id": id, "cycle": cycle, "windows": [list(window) for window in windows]},
    )

    return {
        "id": id,
        "type": "ShiftCalendar",
        "cycle": cycle,
        "windows": [list(window) for window in calendar.windows],
    }


def resolve_calendar(model: FactoryModel, calendar: str | None) -> object | None:
    """Return the ShiftCalendar registered under `calendar`, or None for None."""
    if calendar is None:
        return None
    if not isinstance(calendar, str) or not calendar:
        raise ValueError("calendar must be the id of a shift calendar.")
    if calendar not in model.calendars:
        raise ValueError(
            f"Shift calendar '{calendar}' does not exist. Create it with "
            "create_shift_calendar first."
        )
    return model.calendars[calendar]
