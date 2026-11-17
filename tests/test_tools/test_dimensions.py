"""Tests for verify_dimensions."""

import pytest

from simtrace.model import FactoryModel
from simtrace.tools.builders import (
    create_buffer,
    create_conveyor,
    create_machine,
    create_shift_calendar,
    create_sink,
    create_source,
)
from simtrace.tools.simulation import connect
from simtrace.tools.validation import verify_dimensions
from simtrace.tools.validation.dimensions import parse_figure


def line(arrival="exp(5)", testing="uniform(2, 3)", speed=6.0, belt=6.0,
         item=0.3) -> FactoryModel:
    """Source, testing, a belt, a store. Correct values are in minutes and metres."""
    m = FactoryModel()
    create_source("src", inter_arrival_time=arrival, blocking=True, item_length=item, model=m)
    create_machine("testing", processing_delay=testing, model=m)
    create_sink("store", model=m)
    create_buffer("b_in", capacity=5, model=m)
    create_conveyor("belt", conveyor_length=belt, speed=speed, item_length=item, model=m)
    connect("b_in", "src", "testing", model=m)
    connect("belt", "testing", "store", model=m)
    return m


SHEET = [
    {"component": "src", "field": "inter_arrival_time", "sheet": "exp(12 parts/h)"},
    {"component": "src", "field": "item_length", "sheet": "300 mm"},
    {"component": "testing", "field": "processing_delay", "sheet": "uniform(120 s, 180 s)"},
    {"component": "belt", "field": "conveyor_length", "sheet": "6 m"},
    {"component": "belt", "field": "speed", "sheet": "0.1 m/s"},
    {"component": "belt", "field": "item_length", "sheet": "300 mm"},
    {"component": "b_in", "field": "capacity", "sheet": "5 parts"},
]


def by_check(result: dict) -> dict:
    found: dict = {}
    for finding in result["findings"]:
        found.setdefault(finding["check"], []).append(finding)
    return found


def test_parse_units_and_labels():
    figure = parse_figure("5 * 8 h")
    assert figure.args[0].quantity.value == pytest.approx(5 * 8 * 3600)
    rate = parse_figure("12 parts/h").args[0].quantity
    assert rate.dim == (-1, 0)
    assert rate.value == pytest.approx(12 / 3600)
    speed = parse_figure("0.1 m/s").args[0].quantity
    assert speed.dim == (-1, 1)


@pytest.mark.parametrize("text, seconds", [
    ("2 h 30 min", 9000),
    ("1 h 30 min 15 s", 5415),
    ("2 h 30 min / 2", 7200 + 900),
    ("5 * 8 h", 144000),
])
def test_parse_compound_times(text, seconds):
    quantity = parse_figure(text).args[0].quantity
    assert quantity.dim == (1, 0)
    assert quantity.value == pytest.approx(seconds)


@pytest.mark.parametrize("text, kind, values", [
    ("12 parts per hour", "constant", [12 / 3600]),
    ("3 per 8 h", "constant", [3 / (8 * 3600)]),
    ("6 m / 1 min", "constant", [6 / 60]),
    ("1 min / 6 m", "constant", [60 / 6]),
    ("exp(3 failures per 8 h)", "exp", [3 / (8 * 3600)]),
    ("5 Min", "constant", [300]),
    ("2 Hours", "constant", [7200]),
    ("Uniform(3 min, 4 min)", "uniform", [180, 240]),
    ("EXP(5 min)", "exp", [300]),
    ("Gauss(8 min, 3 min)", "normal", [480, 180]),
    ("2 m·s⁻¹", "constant", [2]),
])
def test_parse_sheet_spellings(text, kind, values):
    figure = parse_figure(text)
    assert figure.kind == kind
    assert [a.quantity.value for a in figure.args] == pytest.approx(values)


def test_superscript_power():
    assert parse_figure("1 m/s²").args[0].quantity.dim == (-2, 1)


def test_compound_length():
    assert parse_figure("1 m 20 cm").args[0].quantity.value == pytest.approx(1.2)


def test_parse_rejects_a_number_without_unit_in_a_compound():
    with pytest.raises(ValueError, match="sum"):
        parse_figure("2 h 30")


def test_parse_rejects_a_mixed_sum():
    with pytest.raises(ValueError, match="sum"):
        parse_figure("2 m + 3 s")


def test_fitting_dimensions_pass_with_rate_info():
    result = verify_dimensions(SHEET, model=line())
    assert result["passed"], result["findings"]
    assert [f["check"] for f in result["findings"]] == ["needs_inverting"]
    assert result["checked"][0]["status"] == "info"


