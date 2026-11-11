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


def test_correct_model_passes_with_rate_info():
    result = verify_dimensions("min", SHEET, model=line())
    assert result["passed"], result["findings"]
    assert [f["check"] for f in result["findings"]] == ["converted_from_rate"]


@pytest.mark.parametrize("arrival, cause", [
    ("exp(12)", "sheet figure copied without inverting or converting"),
    ("exp(0.2)", "rate entered instead of the mean"),
    ("exp(0.0833)", "inverted but not converted"),
    ("exp(300)", "value in s instead of min"),
])
def test_rate_slips_name_their_cause(arrival, cause):
    result = verify_dimensions("min", SHEET, model=line(arrival=arrival))
    [finding] = by_check(result)["wrong_dimension"]
    assert finding["component"] == "src"
    assert finding["details"]["likely_cause"].startswith(cause)


def test_seconds_copied_without_converting():
    result = verify_dimensions("min", SHEET, model=line(testing="uniform(120, 180)"))
    [finding] = by_check(result)["unit_mismatch"]
    assert finding["details"]["likely_cause"] == "sheet figure copied without converting"
    assert "expected uniform(2, 3) min" in finding["message"]


def test_belt_speed_in_metres_per_second():
    result = verify_dimensions("min", SHEET, model=line(speed=0.1))
    [finding] = by_check(result)["unit_mismatch"]
    assert finding["component"] == "belt"
    assert "Travel time 1 min expected, 60 in the model" in finding["message"]


def test_rate_range_is_not_inverted():
    sheet = [{"component": "src", "field": "inter_arrival_time",
              "sheet": "uniform(15 /h, 20 /h)"}]
    result = verify_dimensions("min", sheet, model=line(arrival="uniform(3, 4)"))
    assert "not_invertible" in by_check(result)


def test_exp_rate_only_in_arrival_failure_and_repair():
    sheet = [{"component": "testing", "field": "processing_delay", "sheet": "exp(24 /h)"}]
    result = verify_dimensions("min", sheet, model=line(testing="exp(2.5)"))
    assert "not_invertible" in by_check(result)


def test_constant_rate_inverts_in_any_time_field():
    sheet = [{"component": "testing", "field": "processing_delay", "sheet": "24 parts/h"}]
    result = verify_dimensions("min", sheet, model=line(testing=2.5))
    assert result["passed"]
    assert "converted_from_rate" in by_check(result)


def test_length_for_a_time_field_is_rejected():
    sheet = [{"component": "testing", "field": "processing_delay", "sheet": "3 m"}]
    result = verify_dimensions("min", sheet, model=line())
    assert "incompatible_dimension" in by_check(result)


def test_unknown_component_and_field_name_the_closest():
    sheet = [{"component": "bellt", "field": "speed", "sheet": "6 m/min"},
             {"component": "belt", "field": "sped", "sheet": "6 m/min"}]
    result = verify_dimensions("min", sheet, model=line())
    unknown = by_check(result)["unknown_entry"]
    assert "Closest: belt." in unknown[0]["message"]
    assert "Closest: speed." in unknown[1]["message"]


def test_undeclared_time_field_is_a_hint():
    result = verify_dimensions("min", SHEET[1:], model=line())
    [hint] = by_check(result)["undeclared"]
    assert (hint["component"], hint["severity"]) == ("src", "hint")


def test_calendar_windows_in_hours():
    m = line()
    create_shift_calendar("shifts", cycle=1440, windows=[[0, 480]], model=m)
    sheet = [{"component": "shifts", "field": "cycle", "sheet": "24 h"},
             {"component": "shifts", "field": "windows", "sheet": "[[0 h, 8 h]]"}]
    assert verify_dimensions("min", sheet, model=m)["passed"]

    wrong = line()
    create_shift_calendar("shifts", cycle=24, windows=[[0, 8]], model=wrong)
    found = by_check(verify_dimensions("min", sheet, model=wrong))["unit_mismatch"]
    assert {f["details"]["likely_cause"] for f in found} == {
        "sheet figure copied without converting"
    }


def test_a_bare_zero_takes_the_field_unit():
    m = line(testing="uniform(0, 3)")
    create_shift_calendar("shifts", cycle=1440, windows=[[0, 480]], model=m)
    sheet = [{"component": "testing", "field": "processing_delay",
              "sheet": "uniform(0, 180 s)"},
             {"component": "src", "field": "node_setup_time", "sheet": "0"},
             {"component": "shifts", "field": "windows", "sheet": "[[0, 8 h]]"}]
    result = verify_dimensions("min", sheet, model=m)
    assert result["passed"], result["findings"]


def test_an_info_in_a_list_is_not_an_error():
    m = line()
    create_shift_calendar("shifts", cycle=1440, windows=[[0, 480]], model=m)
    sheet = [{"component": "shifts", "field": "windows", "sheet": "[[0 h, 0.125 /h]]"}]
    result = verify_dimensions("min", sheet, model=m)
    assert result["passed"]
    assert result["checked"][0]["status"] == "info"
    assert "converted_from_rate" in by_check(result)


def test_a_wrong_count_is_a_value_mismatch():
    sheet = [{"component": "b_in", "field": "capacity", "sheet": "3 places"}]
    result = verify_dimensions("min", sheet, model=line())
    [finding] = by_check(result)["value_mismatch"]
    assert "unit_mismatch" not in by_check(result)
    assert "Likely cause" not in finding["message"]


@pytest.mark.parametrize("testing, check, cause", [
    (3.5, None, None),
    (210, "unit_mismatch", "value in s instead of min"),
])
def test_uniform_on_the_sheet_against_a_constant_compares_means(testing, check, cause):
    sheet = [{"component": "testing", "field": "processing_delay",
              "sheet": "uniform(3 min, 4 min)"}]
    result = verify_dimensions("min", sheet, model=line(testing=testing))
    if check is None:
        assert result["passed"], result["findings"]
    else:
        [finding] = by_check(result)[check]
        assert finding["details"]["likely_cause"] == cause


def test_a_bare_number_other_than_zero_is_still_rejected():
    sheet = [{"component": "testing", "field": "processing_delay", "sheet": "3"}]
    result = verify_dimensions("min", sheet, model=line())
    assert "incompatible_dimension" in by_check(result)


def test_rejects_bad_units_and_empty_model():
    with pytest.raises(ValueError, match="time_unit"):
        verify_dimensions("minutes", SHEET, model=line())
    with pytest.raises(ValueError, match="empty"):
        verify_dimensions("min", SHEET, model=FactoryModel())
