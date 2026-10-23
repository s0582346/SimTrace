"""Results of one run or a batch of runs as one HTML page: diagram plus tables."""

from __future__ import annotations

import itertools
import math
import statistics
from html import escape

from scipy import stats

from simtrace.model import FactoryModel
from simtrace.tools.simulation.diagram import diagram_section, page, write_page
from simtrace.tools.telemetry import _norm_item

# Column -> the state names that count for it. Machines use the first name,
# splitters and combiners the second, sources GENERATING_STATE and
# BLOCKED_STATE.
_STATE_COLUMNS = {
    "Working": ("ATLEAST_ONE_PROCESSING_STATE", "PROCESSING_STATE", "GENERATING_STATE"),
    "Idle": ("IDLE_STATE",),
    "Blocked": ("ALL_ACTIVE_BLOCKED_STATE", "BLOCKED_STATE"),
    "Down": ("BREAKDOWN_STATE",),
    "Off shift": ("OFF_SHIFT_STATE",),
    "Setup": ("SETUP_STATE",),
}
_STATION_OPS = ("create_source", "create_machine", "create_splitter", "create_combiner")

_CSS = """
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.helper { color: #777; font-size: 0.8rem; }
.pm { color: #999; font-size: 0.85em; }
nav.tabs { display: flex; gap: 4px; border-bottom: 2px solid #0f1b2a; margin: 20px 0 0; }
nav.tabs button { font: inherit; padding: 8px 16px; border: 1px solid #ccc;
  border-bottom: none; border-radius: 6px 6px 0 0; background: #f4f4f6; color: #333;
  cursor: pointer; }
nav.tabs button.active { background: #0f1b2a; border-color: #0f1b2a; color: #fff; }
section.tab { display: none; }
section.tab.active { display: block; }
h2 code { font-size: inherit; }
.status { display: inline-block; padding: 3px 10px; border-radius: 12px; font-weight: 600; }
.status.pass { background: #e3f4e8; color: #1f6b35; }
.status.warn { background: #fdf1d8; color: #8a5a00; }
.status.fail { background: #fbe3e3; color: #a12828; }
details.findings { margin: 8px 0; border: 1px solid #ddd; border-radius: 6px; padding: 6px 12px; }
details.findings summary { cursor: pointer; font-weight: 600; }
details.findings.warn summary { color: #8a5a00; }
details.findings.fail summary { color: #a12828; }
details.findings li { margin: 8px 0; }
details.findings p.check { margin: 12px 0 6px; }
details.findings table { margin-bottom: 8px; }
details.tool { margin: 16px 0; border: 1px solid #ccc; border-radius: 8px; padding: 8px 14px; }
details.tool > summary { cursor: pointer; font-size: 1.15rem; font-weight: 600; }
details.tool > summary .status { font-size: 0.85rem; margin-left: 8px; }
details.tool h3 { margin: 14px 0 6px; font-size: 1rem; }
ul.settings { color: #222; margin: 0 0 12px; padding-left: 20px; }
details.findings.info summary { color: #2f6fb0; }
.extreme .xc-switch { display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0 6px; }
.extreme .xc-switch button { font: inherit; font-size: 0.84rem; padding: 4px 12px;
  border: 1px solid #dfe2e7; border-radius: 14px; background: #f4f5f7; color: #1d1d1f;
  cursor: pointer; }
.extreme .xc-switch button[aria-pressed="true"] { background: #0f1b2a; border-color: #0f1b2a;
  color: #fff; }
.extreme .xc-pane { display: none; }
.extreme .xc-pane.active { display: block; }
.extreme .ladder { display: grid; gap: 1px; min-width: 0; }
.extreme .grp { display: flex; align-items: center; gap: 7px; font-size: 0.7rem;
  letter-spacing: 0.06em; text-transform: uppercase; color: #5b6370; font-weight: 600;
  margin: 10px 0 2px 4px; }
.extreme .grp i { display: inline-block; width: 10px; height: 10px; flex: none; }
.extreme .row { display: grid; grid-template-columns: var(--lab, 150px) minmax(0, 1fr); gap: 10px;
  align-items: center; border: 0; background: none; font: inherit; color: inherit;
  text-align: left; padding: 3px 4px; border-radius: 4px; cursor: pointer; width: 100%; }
.extreme .row:hover { background: #f4f5f7; }
.extreme .row.sel { background: #f4f5f7; }
.extreme .row .lab { font-size: 0.82rem; line-height: 1.25; min-width: 0;
  overflow-wrap: anywhere; }
.extreme .track { position: relative; height: 22px; }
.extreme .track::before { content: ""; position: absolute; left: 0; right: 0; top: 50%;
  border-top: 1px dashed #dfe2e7; }
.extreme .track .ab { position: absolute; top: 0; bottom: 0; width: 2px;
  background: #1d1d1f; opacity: 0.18; }
.extreme .mark { position: absolute; top: 50%; width: 11px; height: 11px;
  transform: translate(-50%, -50%); }
.extreme .circle { border-radius: 50%; background: #c27613; }
.extreme .square { background: #1b7f8c; }
.extreme .diamond { background: #6b7686; }
.extreme .grp i.diamond { transform: rotate(45deg) scale(0.8); }
.extreme .mark.diamond { transform: translate(-50%, -50%) rotate(45deg) scale(0.85); }
.extreme .mark.flag { box-shadow: 0 0 0 3px #e6eef8, 0 0 0 4px #2f6fb0; }
.extreme .val { position: absolute; top: 50%; transform: translate(8px, -50%);
  font-size: 0.74rem; color: #5b6370; white-space: nowrap;
  font-variant-numeric: tabular-nums; }
.extreme .val.left { transform: translate(calc(-100% - 8px), -50%); }
.extreme .none { position: absolute; left: 0; top: 50%; transform: translateY(-50%);
  font-size: 0.74rem; font-style: italic; color: #5b6370; background: #fff;
  padding-right: 6px; }
.extreme .row:hover .none, .extreme .row.sel .none { background: #f4f5f7; }
.extreme .axis { display: grid; grid-template-columns: var(--lab, 150px) minmax(0, 1fr); gap: 10px;
  padding: 0 4px; }
.extreme .ticks { position: relative; height: 18px; font-size: 0.72rem; color: #5b6370; }
.extreme .ticks span { position: absolute; transform: translateX(-50%); white-space: nowrap; }
.extreme .axis-title { font-size: 0.74rem; color: #5b6370; text-align: center;
  margin-top: 2px; }
.extreme .grid-wrap { display: flex; flex-wrap: wrap; gap: 20px; align-items: flex-start;
  overflow-x: auto; margin-top: 10px; }
.extreme table.bounds { border-collapse: separate; border-spacing: 4px; font-size: 0.88rem; }
.extreme table.bounds th { font-weight: 600; font-size: 0.78rem; color: #5b6370;
  text-align: center; padding: 4px 8px; border: none; background: none; }
.extreme table.bounds th span { font-weight: 400; }
.extreme table.bounds th.rowh { text-align: right; white-space: nowrap; }
.extreme table.bounds th { white-space: nowrap; }
.extreme table.bounds.compact td.cell { min-width: 0; padding: 6px 8px; }
.extreme table.bounds.compact td.cell b { font-size: 1rem; }
.extreme table.bounds.compact td.cell small, .extreme table.bounds.compact td.cell br { display: none; }
.extreme table.bounds.compact .lim { margin-top: 2px; font-size: 0.68rem; padding: 0 5px; }
.extreme table.bounds td.cell { background: #f4f5f7; border: none; border-radius: 6px;
  padding: 10px 14px; text-align: center; min-width: 118px; cursor: pointer; }
.extreme table.bounds td.cell:hover { outline: 2px solid #1b7f8c; }
.extreme table.bounds td.cell.empty { cursor: default; color: #5b6370; }
.extreme table.bounds td.cell.artifact { background: repeating-linear-gradient(135deg,
  #f4f5f7 0 6px, #e9ebef 6px 12px); }
.extreme table.bounds td.cell b { display: block; font-size: 1.35rem;
  font-variant-numeric: tabular-nums; }
.extreme table.bounds td.cell small { color: #5b6370; font-size: 0.74rem; }
.extreme table.bounds td.eff { color: #5b6370; font-size: 0.8rem; text-align: center;
  border: none; font-variant-numeric: tabular-nums; }
.extreme table.bounds td.eff b { color: #1d1d1f; font-size: 1rem; }
.extreme .lim { display: inline-block; margin-top: 4px; font-size: 0.72rem; padding: 1px 7px;
  border-radius: 9px; background: #fff; border: 1px solid #dfe2e7; }
"""


