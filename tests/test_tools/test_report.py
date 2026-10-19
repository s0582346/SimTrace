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
