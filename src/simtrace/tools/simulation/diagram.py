"""Draw the assembled model as a Mermaid flowchart in one HTML page."""

from __future__ import annotations

import inspect
import json
import os
from datetime import datetime
from html import escape

from simtrace.model import FactoryModel
from simtrace.model import get_model as get_session_model
from simtrace.tools import builders

_MERMAID_URL = "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.esm.min.mjs"

_NODE_OPS = {
    "create_source": "Source",
    "create_sink": "Sink",
    "create_machine": "Machine",
    "create_splitter": "Splitter",
    "create_combiner": "Combiner",
}
_EDGE_OPS = {
    "create_buffer": "Buffer",
    "create_conveyor": "Conveyor",
    "create_fleet": "Fleet",
}

# Settings shown in a box, in this order, when they differ from the default.
_NODE_FIELDS = {
    "Source": ["inter_arrival_time", "part_type", "attributes", "blocking",
               "out_edge_selection", "node_setup_time"],
    "Sink": ["node_setup_time"],
    "Machine": ["processing_delay", "work_capacity", "time_to_failure",
                "repair_time", "in_edge_selection", "out_edge_selection",
                "route_by", "blocking", "node_setup_time"],
    "Splitter": ["processing_delay", "mode", "split_quantity",
                 "in_edge_selection", "out_edge_selection", "blocking",
                 "node_setup_time"],
    "Combiner": ["processing_delay", "target_quantity_of_each_item",
                 "out_edge_selection", "blocking", "node_setup_time"],
}
_EDGE_FIELDS = {
    "Buffer": ["capacity", "delay", "mode"],
    "Conveyor": ["conveyor_length", "speed", "item_length", "accumulating"],
    "Fleet": ["capacity", "delay", "transit_delay"],
}
# Settings that are always shown, default or not.
_ALWAYS = {"inter_arrival_time", "processing_delay", "capacity"}

_SHAPES = {
    "Source": ('(["', '"])'),
    "Sink": ('(["', '"])'),
    "Machine": ('{{"', '"}}'),
    "Splitter": ('[/"', '"\\]'),
    "Combiner": ('[\\"', '"/]'),
}

_CSS = """
body { font-family: system-ui, sans-serif; margin: 0 16px 32px; color: #1d1d1f; background: #fff; }
h1 { font-size: 1.4rem; margin: 20px 0 4px; }
h2 { font-size: 1.1rem; margin-top: 28px; }
h3 { font-size: 0.95rem; margin: 18px 0 8px; }
table + .note { margin-top: 6px; }
.note { color: #222; margin: 0 0 12px; }
.diagram { overflow-x: auto; border: 1px solid #ddd; border-radius: 8px; padding: 12px; }
table { border-collapse: collapse; font-size: 0.9rem; }
th, td { border: 1px solid #ddd; padding: 4px 8px; text-align: left; }
th { background: #f4f4f6; }
code { font-size: 0.85rem; }
:root { color-scheme: light; }
header.brand { display: flex; justify-content: space-between; align-items: center;
  margin: 0 -16px; padding: 10px 16px; background: #0f1b2a; color: #e8eef4; }
header.brand .mark { display: flex; align-items: center; gap: 8px;
  font-size: 1.15rem; letter-spacing: 0.02em; }
header.brand .mark b { color: #2dd4d4; }
header.brand .stamp { font-size: 0.85rem; color: #9fb0c0; font-variant-numeric: tabular-nums; }
h1 { border-left: 4px solid #f5b03c; padding-left: 10px; }
"""


def _defaults(op: str) -> dict:
    signature = inspect.signature(getattr(builders, op))
    return {
        name: param.default
        for name, param in signature.parameters.items()
        if param.default is not inspect.Parameter.empty
    }