def _item_times(model: FactoryModel) -> dict[str, dict[str, float]]:
    """Per sink, the time the received items spent in production and transport.

    Production runs from a node's process_start to its put of the same item.
    Transport runs from a put into a conveyor or fleet to the next node's
    process_start or the sink's received.
    """
    from factorysimpy.edges.buffer import Buffer

    moving_edges = {
        edge_id for edge_id, edge in model.edges.items() if not isinstance(edge, Buffer)
    }
    per_item: dict[str, dict] = {}
    for event in model.events:
        item = _norm_item(event.get("item"))
        time = event.get("time")
        if item is None or time is None:
            continue
        entry = per_item.setdefault(
            item, {"production": 0.0, "transport": 0.0, "started": None, "moving": None}
        )
        kind = event["kind"]
        if kind == "process_start":
            if entry["moving"] is not None:
                entry["transport"] += time - entry["moving"]
                entry["moving"] = None
            entry["started"] = (event.get("node"), time)
        elif kind == "put":
            started = entry["started"]
            if started is not None and started[0] == event.get("node"):
                entry["production"] += time - started[1]
                entry["started"] = None
            if event.get("edge") in moving_edges:
                entry["moving"] = time
        elif kind == "received":
            if entry["moving"] is not None:
                entry["transport"] += time - entry["moving"]
                entry["moving"] = None
            entry["sink"] = event.get("node")

    sums: dict[str, dict[str, float]] = {}
    for entry in per_item.values():
        sink = entry.get("sink")
        if sink is None:
            continue
        total = sums.setdefault(sink, {"production": 0.0, "transport": 0.0})
        total["production"] += entry["production"]
        total["transport"] += entry["transport"]
    return sums