@pytest.mark.parametrize("arrival", ["exp(5)", "exp(12)", "exp(300)"])
def test_values_are_not_compared(arrival):
    assert verify_dimensions(SHEET, model=line(arrival=arrival))["passed"]


def test_rate_range_is_not_inverted():
    sheet = [{"component": "src", "field": "inter_arrival_time",
              "sheet": "uniform(15 /h, 20 /h)"}]
    result = verify_dimensions(sheet, model=line(arrival="uniform(3, 4)"))
    assert "not_invertible" in by_check(result)


def test_exp_rate_only_in_arrival_failure_and_repair():
    sheet = [{"component": "testing", "field": "processing_delay", "sheet": "exp(24 /h)"}]
    result = verify_dimensions(sheet, model=line(testing="exp(2.5)"))
    assert "not_invertible" in by_check(result)


def test_constant_rate_fits_any_time_field():
    sheet = [entry for entry in SHEET if entry["component"] != "testing"]
    sheet.append({"component": "testing", "field": "processing_delay", "sheet": "24 parts/h"})
    result = verify_dimensions(sheet, model=line(testing=2.5))
    assert result["passed"], result["findings"]
    assert "needs_inverting" in by_check(result)


def test_a_rate_of_zero_is_rejected():
    sheet = [{"component": "src", "field": "inter_arrival_time", "sheet": "0 /h"}]
    assert "invalid_figure" in by_check(verify_dimensions(sheet, model=line()))


@pytest.mark.parametrize("component, field, sheet", [
    ("testing", "processing_delay", "3 m"),
    ("belt", "speed", "6 m"),
    ("belt", "conveyor_length", "6 m/s"),
    ("b_in", "capacity", "5 m"),
    ("testing", "processing_delay", "3"),
])
def test_wrong_dimension_is_incompatible(component, field, sheet):
    figures = [{"component": component, "field": field, "sheet": sheet}]
    [finding] = by_check(verify_dimensions(figures, model=line()))["incompatible_dimension"]
    assert finding["component"] == component


def test_a_mixed_sum_is_an_invalid_figure():
    sheet = [{"component": "testing", "field": "processing_delay", "sheet": "2 min + 3 m"}]
    assert "invalid_figure" in by_check(verify_dimensions(sheet, model=line()))


def test_mixed_dimensions_in_a_distribution_are_rejected():
    sheet = [{"component": "testing", "field": "processing_delay",
              "sheet": "uniform(2 min, 3 m)"}]
    assert "invalid_figure" in by_check(verify_dimensions(sheet, model=line()))


def test_a_field_without_model_value_is_rejected():
    sheet = [{"component": "testing", "field": "time_to_failure", "sheet": "exp(8 h)"}]
    [finding] = by_check(verify_dimensions(sheet, model=line()))["invalid_figure"]
    assert "no value" in finding["message"]


def test_unknown_component_and_field_name_the_closest():
    sheet = [{"component": "bellt", "field": "speed", "sheet": "6 m/min"},
             {"component": "belt", "field": "sped", "sheet": "6 m/min"}]
    result = verify_dimensions(sheet, model=line())
    unknown = by_check(result)["unknown_entry"]
    assert "Closest: belt." in unknown[0]["message"]
    assert "Closest: speed." in unknown[1]["message"]


def test_undeclared_time_field_is_an_error():
    result = verify_dimensions(SHEET[1:], model=line())
    [finding] = by_check(result)["undeclared"]
    assert (finding["component"], finding["severity"]) == ("src", "error")
    assert not result["passed"]


def test_calendar_windows():
    m = line()
    create_shift_calendar("shifts", cycle=1440, windows=[[0, 480]], model=m)
    sheet = SHEET + [{"component": "shifts", "field": "cycle", "sheet": "24 h"},
                     {"component": "shifts", "field": "windows", "sheet": "[[0, 8 h]]"}]
    result = verify_dimensions(sheet, model=m)
    assert result["passed"], result["findings"]
    wrong = [{"component": "shifts", "field": "windows", "sheet": "[[0 h, 8 m]]"}]
    assert "incompatible_dimension" in by_check(verify_dimensions(wrong, model=m))


def test_rejects_bad_arguments_and_empty_model():
    with pytest.raises(ValueError, match="list"):
        verify_dimensions("exp(5 min)", model=line())
    with pytest.raises(ValueError, match="empty"):
        verify_dimensions(SHEET, model=FactoryModel())
