"""Tests for the HTML run report of run_simulation and run_replications."""

import contextlib
import io

from simtrace.model import FactoryModel
from simtrace.tools.builders import (
    create_buffer,
    create_conveyor,
    create_machine,
    create_shift_calendar,
    create_sink,
    create_source,
)
from simtrace.tools.simulation import connect, run_replications, run_simulation
from simtrace.tools.simulation.report import report_view


def _line(calendar=False, part_type=None):
    """src -> b_in -> m -> b_mid -> join (0 min) -> b_out -> snk."""
    model = FactoryModel()
    if calendar:
        create_shift_calendar("day", 100, [[0, 60]], default=True, model=model)
    create_source("src", inter_arrival_time="exp(2)", blocking=True,
                  part_type=part_type, model=model)
    create_machine("m", processing_delay="uniform(1, 2)", time_to_failure="exp(50)",
                   repair_time=5, model=model)
    create_machine("join", processing_delay=0, model=model)
    create_sink("snk", model=model)
    for edge_id, capacity in (("b_in", 4), ("b_mid", 1), ("b_out", 1)):
        create_buffer(edge_id, capacity=capacity, model=model)
    connect("b_in", "src", "m", model=model)
    connect("b_mid", "m", "join", model=model)
    connect("b_out", "join", "snk", model=model)
    return model


