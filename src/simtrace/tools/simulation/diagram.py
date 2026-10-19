"""Draw the assembled model as a Mermaid flowchart in one HTML page."""

from __future__ import annotations

import inspect
import os
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
.note { color: #555; margin: 0 0 12px; }
.diagram { overflow-x: auto; border: 1px solid #ddd; border-radius: 8px; padding: 12px; }
table { border-collapse: collapse; font-size: 0.9rem; }
th, td { border: 1px solid #ddd; padding: 4px 8px; text-align: left; }
th { background: #f4f4f6; }
code { font-size: 0.85rem; }
@media (prefers-color-scheme: dark) {
  body { color: #e8e8ea; background: #1b1b1f; }
  .note { color: #aaa; }
  .diagram, th, td { border-color: #444; }
  th { background: #2a2a30; }
}
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


def _calendar_table(model: FactoryModel) -> str:
    if not model.calendars:
        return ""
    rows = []
    for calendar_id, calendar in model.calendars.items():
        windows = ", ".join(f"{start:g}–{end:g}" for start, end in calendar.windows)
        default = "yes" if calendar_id == model.default_calendar else ""
        rows.append(
            f"<tr><td><code>{escape(calendar_id)}</code></td><td>{calendar.cycle:g}</td>"
            f"<td>{escape(windows)}</td><td>{default}</td></tr>"
        )
    return (
        "<h2>Calendars</h2><table><tr><th>Calendar</th><th>Cycle</th>"
        "<th>Working windows</th><th>Default</th></tr>" + "".join(rows) + "</table>"
    )


def _unconnected(model: FactoryModel) -> str:
    loose = [edge_id for edge_id, edge in model.edges.items() if edge.src_node is None]
    if not loose:
        return ""
    names = ", ".join(f"<code>{escape(edge_id)}</code>" for edge_id in loose)
    return f"<h2>Not connected</h2><p>{names}</p>"


def render_page(model: FactoryModel) -> str:
    source = mermaid_source(model)
    return "".join([
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>Model diagram</title>",
        f"<style>{_CSS}</style></head><body>",
        "<h1>Model diagram</h1>",
        f"<p class='note'>{len(model.nodes)} nodes, {len(model.edges)} edges. "
        "Settings at their default are left out.</p>",
        f"<div class='diagram'><pre class='mermaid'>{escape(source)}</pre></div>",
        _calendar_table(model),
        _unconnected(model),
        "<script type='module'>",
        f"import mermaid from '{_MERMAID_URL}';",
        "const dark = matchMedia('(prefers-color-scheme: dark)').matches;",
        "mermaid.initialize({startOnLoad: true, theme: dark ? 'dark' : 'default',",
        " flowchart: {htmlLabels: true}, securityLevel: 'loose'});",
        "</script></body></html>",
    ])


def render_model_diagram(
    path: str = "out/model.html", *, model: FactoryModel | None = None
) -> dict:
    """Write the model diagram to `path` and return where it went."""
    model = model if model is not None else get_session_model()
    if not model.nodes:
        raise ValueError(
            "The model is empty. Build it with the create_* tools first."
        )
    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(render_page(model))
    return {
        "path": target,
        "nodes": len(model.nodes),
        "edges": len(model.edges),
        "mermaid": mermaid_source(model),
    }
