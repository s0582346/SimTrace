"""The checks that run after every `run_simulation`.

`run_checks` runs `verify_item_flow`, `verify_extreme_conditions` and
`verify_time_course` on the model that was just run, with the run's seed. The
HTML report draws its Validation tab from these results, and `check_summary`
turns them into the short `checks` part of the run result. When the time course
finds a line stop, each no_influence hint of the extreme conditions says so.
`validate_model` and
`verify_conservation` are not part of it: the agent calls them itself.
"""

from __future__ import annotations

from simtrace.model import FactoryModel

ITEM_FLOW = "verify_item_flow"
EXTREME_CONDITIONS = "verify_extreme_conditions"
TIME_COURSE = "verify_time_course"

# Improper trails listed in the summary; the count is always given in full.
IMPROPER_SHOWN = 20


def run_checks(model: FactoryModel, until: float, seed: int | None,
               *, item_flow: bool = True) -> dict:
    """Each check's full result, or the reason it could not run as a str.

    `item_flow=False` leaves verify_item_flow out, for run_replications, which
    keeps no item trails.
    """
    # Imported here: the validation package imports the simulation package.
    from simtrace.tools.validation.extreme_conditions import verify_extreme_conditions
    from simtrace.tools.validation.item_flow import verify_item_flow
    from simtrace.tools.validation.time_course import verify_time_course

    calls = {
        ITEM_FLOW: lambda: verify_item_flow(model=model),
        EXTREME_CONDITIONS: lambda: verify_extreme_conditions(until, seed=seed, model=model),
        TIME_COURSE: lambda: verify_time_course(until, seed=seed, series=True, model=model),
    }
    if not item_flow:
        del calls[ITEM_FLOW]
    results: dict = {}
    for tool, call in calls.items():
        try:
            results[tool] = call()
        except ValueError as exc:
            results[tool] = str(exc)
    _note_line_stop(results)
    return results


def _note_line_stop(results: dict) -> None:
    """Add the line stop that verify_time_course found to each no_influence
    finding of verify_extreme_conditions."""
    course, extreme = results.get(TIME_COURSE), results.get(EXTREME_CONDITIONS)
    if not isinstance(course, dict) or not isinstance(extreme, dict):
        return
    stop = next((f for f in course["findings"] if f["check"] == "line_stop"), None)
    if stop is None:
        return
    for finding in extreme["findings"]:
        if finding["check"] == "no_influence":
            finding["message"] += (
                f" verify_time_course: line_stop from {stop['details']['from']:g} on in "
                "the run as built. A stopped line shows no station time in its output."
            )


def count(number: int, word: str) -> str:
    return f"{number} {word}" + ("" if number == 1 else "s")


def item_flow_status(result: dict) -> tuple[str, str]:
    """(status, one-line summary) of a verify_item_flow result."""
    improper, warnings = result["improper"], result["warnings"]
    status = "fail" if improper else "warn" if warnings else "pass"
    summary = f"{result['passed']} of {result['delivered']} delivered items on a wired route"
    if warnings:
        summary += f", {count(len(warnings), 'warning')}"
    return status, summary


def extreme_conditions_status(result: dict) -> tuple[str, str]:
    """(status, one-line summary) of a verify_extreme_conditions result."""
    errors = [f for f in result["findings"] if f["severity"] == "error"]
    hints = [f for f in result["findings"] if f["severity"] == "hint"]
    status = "fail" if errors else "warn" if hints else "pass"
    summary = (f"{count(len(errors), 'error')}, {count(len(hints), 'hint')}, "
               f"{count(len(result['combinations']['runs']), 'run')} at the bounds")
    return status, summary


def time_course_status(result: dict) -> tuple[str, str]:
    """(status, one-line summary) of a verify_time_course result."""
    hints = result["findings"]
    return ("warn" if hints else "pass"), count(len(hints), "hint")


def check_summary(results: dict) -> dict:
    """Per check its status, one summary line and its findings."""
    summary: dict = {}
    for tool, result in results.items():
        if isinstance(result, str):
            summary[tool] = {"status": "not checked", "summary": result, "findings": []}
        elif tool == ITEM_FLOW:
            status, line = item_flow_status(result)
            summary[tool] = {
                "status": status,
                "summary": line,
                "findings": result["warnings"],
                "improper": [
                    {"item": f["item"], "at": f["at"], "reason": f["reason"]}
                    for f in result["improper"][:IMPROPER_SHOWN]
                ],
            }
        elif tool == EXTREME_CONDITIONS:
            status, line = extreme_conditions_status(result)
            summary[tool] = {"status": status, "summary": line, "findings": result["findings"]}
        else:
            status, line = time_course_status(result)
            summary[tool] = {"status": status, "summary": line, "findings": result["findings"]}
    return summary
