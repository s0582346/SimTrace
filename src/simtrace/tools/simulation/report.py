"""Results of one run or a batch of runs as one HTML page: diagram plus tables."""

from __future__ import annotations

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
ul.settings { color: #222; margin: 0 0 12px; padding-left: 20px; }
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


def _validate_model_section(model: FactoryModel) -> str:
    """validate_model on the model as it was run: a summary line, then the
    errors and warnings as lists that open on click."""
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
    return (
        "<h2><code>validate_model</code></h2>"
        f"<p><span class='status {status}'>{escape(summary)}</span></p>"
        + _findings_list("Errors", errors, "fail")
        + _findings_list("Warnings", warnings, "warn")
    )


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
        ("Validation", _validate_model_section(model)),
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