def report_view(result: dict, model: FactoryModel | None = None) -> dict:
    """The part of a run_simulation result the report needs, as plain data.

    With `model`, it also holds `item_times` from the run's events.
    """
    def keep(stats_dict):
        if not isinstance(stats_dict, dict):
            return {}
        out = {
            key: value
            for key, value in stats_dict.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        states = stats_dict.get("total_time_spent_in_states")
        if isinstance(states, dict):
            out["states"] = dict(states)
        return out

    return {
        "nodes": {key: keep(value) for key, value in result.get("nodes", {}).items()},
        "edges": {key: keep(value) for key, value in result.get("edges", {}).items()},
        "part_types": result.get("part_types", {}),
        "item_times": _item_times(model) if model is not None else {},
    }


def _summary(values: list[float]) -> dict:
    n = len(values)
    mean = statistics.mean(values)
    half = None
    if n > 1:
        half = stats.t.ppf(0.975, n - 1) * statistics.stdev(values) / math.sqrt(n)
    return {"mean": mean, "half": half, "min": min(values), "max": max(values), "n": n}


def _number(value: float) -> str:
    return f"{value:,.0f}" if abs(value) >= 100 else f"{value:,.2f}".rstrip("0").rstrip(".")


def _cell(values: list[float], percent_of: float | None = None) -> str:
    if not values:
        return "<td class='num'>–</td>"
    if percent_of:
        values = [100 * value / percent_of for value in values]
    s = _summary(values)
    unit = "%" if percent_of else ""
    if percent_of:
        text = f"{s['mean']:.1f}{unit}"
        if s["half"] is not None:
            text += f" <span class='pm'>± {s['half']:.1f}</span>"
    else:
        text = _number(s["mean"])
        if s["half"] is not None:
            text += f" <span class='pm'>± {_number(s['half'])}</span>"
    return f"<td class='num'>{text}</td>"


def _values(views: list[dict], section: str, owner: str, key: str) -> list[float]:
    return [
        view[section][owner][key]
        for view in views
        if key in view[section].get(owner, {})
    ]


def _state_values(views: list[dict], owner: str, names: tuple) -> list[float]:
    values = []
    for view in views:
        states = view["nodes"].get(owner, {}).get("states", {})
        found = [states[name] for name in names if name in states]
        if found:
            values.append(found[0])
    return values


def _table(head: list[str], rows: list[str], numeric_from: int = 1) -> str:
    cells = "".join(
        f"<th class='num'>{escape(name)}</th>" if index >= numeric_from
        else f"<th>{escape(name)}</th>"
        for index, name in enumerate(head)
    )
    return f"<table><tr>{cells}</tr>{''.join(rows)}</table>"


def _spec_nodes(model: FactoryModel, ops: tuple) -> list[dict]:
    return [step["kwargs"] for step in model.spec if step["op"] in ops]


def _output(model: FactoryModel, views: list[dict], until: float) -> str:
    """Per sink: throughput, throughput per hour, mean throughput time and the
    split of that time into production, transport and storage."""
    rows = []
    for kwargs in _spec_nodes(model, ("create_sink",)):
        sink = kwargs["id"]
        received, per_hour, mean_time, production, transport, storage = ([] for _ in range(6))
        for view in views:
            stats_ = view["nodes"].get(sink, {})
            count = stats_.get("num_item_received", 0)
            cycle = stats_.get("total_cycle_time", 0.0)
            received.append(count)
            per_hour.append(60 * count / until)
            if count == 0 or cycle <= 0:
                continue
            times = view.get("item_times", {}).get(sink, {})
            produced = times.get("production", 0.0) / cycle
            moved = times.get("transport", 0.0) / cycle
            mean_time.append(cycle / count)
            production.append(produced)
            transport.append(moved)
            storage.append(1 - produced - moved)
        name = f"<code>{escape(sink)}</code>"
        if kwargs.get("scrap"):
            name += " <span class='helper'>scrap</span>"
        cells = [
            _cell(received),
            _cell(per_hour),
            _cell(mean_time),
            _cell(production, 1),
            _cell(transport, 1),
            _cell(storage, 1),
        ]
        rows.append(f"<tr><td>{name}</td>{''.join(cells)}</tr>")
    if not rows:
        return ""
    head = ["Sink", "Throughput [parts]", "Per hour [parts/h]", "Mean throughput time [min]",
            "Production [%]", "Transport [%]", "Storage [%]"]
    return "<h2>Output</h2>" + _table(head, rows)


def _columns(views: list[dict], owners: list[str], columns: dict) -> list[tuple]:
    """The state columns that at least one owner reports."""
    return [
        (name, states) for name, states in columns.items()
        if any(_state_values(views, owner, states) for owner in owners)
    ]


def _stations(model: FactoryModel, views: list[dict], until: float) -> str:
    # Sources first, then the stations in build order.
    stations = _spec_nodes(model, _STATION_OPS[:1]) + _spec_nodes(model, _STATION_OPS[1:])
    if not stations:
        return ""
    owners = [kwargs["id"] for kwargs in stations]
    # Setup is shown only when some station has a setup time.
    columns = [
        (name, states) for name, states in _columns(views, owners, _STATE_COLUMNS)
        if name != "Setup" or any(
            value > 0 for owner in owners for value in _state_values(views, owner, states)
        )
    ]
    has_breakdowns = any(
        _values(views, "nodes", kwargs["id"], "num_breakdowns") for kwargs in stations
    )
    head = ["Station", "Processed [parts]", *(f"{name} [%]" for name, _ in columns)]
    if has_breakdowns:
        head.append("Breakdowns [count]")
    sources = {kwargs["id"] for kwargs in _spec_nodes(model, ("create_source",))}
    rows = []
    for kwargs in stations:
        station = kwargs["id"]
        name = f"<code>{escape(station)}</code>"
        if station in sources:
            name += " <span class='helper'>source, processed = generated</span>"
            processed = "num_item_generated"
        else:
            processed = "num_item_processed"
            if kwargs.get("processing_delay", 0) == 0:
                name += " <span class='helper'>helper, takes no time</span>"
        cells = [_cell(_values(views, "nodes", station, processed))]
        cells += [_cell(_state_values(views, station, states), until) for _, states in columns]
        if has_breakdowns:
            cells.append(_cell(_values(views, "nodes", station, "num_breakdowns")))
        rows.append(f"<tr><td>{name}</td>{''.join(cells)}</tr>")
    return "<h2>Stations</h2>" + _table(head, rows)


def _buffers(model: FactoryModel, views: list[dict]) -> str:
    rows = []
    for step in model.spec:
        if step["op"] != "create_buffer":
            continue
        edge = step["kwargs"]["id"]
        capacity = step["kwargs"].get("capacity", 1)
        values = _values(views, "edges", edge, "time_averaged_num_of_items_in_buffer")
        if not values:
            continue
        rows.append(
            f"<tr><td><code>{escape(edge)}</code></td><td class='num'>{capacity}</td>"
            f"{_cell(values)}{_cell(values, capacity)}</tr>"
        )
    if not rows:
        return ""
    return "<h2>Buffers</h2>" + _table(
        ["Buffer", "Capacity [parts]", "Average items [parts]", "Average fill [%]"], rows
    )


def _part_types(views: list[dict]) -> str:
    names = list(views[0].get("part_types", {}))
    if not names:
        return ""
    rows = []
    for name in names:
        cells = "".join(
            _cell([view["part_types"][name][key] for view in views if name in view["part_types"]])
            for key in ("generated", "completed", "scrapped")
        )
        rows.append(f"<tr><td><code>{escape(name)}</code></td>{cells}</tr>")
    return "<h2>Part types</h2>" + _table(
        ["Part type", "Generated [parts]", "Completed [parts]", "Scrapped [parts]"], rows
    )


def _settings_line(runs: int, until: float, seed: int | None,
                   seed_base: int | None, requested: int | None) -> str:
    if requested is None:
        items = ["<b>One run</b>", f"Seed <b>{seed}</b>" if seed is not None else "No seed"]
    else:
        items = [f"<b>{runs}</b> of <b>{requested}</b> runs", f"Seed base <b>{seed_base}</b>"]
    items.append(f"Run length <b>{_number(until)}</b> min")
    if requested is not None:
        items.append("Numbers: mean ± half-width of the 95% interval")
    return "<ul class='settings'>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


# The first tab is open on load, so the Mermaid diagram in it is drawn while
# visible. Mermaid cannot measure a diagram inside a hidden tab.
_TAB_SCRIPT = """<script>
document.querySelectorAll('nav.tabs button').forEach(button => {
  button.addEventListener('click', () => {
    document.querySelectorAll('nav.tabs button, section.tab').forEach(
      element => element.classList.remove('active'));
    button.classList.add('active');
    document.getElementById(button.dataset.tab).classList.add('active');
  });
});
</script>"""


def _tabbed(tabs: list[tuple[str, str]]) -> str:
    buttons = "".join(
        f"<button type='button' data-tab='tab-{index}'"
        f"{' class=active' if index == 0 else ''}>{escape(name)}</button>"
        for index, (name, _) in enumerate(tabs)
    )
    sections = "".join(
        f"<section id='tab-{index}' class='tab{' active' if index == 0 else ''}'>{content}</section>"
        for index, (_, content) in enumerate(tabs)
    )
    return f"<nav class='tabs'>{buttons}</nav>{sections}{_TAB_SCRIPT}"


def _count(number: int, word: str) -> str:
    return f"{number} {word}" + ("" if number == 1 else "s")


# Checks whose findings carry numbers: a one-line description, the name of
# the component column, and (details key, column header, unit) per column.
_DETAIL_TABLES = {
    "station_overload": (
        "Machines that get more parts than they can finish.",
        "Machine",
        [
            ("effective_process_time", "Effective process time [min]", "time"),
            ("interarrival_time", "Interarrival time [min]", "time"),
            ("utilization", "Utilization [%]", "share"),
        ],
    ),
}


def _detail_value(value: float, unit: str) -> str:
    return f"{100 * value:.0f}%" if unit == "share" else f"{value:.2f}"


def _detail_table(check: str, findings: list[dict]) -> str:
    description, component, columns = _DETAIL_TABLES[check]
    rows = "".join(
        f"<tr><td><code>{escape(f['component'] or '')}</code></td>"
        + "".join(
            f"<td class='num'>{_detail_value(f['details'][key], unit)}</td>"
            for key, _, unit in columns
        )
        + "</tr>"
        for f in findings
    )
    head = "".join(
        f"<th class='num'>{escape(header)}</th>" for _, header, _ in columns
    )
    return (
        f"<p class='check'><code>{escape(check)}</code> {escape(description)}</p>"
        f"<table><tr><th>{component}</th>{head}</tr>{rows}</table>"
    )


def _findings_list(title: str, findings: list[dict], kind: str) -> str:
    if not findings:
        return ""
    tabled = [f for f in findings if f["check"] in _DETAIL_TABLES and "details" in f]
    listed = [f for f in findings if f not in tabled]
    tables = "".join(
        _detail_table(check, [f for f in tabled if f["check"] == check])
        for check in dict.fromkeys(f["check"] for f in tabled)
    )
    items = "".join(
        f"<li><code>{escape(f['check'])}</code>"
        + (f" at <code>{escape(f['component'])}</code>" if f.get("component") else "")
        + f"<br>{escape(f['message'])}</li>"
        for f in listed
    )
    return (
        f"<details class='findings {kind}'><summary>{title} ({len(findings)})</summary>"
        + tables
        + (f"<ul>{items}</ul>" if items else "")
        + "</details>"
    )


def _tool_section(tool: str, status: str, summary: str, body: str) -> str:
    """One validation tool as a block that opens on click; the status shows
    while it is closed."""
    return (
        f"<details class='tool'><summary><code>{tool}</code>"
        f"<span class='status {status}'>{escape(summary)}</span></summary>"
        f"{body}</details>"
    )


def _validate_model_section(model: FactoryModel) -> str:
    """validate_model on the model as it was run: errors and warnings as lists
    that open on click."""
    # Imported here: the validation package imports the simulation package.
    from simtrace.tools.validation.model_check import validate_model

    result = validate_model(model=model)
    errors, warnings = result["errors"], result["warnings"]
    if errors:
        status = "fail"
    elif warnings:
        status = "warn"
    else:
        status = "pass"
    summary = f"{_count(len(errors), 'error')}, {_count(len(warnings), 'warning')}"
    body = (
        _findings_list("Errors", errors, "fail")
        + _findings_list("Warnings", warnings, "warn")
    ) or "<p>No findings.</p>"
    return _tool_section("validate_model", status, summary, body)


_IMPROPER_SHOWN = 20


def _item_flow_section(model: FactoryModel, single_run: bool) -> str:
    """verify_item_flow on the last run: wiring check, routes per part type,
    repeat visits and items not delivered."""
    from simtrace.tools.validation.item_flow import verify_item_flow

    if not single_run:
        return _tool_section(
            "verify_item_flow", "warn", "not checked",
            "<p>run_replications keeps no item trails. Use run_simulation.</p>",
        )
    result = verify_item_flow(model=model)
    improper, warnings = result["improper"], result["warnings"]
    status = "fail" if improper else "warn" if warnings else "pass"
    summary = f"{result['passed']} of {result['delivered']} delivered items on a wired route"
    if warnings:
        summary += f", {_count(len(warnings), 'warning')}"

    parts = [_findings_list("Warnings", warnings, "warn")]
    if improper:
        items = "".join(
            f"<li><code>{escape(f['item'])}</code> at position {f['at']}"
            f"<br>{escape(f['reason'])}</li>"
            for f in improper[:_IMPROPER_SHOWN]
        )
        more = len(improper) - _IMPROPER_SHOWN
        parts.append(
            f"<details class='findings fail'><summary>Improper ({len(improper)})</summary>"
            f"<ul>{items}</ul>"
            + (f"<p>and {more} more.</p>" if more > 0 else "")
            + "</details>"
        )
    if result["routes"]:
        rows = [
            f"<tr><td>{escape(r['part_type'] or '–')}</td>"
            f"<td>{escape(' → '.join(r['route']))}</td>"
            f"<td>{'scrap' if r['scrap'] else 'good'}</td>"
            f"<td class='num'>{r['items']}</td>"
            f"<td class='num'>{100 * r['share']:.0f}%</td></tr>"
            for r in result["routes"]
        ]
        parts.append("<h3>Routes</h3>" + _table(
            ["Part type", "Route", "End", "Items", "Share [%]"], rows, numeric_from=3))
    if result["repeat_visits"]:
        rows = [
            f"<tr><td><code>{escape(r['node'])}</code></td>"
            f"<td class='num'>{r['items']}</td><td class='num'>{r['max_visits']}</td></tr>"
            for r in result["repeat_visits"]
        ]
        parts.append("<h3>Repeat visits</h3>" + _table(
            ["Station", "Items", "Most visits"], rows))
    if result["not_delivered"]:
        rows = [
            f"<tr><td><code>{escape(r['place'])}</code></td>"
            f"<td class='num'>{r['items']}</td></tr>"
            for r in result["not_delivered"]
        ]
        parts.append("<h3>Not delivered at run end</h3>" + _table(["Place", "Items"], rows))
    return _tool_section("verify_item_flow", status, summary, "".join(parts))


def _nice_end(value: float) -> tuple[float, float]:
    """An axis end at or above `value` and a tick step that divides it."""
    if value <= 0:
        return 1.0, 0.25
    raw = value / 5
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    return step * math.ceil(value / step - 1e-9), step


def _num(value: float | None) -> str:
    return "–" if value is None else f"{value:.2f}"


def _amount(value: float) -> str:
    """A count as a whole number, a mean of several runs with one decimal."""
    return f"{value:g}" if value == int(value) else f"{value:.1f}"


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _most_blocked(run: dict) -> float | None:
    """The largest share of the run any station spent blocked, in percent."""
    shares = run.get("shares")
    if not shares:
        return None
    return 100 * max(own["blocked"] for own in shares.values())


def _blocked_by(runs: list[dict]) -> str | None:
    """The station with the largest blocked share, on average over the runs."""
    blocked: dict[str, list[float]] = {}
    for run in runs:
        for station, own in (run.get("shares") or {}).items():
            blocked.setdefault(station, []).append(own["blocked"])
    return max(blocked, key=lambda s: sum(blocked[s])) if blocked else None


# Ladder measures: key, button, axis title, value of one run.
_MEASURES = (
    ("delivered", "Delivered", "parts delivered", lambda r: r.get("received")),
    ("wip", "WIP", "WIP at the end [parts]", lambda r: r.get("in_line")),
    ("blocked", "Blocked", "most-blocked station", _most_blocked),
)
_GROUPS = (
    ("bounds", "square", "Stations at their bounds"),
    ("inputs", "circle", "Extreme inputs"),
    ("targeted", "diamond", "Targeted extremes"),
)
# Extreme inputs in ladder order.
_INPUT_LABELS = {
    "zero_times": "zero times",
    "arrivals_x10": "arrivals × 10",
    "no_breakdowns": "no breakdowns",
    "endless_buffers": "endless buffers",
    "minimal_buffers": "minimal buffers",
    "no_arrivals": "no arrivals",
}


def _at_bound(bounds: dict, station: str, level: str) -> str:
    """A station's time at one bound, to two decimals at most."""
    value = bounds[station]["low" if level == "min" else "high"]
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _impossible(bounds: dict, levels: dict) -> bool:
    """A station sits at a lower bound of 0 that only its distribution allows."""
    return any(
        level == "min" and bounds[s]["low"] == 0 and bounds[s]["high"] > 0
        for s, level in levels.items()
    )


# Whether the grid names what limits the line in each cell and shows each
# station's effect. Off: the grid shows the parts delivered only.
_SHOW_LIMITS = False

# Whether the grid has its note on arrivals, breakdowns, runs and hatching.
_SHOW_GRID_NOTE = False

# Stations whose busy shares lie this close to the busiest one are named with it.
_LIMIT_TIE = 0.02


def _limit(runs: list[dict], saturated: bool) -> str | None:
    """What limits the line in these runs.

    "arrivals" when the line was not kept full and no source was ever held up
    by a full buffer. Otherwise the busiest station, busy meaning working or
    broken down, on average over the runs, joined by every station within
    `_LIMIT_TIE` of it.
    """
    if not runs:
        return None
    if not saturated and all(
        not any((run.get("sources_blocked") or {}).values()) for run in runs
    ):
        return "arrivals"
    busy: dict[str, list[float]] = {}
    for run in runs:
        for station, own in (run.get("shares") or {}).items():
            busy.setdefault(station, []).append(own["working"] + own.get("down", 0.0))
    if not busy:
        return None
    mean = {station: sum(v) / len(v) for station, v in busy.items()}
    top = max(mean.values())
    return " + ".join(s for s, v in mean.items() if v >= top - _LIMIT_TIE)


def _combination_cells(combos: dict) -> dict[tuple, dict]:
    """Per combination, as a tuple of levels in station order: the mean of its
    runs and what limits the line there."""
    grouped: dict[tuple, list[dict]] = {}
    for run in combos["runs"]:
        key = tuple(run["at"][s] for s in combos["stations"])
        grouped.setdefault(key, []).append(run)
    cells = {}
    for key, runs in grouped.items():
        done = [r for r in runs if not r["failed"]]
        cells[key] = {
            "id": "c" + "".join("1" if level == "max" else "0" for level in key),
            "levels": dict(zip(combos["stations"], key)),
            "failed": runs[0]["failed"] if not done else None,
            "values": {
                name: _mean([v for v in (value(r) for r in done) if v is not None])
                for name, _, _, value in _MEASURES
            },
            "limit": _limit(done, combos.get("arrivals") == "saturated"),
            "blocked_by": _blocked_by(done),
        }
    return cells


def _ladder_rows(result: dict, cells: dict[tuple, dict]) -> list[dict]:
    """Every run as one row: the combinations, the extreme inputs, the targeted extremes."""
    combos = result["combinations"]
    bounds = combos.get("bounds") or {}
    rows = []
    for cell in cells.values():
        levels = cell["levels"]
        rows.append({
            "id": cell["id"], "group": "bounds", "failed": cell["failed"],
            "label": " · ".join(f"{s} {_at_bound(bounds, s, lv)}" for s, lv in levels.items()),
            "values": cell["values"], "flag": _impossible(bounds, levels),
            "blocked_by": cell["blocked_by"],
        })
    inputs = result.get("inputs", {}).get("runs", [])
    for case, label in _INPUT_LABELS.items():
        runs = [r for r in inputs if r["case"] == case]
        if not runs:
            continue
        done = [r for r in runs if not r["failed"]]
        rows.append({
            "id": case, "group": "inputs", "label": label, "flag": False,
            "failed": runs[0]["failed"] if not done else None,
            "values": {
                name: _mean([v for v in (value(r) for r in done) if v is not None])
                for name, _, _, value in _MEASURES
            },
            "blocked_by": _blocked_by(done),
        })
    for n, run in enumerate((result.get("targeted") or {}).get("runs", [])):
        label = {
            "station_stopped": f"{run['target']} stopped",
            "one_branch": f"{run['target']}: all to {run['edge']}",
            "one_source": f"{run['target']} alone",
        }.get(run["case"], run["target"])
        rows.append({
            "id": f"t{n}", "group": "targeted", "label": label, "flag": False,
            "failed": run["failed"],
            "values": {name: None if run["failed"] else value(run)
                       for name, _, _, value in _MEASURES},
            "blocked_by": None if run["failed"] else _blocked_by([run]),
        })
    return rows


def _ladder(rows: list[dict], as_built: list[dict]) -> str:
    """Every run on one axis, one pane per measure behind a switch."""
    buttons, panes = [], []
    # Wide enough for the longest label on one line, up to half the page.
    width = min(440, max(150, 7 * max(len(row["label"]) for row in rows)))
    for i, (name, button, title, value) in enumerate(_MEASURES):
        built = _mean([v for v in (value(r) for r in as_built) if v is not None])
        seen = [r["values"][name] for r in rows if r["values"][name] is not None]
        if name == "blocked":
            end, step = 100.0, 25.0
        else:
            end, step = _nice_end(max(seen + ([built] if built is not None else []), default=0))
        unit = " %" if name == "blocked" else ""

        def pos(v: float) -> str:
            return f"{100 * v / end:.2f}%"

        html, group = [], None
        for row in rows:
            if row["group"] != group:
                group = row["group"]
                shape, heading = next((s, h) for g, s, h in _GROUPS if g == group)
                html.append(f"<div class='grp'><i class='{shape}'></i>{escape(heading)}</div>")
            shape = next(s for g, s, _ in _GROUPS if g == row["group"])
            v = row["values"][name]
            if row["failed"]:
                inner = "<span class='none'>failed</span>"
            elif v is None:
                inner = "<span class='none'>not recorded</span>"
            else:
                text = f"{v:.1f}" if name == "blocked" else _amount(round(v, 1))
                if name == "blocked" and v > 0 and row.get("blocked_by"):
                    unit_text = f"{unit} {escape(row['blocked_by'])}"
                else:
                    unit_text = unit
                left = " left" if v > 0.8 * end else ""
                inner = (f"<span class='mark {shape}{' flag' if row['flag'] else ''}' "
                         f"style='left:{pos(v)}'></span><span class='val{left}' "
                         f"style='left:{pos(v)}'>{text}{unit_text}</span>")
            line = (f"<span class='ab' style='left:{pos(built)}' "
                    f"title='as built: {_amount(round(built, 1))}{unit}'></span>"
                    if built is not None else "")
            html.append(
                f"<button type='button' class='row' data-id='{row['id']}'>"
                f"<span class='lab'>{escape(row['label'])}</span>"
                f"<span class='track'>{line}{inner}</span></button>"
            )
        ticks = "".join(
            f"<span style='left:{pos(k * step)}'>{k * step:g}{unit}</span>"
            for k in range(round(end / step) + 1)
        )
        html.append(f"<div class='axis'><div></div><div><div class='ticks'>{ticks}</div>"
                    f"<div class='axis-title'>{escape(title)}</div></div></div>")
        buttons.append(f"<button type='button' data-m='{name}' "
                       f"aria-pressed='{'true' if i == 0 else 'false'}'>{button}</button>")
        panes.append(f"<div class='xc-pane{' active' if i == 0 else ''}' data-m='{name}'>"
                     f"<div class='ladder' style='--lab: {width}px'>{''.join(html)}</div></div>")
    return (
        "<h3>Every run on one axis</h3>"
        f"<div class='xc-switch' role='group' aria-label='Measure'>{''.join(buttons)}</div>"
        + "".join(panes)
    )


def _header(bounds: dict, stations: list[str], levels: tuple, with_kind: bool) -> str:
    """A grid header: each station at its bound, and under a single station
    which bound that is."""
    lines = "<br>".join(
        f"{escape(s)} {_at_bound(bounds, s, lv)} min" for s, lv in zip(stations, levels)
    )
    if not with_kind or len(stations) != 1:
        return lines
    own = bounds[stations[0]]
    kind = own["entered"].split("(")[0]
    if levels[0] == "min":
        sub = f"{kind} lower bound"
    else:
        sub = f"{kind} upper bound" if own["capped"] else "99 % value"
    return f"{lines}<br><span>{escape(sub)}</span>"


def _bounds_grid(combos: dict, cells: dict[tuple, dict]) -> str:
    """The parts delivered at every combination, one cell each: rows for the
    first half of the stations, columns for the rest. With `_SHOW_LIMITS` each
    cell also names what limits the line, and each station's effect is shown."""
    stations = combos["stations"]
    bounds = combos.get("bounds") or {}
    if not stations or not bounds:
        return ""
    half = len(stations) // 2
    on_rows, on_cols = stations[:half], stations[half:]
    row_keys = list(itertools.product(("min", "max"), repeat=len(on_rows)))
    col_keys = list(itertools.product(("min", "max"), repeat=len(on_cols)))
    margin_col = _SHOW_LIMITS and len(on_cols) == 1
    margin_row = _SHOW_LIMITS and len(on_rows) == 1

    def delivered(rk: tuple, ck: tuple) -> float | None:
        cell = cells.get(rk + ck)
        return None if cell is None else cell["values"]["delivered"]

    def effect(low: float | None, high: float | None) -> str:
        return "–" if low is None or high is None else _amount(round(low - high, 1))

    head = "<tr><th></th>" + "".join(
        f"<th>{_header(bounds, on_cols, ck, True)}</th>" for ck in col_keys
    ) + (f"<th>{escape(on_cols[0])} effect</th>" if margin_col else "") + "</tr>"
    body, hatched = [], False
    for rk in row_keys:
        tds = []
        for ck in col_keys:
            cell = cells.get(rk + ck)
            if cell is None:
                tds.append("<td class='cell empty'>–</td>")
                continue
            impossible = _impossible(bounds, cell["levels"])
            hatched |= impossible
            value = cell["values"]["delivered"]
            shown = "failed" if cell["failed"] else _amount(round(value, 1)) if value is not None else "–"
            limit = (f"<br><span class='lim'>limit: {escape(cell['limit'])}</span>"
                     if _SHOW_LIMITS and cell["limit"] else "")
            tds.append(f"<td class='cell{' artifact' if impossible else ''}' "
                       f"data-run='{cell['id']}'><b>{shown}</b><small>parts</small>{limit}</td>")
        if margin_col:
            tds.append(f"<td class='eff'><b>{effect(delivered(rk, ('min',)), delivered(rk, ('max',)))}</b></td>")
        body.append(f"<tr><th class='rowh'>{_header(bounds, on_rows, rk, False)}</th>"
                    + "".join(tds) + "</tr>")
    if margin_row:
        body.append(
            f"<tr><th class='rowh'>{escape(on_rows[0])} effect</th>" + "".join(
                f"<td class='eff'><b>{effect(delivered(('min',), ck), delivered(('max',), ck))}</b></td>"
                for ck in col_keys
            ) + ("<td class='eff'></td>" if margin_col else "") + "</tr>"
        )
    side = ""
    if _SHOW_LIMITS and not (margin_col and margin_row) and combos["influence"]:
        rows = [
            f"<tr><td><code>{escape(r['component'])}</code></td>"
            f"<td class='num'>{r['mean']:.1f}</td><td class='num'>{r['smallest']}</td>"
            f"<td class='num'>{r['largest']}</td></tr>"
            for r in combos["influence"]
        ]
        side = ("<div class='effects'>"
                + _table(["Station", "Effect [parts]", "Smallest", "Largest"], rows)
                + "</div>")

    saturated = combos.get("arrivals") == "saturated"
    if saturated:
        note = ("Every source releases its next part as soon as there is room. "
                "The counts are the most the stations can deliver, not the "
                "throughput at the entered arrivals. "
                "Each station at its shortest or longest time.")
    else:
        note = (f"Arrivals as entered, since {combos.get('arrivals_why') or 'the mix can shift'}"
                ", each station at its shortest or longest time.")
    if combos.get("breakdowns_at_means"):
        note += " Breakdowns at their means."
    if combos["replications"] > 1:
        note += f" Each value is the mean of {combos['replications']} runs."
    if _SHOW_LIMITS:
        note += (" Margins show each station's effect." if margin_col and margin_row
                 else " The effect table shows each station's effect, the parts "
                      "delivered at its lower bound minus at its upper bound.")
    if hatched:
        note += " Hatched: a time the plant cannot have."
    return (
        f"<h3>{'What limits the line' if _SHOW_LIMITS else 'Parts delivered per combination'}</h3>"
        + (f"<p class='note'>{escape(note)}</p>" if _SHOW_GRID_NOTE else "")
        + f"<div class='grid-wrap'><table class='bounds{'' if len(stations) <= 2 else ' compact'}'>{head}{''.join(body)}</table>{side}</div>"
    )


_EXTREME_SCRIPT = """<script>
document.querySelectorAll('div.extreme').forEach(box => {
  box.querySelectorAll('.xc-switch button').forEach(button => {
    button.addEventListener('click', () => {
      box.querySelectorAll('.xc-switch button').forEach(
        other => other.setAttribute('aria-pressed', other === button));
      box.querySelectorAll('.xc-pane').forEach(
        pane => pane.classList.toggle('active', pane.dataset.m === button.dataset.m));
    });
  });
  const pick = id => box.querySelectorAll('.row').forEach(
    row => row.classList.toggle('sel', row.dataset.id === id));
  box.querySelectorAll('.row').forEach(
    row => row.addEventListener('click', () => pick(row.dataset.id)));
  box.querySelectorAll('td.cell[data-run]').forEach(cell => {
    cell.addEventListener('click', () => {
      pick(cell.dataset.run);
      const row = box.querySelector('.xc-pane.active .row[data-id="' + cell.dataset.run + '"]');
      if (row) row.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    });
  });
});
</script>"""


def _extreme_conditions_section(model: FactoryModel, until: float, seed: int | None) -> str:
    """verify_extreme_conditions on the model: its findings, every run on one
    axis, and what limits the line at every combination of station bounds."""
    from simtrace.tools.validation.extreme_conditions import verify_extreme_conditions

    try:
        result = verify_extreme_conditions(until, seed=seed, model=model)
    except ValueError as exc:
        return _tool_section("verify_extreme_conditions", "warn", "not checked",
                             f"<p>{escape(str(exc))}</p>")
    errors = [f for f in result["findings"] if f["severity"] == "error"]
    hints = [f for f in result["findings"] if f["severity"] == "hint"]
    notes = [f for f in result["findings"] if f["severity"] == "info"]
    combos = result["combinations"]
    status = "fail" if errors else "warn" if hints else "pass"
    summary = (f"{_count(len(errors), 'error')}, {_count(len(hints), 'hint')}, "
               f"{_count(len(combos['runs']), 'run')} at the bounds")

    parts = [_findings_list("Errors", errors, "fail"), _findings_list("Hints", hints, "warn"),
             _findings_list("For information", notes, "info")]

    if result["below_floor"]:
        rows = [
            f"<tr><td><code>{escape(p['item'])}</code></td><td class='num'>{_num(p['total'])}</td>"
            f"<td class='num'>{_num(p['route_floor'])}</td><td>{escape(p['cause'])}</td>"
            f"<td>{escape(' → '.join(p['route']))}</td></tr>"
            for p in result["below_floor"][:_IMPROPER_SHOWN]
        ]
        parts.append(
            "<h3>Parts quicker than the single part</h3>"
            + _table(["Part", "Total [min]", "Route floor [min]", "Cause", "Route"], rows)
        )

    cells = _combination_cells(combos)
    rows = _ladder_rows(result, cells)
    if rows:
        as_built = [r for r in result.get("inputs", {}).get("runs", [])
                    if r["case"] == "as_built" and not r["failed"]]
        parts.append(_ladder(rows, as_built))
    parts.append(_bounds_grid(combos, cells))
    return _tool_section("verify_extreme_conditions", status, summary,
                         f"<div class='extreme'>{''.join(parts)}</div>{_EXTREME_SCRIPT}")


def render_report(
    model: FactoryModel,
    views: list[dict],
    until: float,
    *,
    seed: int | None = None,
    seed_base: int | None = None,
    requested: int | None = None,
) -> str:
    """`requested` is the batch size of run_replications, None for one run."""
    tabs = [
        ("Model", diagram_section(model)),
        ("Output", "".join([
            _output(model, views, until),
            _stations(model, views, until),
            _buffers(model, views),
            _part_types(views),
        ])),
        ("Validation", _validate_model_section(model)
         + _item_flow_section(model, requested is None)
         + _extreme_conditions_section(model, until, seed if requested is None else seed_base)),
    ]
    body = _settings_line(len(views), until, seed, seed_base, requested) + _tabbed(tabs)
    title = "Single-Run Report" if requested is None else "Replication Report"
    return page(title, body, _CSS)


def write_report(path: str, model: FactoryModel, views: list[dict], until: float,
                 **settings) -> str:
    """Write the run report for `views` to `path` and return the full path.

    Args:
        path: file to write, e.g. "out/report.html". Missing folders are
            created and an existing file is replaced.
        model: the model that was run. Its build log gives the diagram and
            the order of the table rows.
        views: one `report_view` per run, in run order. One view gives a
            single-run report, several give means with 95% intervals.
        until: run length of every run. State shares are taken of it.
        **settings: passed on to `render_report` for the settings line:
            `seed` for one run, or `seed_base` and `requested` for a batch.
    """
    return write_page(path, render_report(model, views, until, **settings))