def _quiet(call, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return call(*args, **kwargs)


def test_run_simulation_writes_report(tmp_path):
    target = tmp_path / "out" / "run.html"
    result = _quiet(run_simulation, 200, seed=1, model=_line(),
                    report_path=str(target))
    assert result["report"] == str(target)
    html = target.read_text(encoding="utf-8")
    for heading in ("<h1>Single-Run Report</h1>", "Output", "Stations", "Buffers", "header class='brand'"):
        assert heading in html
    assert "Sources</h2>" not in html
    assert "class='mermaid'" in html
    assert "<li><b>One run</b></li><li>Seed <b>1</b></li><li>Run length <b>200</b> min</li></ul>" in html
    assert "nodes" not in html.split("<pre")[0]
    assert "95% interval" not in html


def test_no_report_without_path():
    result = _quiet(run_simulation, 50, seed=1, model=_line())
    assert "report" not in result


def test_run_replications_writes_report(tmp_path):
    target = tmp_path / "batch.html"
    result = _quiet(run_replications, 200, 3, model=_line(), n_jobs=1,
                    report_path=str(target))
    assert result["report"] == str(target)
    html = target.read_text(encoding="utf-8")
    assert "<h1>Replication Report</h1>" in html
    assert "<li><b>3</b> of <b>3</b> runs</li><li>Seed base <b>0</b></li>" in html
    assert "<li>Numbers: mean ± half-width of the 95% interval</li>" in html
    assert "95% interval" in html
    assert " ± " in html


def _timed_line(edge="buffer"):
    """src (every 10) -> b1 (delay 2) or a conveyor -> m (3) -> b2 -> snk."""
    model = FactoryModel()
    create_source("src", inter_arrival_time=10, blocking=True, model=model)
    create_machine("m", processing_delay=3, model=model)
    create_sink("snk", model=model)
    if edge == "buffer":
        create_buffer("b1", capacity=1, delay=2, model=model)
    else:
        create_conveyor("b1", conveyor_length=4, speed=1, item_length=1, model=model)
    create_buffer("b2", capacity=1, model=model)
    connect("b1", "src", "m", model=model)
    connect("b2", "m", "snk", model=model)
    return model


def _output_row(html):
    return html[html.index("Output</h2>"):].split("</table>")[0]


def test_output_splits_the_throughput_time(tmp_path):
    target = tmp_path / "run.html"
    result = _quiet(run_simulation, 100, seed=1, model=_timed_line(),
                    report_path=str(target))
    received = result["nodes"]["snk"]["num_item_received"]
    row = _output_row(target.read_text(encoding="utf-8"))
    # 2 in the buffer and 3 on the machine: 5 per part, 60% production.
    assert f"<td class='num'>{received}</td>" in row
    assert "<td class='num'>5</td>" in row
    assert "<td class='num'>60.0%</td><td class='num'>0.0%</td><td class='num'>40.0%</td>" in row


def test_output_counts_conveyors_as_transport(tmp_path):
    target = tmp_path / "run.html"
    _quiet(run_simulation, 100, seed=1, model=_timed_line("conveyor"),
           report_path=str(target))
    cells = _output_row(target.read_text(encoding="utf-8")).split("<td class='num'>")
    transport = float(cells[5].split("%")[0])
    assert transport > 0


def test_source_is_first_station_row(tmp_path):
    target = tmp_path / "run.html"
    _quiet(run_simulation, 200, seed=1, model=_line(), report_path=str(target))
    html = target.read_text(encoding="utf-8")
    stations = html[html.index("Stations</h2>"):]
    assert stations.index("<code>src</code>") < stations.index("<code>m</code>")
    assert "source, processed = generated" in stations


def test_report_does_not_change_replication_result():
    plain = _quiet(run_replications, 200, 3, model=_line(), n_jobs=1)
    assert "report" not in plain
    assert "view" not in str(plain["analysis"]["_individual_replications"][0].keys())


def test_helper_and_breakdowns_are_shown(tmp_path):
    target = tmp_path / "run.html"
    _quiet(run_simulation, 200, seed=1, model=_line(), report_path=str(target))
    html = target.read_text(encoding="utf-8")
    assert "helper, takes no time" in html
    assert "Breakdowns" in html
    assert "Down" in html


def test_off_shift_column_only_with_calendar(tmp_path):
    plain = tmp_path / "plain.html"
    shifts = tmp_path / "shifts.html"
    _quiet(run_simulation, 200, seed=1, model=_line(), report_path=str(plain))
    _quiet(run_simulation, 200, seed=1, model=_line(calendar=True),
           report_path=str(shifts))
    assert "Off shift" not in plain.read_text(encoding="utf-8")
    shifts_html = shifts.read_text(encoding="utf-8")
    assert "Off shift" in shifts_html
    assert "Calendars" in shifts_html


def test_part_type_table(tmp_path):
    target = tmp_path / "run.html"
    _quiet(run_simulation, 200, seed=1, model=_line(part_type="shaft"),
           report_path=str(target))
    html = target.read_text(encoding="utf-8")
    assert "Part types" in html
    assert "<code>shaft</code>" in html


def test_run_simulation_runs_no_checks_by_default():
    result = _quiet(run_simulation, 100, seed=1, model=_line())
    assert "checks" not in result


def test_run_simulation_with_checks_returns_each_check_once():
    result = _quiet(run_simulation, 100, seed=1, model=_line(), checks=True)
    checks = result["checks"]
    assert list(checks) == ["verify_item_flow", "verify_extreme_conditions",
                            "verify_time_course"]
    assert all(c["status"] in ("pass", "warn", "fail") for c in checks.values())
    assert checks["verify_item_flow"]["improper"] == []


def test_run_simulation_with_checks_draws_a_seed_and_shares_it():
    from simtrace.tools.simulation.checks import TIME_COURSE

    seen = {}
    from simtrace.tools.simulation import lifecycle
    real = lifecycle.run_checks

    def spy(model, until, seed, **kwargs):
        seen["seed"] = seed
        return real(model, until, seed, **kwargs)

    lifecycle.run_checks = spy
    try:
        result = _quiet(run_simulation, 100, model=_line(), checks=True)
    finally:
        lifecycle.run_checks = real
    assert isinstance(result["seed"], int)
    assert seen["seed"] == result["seed"]
    assert TIME_COURSE in result["checks"]


def test_a_line_stop_is_added_to_no_influence_hints():
    from simtrace.tools.simulation.checks import (
        EXTREME_CONDITIONS, TIME_COURSE, _note_line_stop,
    )

    results = {
        TIME_COURSE: {"findings": [{"check": "line_stop", "details": {"from": 909.6}}]},
        EXTREME_CONDITIONS: {"findings": [
            {"check": "no_influence", "message": "'m' changes nothing."},
            {"check": "zero_possible", "message": "'m' can take 0."},
        ]},
    }
    _note_line_stop(results)
    no_influence, zero = results[EXTREME_CONDITIONS]["findings"]
    assert "line_stop from 909.6 on" in no_influence["message"]
    assert zero["message"] == "'m' can take 0."


def test_report_view_keeps_numbers_and_states_only():
    result = _quiet(run_simulation, 100, seed=1, model=_line())
    view = report_view(result)
    machine = view["nodes"]["m"]
    assert "states" in machine
    assert "processing_delay" not in machine
    assert all(
        isinstance(value, (int, float))
        for key, value in machine.items() if key != "states"
    )


def test_headers_carry_units(tmp_path):
    target = tmp_path / "run.html"
    _quiet(run_simulation, 200, seed=1, model=_line(), report_path=str(target))
    html = target.read_text(encoding="utf-8")
    for header in ("Throughput [parts]", "Per hour [parts/h]", "Mean throughput time [min]",
                   "Processed [parts]", "Working [%]", "Breakdowns [count]",
                   "Average fill [%]"):
        assert f">{header}</th>" in html


def test_report_has_three_tabs(tmp_path):
    target = tmp_path / "run.html"
    _quiet(run_simulation, 100, seed=1, model=_line(), report_path=str(target))
    html = target.read_text(encoding="utf-8")
    assert "<button type='button' data-tab='tab-0' class=active>Model</button>" in html
    assert ">Output</button>" in html and ">Validation</button>" in html
    model_tab = html[html.index("id='tab-0'"):html.index("id='tab-1'")]
    assert "class='mermaid'" in model_tab and "Technical definition" in model_tab
    output_tab = html[html.index("id='tab-1'"):html.index("id='tab-2'")]
    assert "Output</h2>" in output_tab and "Stations</h2>" in output_tab


def _validation_tab(html):
    return html[html.index("id='tab-2'"):]


def test_validation_tab_shows_validate_model_summary(tmp_path):
    target = tmp_path / "run.html"
    _quiet(run_simulation, 100, seed=1, model=_line(), report_path=str(target))
    tab = _validation_tab(target.read_text(encoding="utf-8"))
    assert ("<details class='tool'><summary><code>validate_model</code>"
            "<span class='status pass'>0 errors, 0 warnings</span></summary>") in tab
    assert "<p>No findings.</p>" in tab
    assert "<details class='findings" not in tab.split("verify_item_flow")[0]


def test_validation_tab_lists_warnings_in_a_toggle(tmp_path):
    model = _line()
    create_shift_calendar("spare", 100, [[0, 50]], model=model)
    target = tmp_path / "run.html"
    _quiet(run_simulation, 100, seed=1, model=model, report_path=str(target))
    tab = _validation_tab(target.read_text(encoding="utf-8"))
    assert "<span class='status warn'>0 errors, 1 warning</span>" in tab
    assert "<summary>Warnings (1)</summary>" in tab
    assert "<code>unused_calendar</code> at <b><code>spare</code></b>" in tab
    assert "Errors (" not in tab


def test_overload_warning_is_a_table(tmp_path):
    model = FactoryModel()
    create_source("src", inter_arrival_time=3, blocking=True, model=model)
    create_machine("m", processing_delay=5, model=model)
    create_sink("snk", model=model)
    create_buffer("b_in", capacity=4, model=model)
    create_buffer("b_out", capacity=1, model=model)
    connect("b_in", "src", "m", model=model)
    connect("b_out", "m", "snk", model=model)
    target = tmp_path / "run.html"
    _quiet(run_simulation, 50, seed=1, model=model, report_path=str(target))
    tab = _validation_tab(target.read_text(encoding="utf-8"))
    assert "<th>Machine</th><th class='num'>Effective process time [min]</th>" in tab
    assert "<td><code>m</code></td><td class='num'>5.00</td><td class='num'>3.00</td><td class='num'>167%</td>" in tab
    validate_model_part = tab.split("<code>verify_item_flow</code>")[0]
    assert "<li>" not in validate_model_part


def test_validation_tab_has_item_flow_toggle(tmp_path):
    target = tmp_path / "run.html"
    _quiet(run_simulation, 100, seed=1, model=_line(part_type="shaft"),
           report_path=str(target))
    tab = _validation_tab(target.read_text(encoding="utf-8"))
    flow = tab[tab.index("<code>verify_item_flow</code>"):]
    assert "<span class='status pass'>" in flow and "delivered items on a wired route" in flow
    assert "<h3>Routes</h3>" in flow
    assert "<td>shaft</td><td>src → m → join → snk</td><td>good</td>" in flow
    assert "<td class='num'>100%</td>" in flow


def test_validation_tab_has_extreme_conditions_toggle(tmp_path):
    target = tmp_path / "run.html"
    _quiet(run_simulation, 100, seed=1, model=_line(), report_path=str(target))
    tab = _validation_tab(target.read_text(encoding="utf-8"))
    extreme = tab[tab.index("<code>verify_extreme_conditions</code>"):]
    assert "runs at the bounds</span>" in extreme
    assert "<h3>Every run on one axis</h3>" in extreme
    for name in ("Delivered", "WIP", "Blocked"):
        assert f">{name}</button>" in extreme
    for group in ("Stations at their bounds", "Extreme inputs", "Targeted extremes"):
        assert group in extreme
    assert "<h3>Parts delivered per combination</h3>" in extreme
    assert "<table class='bounds'>" in extreme
    assert "limit:" not in extreme and " effect</th>" not in extreme


def _shares(**busy):
    """A run in which each named station worked for its share of the run."""
    return {"shares": {s: {"working": w, "waiting": 1 - w, "blocked": 0.0, "down": 0.0}
                       for s, w in busy.items()},
            "sources_blocked": {"src": 5.0}}


def test_the_busiest_station_limits_the_line():
    from simtrace.tools.simulation.report import _limit

    assert _limit([_shares(sanding=0.14, painting=0.99)], saturated=True) == "painting"


def test_stations_equally_busy_limit_the_line_together():
    from simtrace.tools.simulation.report import _limit

    assert _limit([_shares(mill1=0.95, mill2=0.94, turning=0.6)], True) == "mill1 + mill2"


def test_breakdown_time_counts_as_busy():
    from simtrace.tools.simulation.report import _limit

    run = _shares(lathe=0.60, mill=0.75)
    run["shares"]["lathe"]["down"] = 0.30

    assert _limit([run], True) == "lathe"


def test_a_line_whose_sources_never_wait_is_limited_by_its_arrivals():
    from simtrace.tools.simulation.report import _limit

    run = {**_shares(painting=0.7), "sources_blocked": {"src": 0.0}}

    assert _limit([run], saturated=False) == "arrivals"
    assert _limit([run], saturated=True) == "painting"


def test_replication_report_says_item_flow_was_not_checked(tmp_path):
    target = tmp_path / "reps.html"
    _quiet(run_replications, 50, 2, model=_line(), n_jobs=1,
           report_path=str(target))
    tab = _validation_tab(target.read_text(encoding="utf-8"))
    assert "<code>verify_item_flow</code><span class='status warn'>not checked</span>" in tab
