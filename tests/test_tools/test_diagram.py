"""Tests for render_model_diagram."""

import pytest

from simtrace.model import FactoryModel
from simtrace.tools.builders import (
    create_buffer,
    create_combiner,
    create_machine,
    create_shift_calendar,
    create_sink,
    create_source,
    create_splitter,
)
from simtrace.tools.simulation import connect, render_model_diagram
from simtrace.tools.simulation.diagram import mermaid_source


def _routed_line():
    """Two sources -> m (routes by family) -> good / scrap."""
    model = FactoryModel()
    create_shift_calendar("day", 1440, [[360, 1320]], default=True, model=model)
    create_source("src_a", inter_arrival_time="exp(5)", part_type="a",
                  attributes={"family": "A"}, model=model)
    create_source("src_b", inter_arrival_time=7, part_type="b",
                  attributes={"family": "B"}, model=model)
    create_machine("m", processing_delay="uniform(2, 3)", route_by="family",
                   routes={"A": "b_good", "*": "b_scrap"}, model=model)
    create_sink("good", model=model)
    create_sink("bin", scrap=True, model=model)
    for edge_id in ("b_a", "b_b", "b_good", "b_scrap"):
        create_buffer(edge_id, capacity=3, model=model)
    connect("b_a", "src_a", "m", model=model)
    connect("b_b", "src_b", "m", model=model)
    connect("b_good", "m", "good", model=model)
    connect("b_scrap", "m", "bin", model=model)
    return model


def test_writes_page_and_reports_counts(tmp_path):
    target = tmp_path / "sub" / "model.html"
    result = render_model_diagram(str(target), model=_routed_line())
    assert result["path"] == str(target)
    assert result["nodes"] == 5
    assert result["edges"] == 4
    page = target.read_text(encoding="utf-8")
    assert "<pre class='mermaid'>" in page
    assert "mermaid@10.9.1" in page
    assert "<h3>Calendars</h3>" in page


def test_nodes_get_shapes_and_settings():
    source = mermaid_source(_routed_line())
    assert '(["<b>src_a</b><br/>source<br/>inter_arrival_time: exp(5)' in source
    assert "part_type: a<br/>attributes: family=A" in source
    assert '{{"<b>m</b><br/>machine<br/>processing_delay: uniform(2, 3)' in source
    assert "sink · scrap" in source
    assert "calendar: day (default)" in source


def test_defaults_are_left_out():
    source = mermaid_source(_routed_line())
    assert "in_edge_selection" not in source
    assert "node_setup_time" not in source
    assert "mode: FIFO" not in source


def test_edges_show_routes_and_priority():
    source = mermaid_source(_routed_line())
    assert "family=A" in source.split("b_good</b>")[1].splitlines()[0]
    assert "other" in source.split("b_scrap</b>")[1].splitlines()[0]
    assert "priority 1" in source.split("b_a</b>")[1].splitlines()[0]
    assert "priority 2" in source.split("b_b</b>")[1].splitlines()[0]


def test_route_shares_become_percentages():
    model = FactoryModel()
    create_source("src", inter_arrival_time=1, model=model)
    create_machine("m", processing_delay=1,
                   route_shares={"b_ok": 0.95, "b_bad": 0.05}, model=model)
    create_sink("ok", model=model)
    create_sink("bad", scrap=True, model=model)
    for edge_id in ("b_in", "b_ok", "b_bad"):
        create_buffer(edge_id, model=model)
    connect("b_in", "src", "m", model=model)
    connect("b_ok", "m", "ok", model=model)
    connect("b_bad", "m", "bad", model=model)
    source = mermaid_source(model)
    assert "95%" in source
    assert "5%" in source