def _text(value: object) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{key}={_text(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return ", ".join(_text(item) for item in value)
    return str(value)


def _settings(op: str, kwargs: dict, fields: list[str]) -> list[str]:
    defaults = _defaults(op)
    lines = []
    for field in fields:
        value = kwargs.get(field)
        if value is None:
            continue
        if field not in _ALWAYS and field in defaults and value == defaults[field]:
            continue
        lines.append(f"{field}: {_text(value)}")
    return lines


def _label(lines: list[str]) -> str:
    # Mermaid reads #quot; as a quote inside a quoted label.
    return "<br/>".join(line.replace('"', "#quot;") for line in lines)


def _calendar_of(kind: str, kwargs: dict, default: str | None) -> str | None:
    if kind not in ("Source", "Machine"):
        return None
    if kwargs.get("calendar"):
        return kwargs["calendar"]
    return f"{default} (default)" if default else None


def _route_labels(nodes: dict) -> dict[str, list[str]]:
    """Edge id -> labels from the routing of the node in front of it."""
    labels: dict[str, list[str]] = {}
    for node in nodes.values():
        kwargs = node["kwargs"]
        route_by = kwargs.get("route_by")
        for value, targets in (kwargs.get("routes") or {}).items():
            targets = targets if isinstance(targets, list) else [targets]
            text = "other" if value == "*" else f"{route_by}={value}"
            for edge_id in targets:
                labels.setdefault(edge_id, []).append(text)
        for edge_id, share in (kwargs.get("route_shares") or {}).items():
            labels.setdefault(edge_id, []).append(f"{share:.0%}")
    return labels


def mermaid_source(model: FactoryModel) -> str:
    """The model's build log as Mermaid flowchart text."""
    nodes: dict[str, dict] = {}
    edges: dict[str, dict] = {}
    connects: list[dict] = []
    for step in model.spec:
        op, kwargs = step["op"], step["kwargs"]
        if op in _NODE_OPS:
            nodes[kwargs["id"]] = {"op": op, "kind": _NODE_OPS[op], "kwargs": kwargs}
        elif op in _EDGE_OPS:
            edges[kwargs["id"]] = {"op": op, "kind": _EDGE_OPS[op], "kwargs": kwargs}
        elif op == "connect":
            connects.append(kwargs)

    keys = {node_id: f"n{index}" for index, node_id in enumerate(nodes)}
    out = ["flowchart LR"]

    for node_id, node in nodes.items():
        kind, kwargs = node["kind"], node["kwargs"]
        lines = [f"<b>{node_id}</b>", kind.lower()]
        if kind == "Sink" and kwargs.get("scrap"):
            lines[1] = "sink · scrap"
        lines += _settings(node["op"], kwargs, _NODE_FIELDS[kind])
        calendar = _calendar_of(kind, kwargs, model.default_calendar)
        if calendar:
            lines.append(f"calendar: {calendar}")
        start, end = _SHAPES[kind]
        out.append(f"    {keys[node_id]}{start}{_label(lines)}{end}")

    routes = _route_labels(nodes)
    # FIRST_AVAILABLE drains in-edges in connect order, so number them.
    in_order: dict[str, list[str]] = {}
    for step in connects:
        in_order.setdefault(step["dest_id"], []).append(step["edge_id"])

    for step in connects:
        edge_id, src, dest = step["edge_id"], step["src_id"], step["dest_id"]
        edge = edges[edge_id]
        lines = [f"<b>{edge_id}</b>"]
        lines += _settings(edge["op"], edge["kwargs"], _EDGE_FIELDS[edge["kind"]])
        lines += routes.get(edge_id, [])
        target = nodes[dest]
        ordered = in_order[dest]
        if (
            len(ordered) > 1
            and target["kind"] in ("Machine", "Splitter")
            and target["kwargs"].get("in_edge_selection", "FIRST_AVAILABLE") == "FIRST_AVAILABLE"
        ):
            lines.append(f"priority {ordered.index(edge_id) + 1}")
        out.append(f'    {keys[src]} -->|"{_label(lines)}"| {keys[dest]}')

    return "\n".join(out)


_DAY = 1440
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _clock(minutes: float) -> str:
    """Minutes since the start of a day as hh:mm. 1440 is 24:00, later wraps."""
    if minutes > _DAY:
        minutes -= _DAY
    hours, rest = divmod(round(minutes), 60)
    return f"{hours:02d}:{rest:02d}"


def _span(start: float, end: float, day_start: float) -> str:
    return f"{_clock(start - day_start)}–{_clock(end - day_start)}"


def _day_plan(windows: list, day_start: float) -> tuple[str, str]:
    """Working hours and breaks of one day's windows."""
    hours = _span(windows[0][0], windows[-1][1], day_start)
    breaks = ", ".join(
        _span(first[1], second[0], day_start) for first, second in zip(windows, windows[1:])
    )
    return hours, breaks


def _calendar_plan(cycle: float, windows: list) -> tuple[str, str]:
    """Working hours and breaks as clock times, for a cycle of a day or a week
    in minutes. Other cycles keep their raw windows and get no breaks."""
    windows = [tuple(window) for window in windows]
    if cycle == _DAY:
        # A night shift over midnight is a last and a first window that touch it.
        if len(windows) > 1 and windows[0][0] == 0 and windows[-1][1] == cycle:
            windows = windows[1:-1] + [(windows[-1][0], windows[0][1] + cycle)]
        return _day_plan(windows, 0)
    if cycle != 7 * _DAY:
        raw = ", ".join(f"{start:g}–{end:g}" for start, end in windows)
        return raw, ""

    # Group days with the same plan that follow each other, e.g. "Mon–Fri".
    groups: list[list] = []
    for day, name in enumerate(_WEEKDAYS):
        own = [w for w in windows if day * _DAY <= w[0] < (day + 1) * _DAY]
        plan = _day_plan(own, day * _DAY) if own else None
        if groups and groups[-1][2] == plan and groups[-1][1] == day - 1:
            groups[-1][1] = day
        else:
            groups.append([day, day, plan])
    hours, breaks = [], []
    for first, last, plan in groups:
        if plan is None:
            continue
        days = _WEEKDAYS[first] if first == last else f"{_WEEKDAYS[first]}–{_WEEKDAYS[last]}"
        hours.append(f"{days} {plan[0]}")
        if plan[1]:
            breaks.append(f"{days} {plan[1]}")
    return "; ".join(hours), "; ".join(breaks)


def _calendar_table(model: FactoryModel) -> str:
    if not model.calendars:
        return ""
    rows = []
    for calendar_id, calendar in model.calendars.items():
        hours, breaks = _calendar_plan(calendar.cycle, calendar.windows)
        cycle = {_DAY: "day", 7 * _DAY: "week"}.get(calendar.cycle, f"{calendar.cycle:g}")
        default = "yes" if calendar_id == model.default_calendar else ""
        rows.append(
            f"<tr><td><code>{escape(calendar_id)}</code></td><td>{cycle}</td>"
            f"<td>{escape(hours)}</td><td>{escape(breaks) or '–'}</td><td>{default}</td></tr>"
        )
    return (
        "<h3>Calendars</h3><table><tr><th>Calendar</th><th>Cycle</th>"
        "<th>Working hours</th><th>Breaks</th><th>Default</th></tr>"
        + "".join(rows) + "</table>"
    )


def _unconnected(model: FactoryModel) -> str:
    loose = [edge_id for edge_id, edge in model.edges.items() if edge.src_node is None]
    if not loose:
        return ""
    names = ", ".join(f"<code>{escape(edge_id)}</code>" for edge_id in loose)
    return f"<h3>Not connected</h3><p>{names}</p>"


def _setting(name: str, value: object) -> str:
    """One setting as it is written in a create_* call, e.g. capacity=10."""
    if isinstance(value, str):
        text = f'{name}="{value}"'
    elif isinstance(value, (dict, list, tuple)):
        text = f"{name}={json.dumps(value)}"
    else:
        text = f"{name}={value}"
    return f"<code>{escape(text)}</code>"


def _call_settings(op: str, kwargs: dict) -> str:
    """Every setting of a create_* call that is not its default."""
    defaults = _defaults(op)
    parts = [
        _setting(name, value)
        for name, value in kwargs.items()
        if name != "id"
        and value is not None
        and (name in _ALWAYS or name not in defaults or value != defaults[name])
    ]
    return ", ".join(parts)


def technical_definition(model: FactoryModel) -> str:
    """Node, edge and calendar tables built from the model's build log."""
    ends = {
        step["kwargs"]["edge_id"]: (step["kwargs"]["src_id"], step["kwargs"]["dest_id"])
        for step in model.spec if step["op"] == "connect"
    }
    node_rows, edge_rows = [], []
    for step in model.spec:
        op, kwargs = step["op"], step["kwargs"]
        if op in _NODE_OPS:
            node_rows.append(
                f"<tr><td><code>{escape(kwargs['id'])}</code></td><td>{_NODE_OPS[op]}</td>"
                f"<td>{_call_settings(op, kwargs)}</td></tr>"
            )
        elif op in _EDGE_OPS:
            src, dest = ends.get(kwargs["id"], ("–", "–"))
            edge_rows.append(
                f"<tr><td><code>{escape(kwargs['id'])}</code></td><td>{_EDGE_OPS[op]}</td>"
                f"<td><code>{escape(src)}</code></td><td><code>{escape(dest)}</code></td>"
                f"<td>{_call_settings(op, kwargs)}</td></tr>"
            )
    out = ["<h2>Technical definition</h2>"]
    if node_rows:
        out.append(
            "<h3>Nodes</h3><table><tr><th>Node</th><th>Type</th><th>Settings</th></tr>"
            + "".join(node_rows) + "</table>"
        )
    if model.default_calendar:
        out.append(
            f"<p class='note'>Every machine and source without a <code>calendar</code> "
            f"follows <code>{escape(model.default_calendar)}</code>.</p>"
        )
    if edge_rows:
        out.append(
            "<h3>Edges</h3><table><tr><th>Edge</th><th>Type</th><th>From</th><th>To</th>"
            "<th>Settings</th></tr>" + "".join(edge_rows) + "</table>"
        )
    out.append(_calendar_table(model))
    out.append(_unconnected(model))
    return "".join(out)


def diagram_section(model: FactoryModel) -> str:
    """The diagram, then the technical definition, as page body."""
    return "".join([
        f"<div class='diagram'><pre class='mermaid'>{escape(mermaid_source(model))}</pre></div>",
        technical_definition(model),
    ])


def _brand_header() -> str:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        "<header class='brand'><span class='mark'>"
        "<span>sim<b>trace</b></span></span>"
        f"<span class='stamp'>{stamp}</span></header>"
    )


def page(title: str, body: str, css: str = "") -> str:
    """A complete HTML page with the brand header that draws every Mermaid
    block in `body`."""
    return "".join([
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>{escape(title)}</title>",
        f"<style>{_CSS}{css}</style></head><body>",
        _brand_header(),
        f"<h1>{escape(title)}</h1>",
        body,
        "<script type='module'>",
        f"import mermaid from '{_MERMAID_URL}';",
        "mermaid.initialize({startOnLoad: true, theme: 'default',",
        " flowchart: {htmlLabels: true}, securityLevel: 'loose'});",
        "</script></body></html>",
    ])


def write_page(path: str, html: str) -> str:
    """Write `html` to `path`, creating the folder, and return the full path."""
    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(html)
    return target


def render_page(model: FactoryModel) -> str:
    return page("Model diagram", diagram_section(model))


def render_model_diagram(
    path: str = "out/model.html", *, model: FactoryModel | None = None
) -> dict:
    """Write the model diagram to `path` and return where it went."""
    model = model if model is not None else get_session_model()
    if not model.nodes:
        raise ValueError(
            "The model is empty. Build it with the create_* tools first."
        )
    target = write_page(path, render_page(model))
    return {
        "path": target,
        "nodes": len(model.nodes),
        "edges": len(model.edges),
        "mermaid": mermaid_source(model),
    }