def test_splitter_and_combiner_shapes():
    model = FactoryModel()
    create_source("src", inter_arrival_time=1, flow_item_type="pallet", model=model)
    create_splitter("split", processing_delay=1, model=model)
    create_combiner("join", target_quantity_of_each_item=[1, 1], model=model)
    create_sink("snk", model=model)
    for edge_id in ("b0", "b1", "b2", "b3"):
        create_buffer(edge_id, model=model)
    connect("b0", "src", "split", model=model)
    connect("b1", "split", "join", model=model)
    connect("b2", "split", "join", model=model)
    connect("b3", "join", "snk", model=model)
    source = mermaid_source(model)
    assert '[/"<b>split</b>' in source
    assert '[\\"<b>join</b>' in source
    # A combiner's in-edges are not a priority order.
    assert "priority" not in source


def test_quotes_and_html_are_escaped(tmp_path):
    model = FactoryModel()
    create_source("src", inter_arrival_time=1, part_type="t",
                  attributes={"note": 'a "b" <c>'}, model=model)
    create_sink("snk", model=model)
    create_buffer("b", model=model)
    connect("b", "src", "snk", model=model)
    assert "#quot;b#quot;" in mermaid_source(model)
    page = (tmp_path / "m.html")
    render_model_diagram(str(page), model=model)
    assert "&lt;c&gt;" in page.read_text(encoding="utf-8")


def test_unconnected_edges_are_listed(tmp_path):
    model = FactoryModel()
    create_source("src", inter_arrival_time=1, model=model)
    create_buffer("loose", model=model)
    target = tmp_path / "m.html"
    render_model_diagram(str(target), model=model)
    page = target.read_text(encoding="utf-8")
    assert "<h3>Not connected</h3>" in page
    assert "<code>loose</code>" in page


def test_empty_model_rejected(tmp_path):
    with pytest.raises(ValueError, match="empty"):
        render_model_diagram(str(tmp_path / "m.html"), model=FactoryModel())


def test_day_calendar_as_clock_times():
    from simtrace.tools.simulation.diagram import _calendar_plan
    windows = [[360, 540], [555, 720], [750, 1020], [1035, 1200], [1230, 1320]]
    assert _calendar_plan(1440, windows) == (
        "06:00–22:00", "09:00–09:15, 12:00–12:30, 17:00–17:15, 20:00–20:30"
    )
    assert _calendar_plan(1440, [[360, 1320]]) == ("06:00–22:00", "")


def test_night_shift_over_midnight():
    from simtrace.tools.simulation.diagram import _calendar_plan
    assert _calendar_plan(1440, [[0, 360], [1320, 1440]]) == ("22:00–06:00", "")


def test_week_calendar_groups_equal_days():
    from simtrace.tools.simulation.diagram import _calendar_plan
    day = [[360, 720], [750, 1320]]
    windows = [[d * 1440 + s, d * 1440 + e] for d in range(5) for s, e in day]
    windows.append([5 * 1440 + 480, 5 * 1440 + 720])
    assert _calendar_plan(10080, windows) == (
        "Mon–Fri 06:00–22:00; Sat 08:00–12:00", "Mon–Fri 12:00–12:30"
    )


def test_other_cycle_keeps_raw_windows():
    from simtrace.tools.simulation.diagram import _calendar_plan
    assert _calendar_plan(20, [[0, 10]]) == ("0–10", "")


def test_calendar_table_in_page(tmp_path):
    target = tmp_path / "model.html"
    render_model_diagram(str(target), model=_routed_line())
    html = target.read_text(encoding="utf-8")
    assert "<th>Working hours</th><th>Breaks</th>" in html
    assert "<td>day</td><td>06:00–22:00</td><td>–</td>" in html


def test_technical_definition_lists_settings_and_wiring():
    from simtrace.tools.simulation.diagram import technical_definition
    html = technical_definition(_routed_line())
    assert "<h2>Technical definition</h2>" in html
    assert "<code>inter_arrival_time=&quot;exp(5)&quot;</code>" in html
    assert "<code>routes={&quot;A&quot;: &quot;b_good&quot;, &quot;*&quot;: &quot;b_scrap&quot;}</code>" in html
    assert "<td><code>b_a</code></td><td>Buffer</td><td><code>src_a</code></td><td><code>m</code></td>" in html
    assert "follows <code>day</code>" in html
    # Defaults stay out, e.g. blocking=True on a machine.
    assert "blocking=True" not in html
