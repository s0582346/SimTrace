"""OpenTelemetry tracing for the MCP tool surface.

`traced` wraps a tool so every call becomes a span (name = tool name) carrying
its arguments as attributes, recording exceptions, and setting an OK/ERROR
status. It is applied at the registry boundary (`register_tools`), so all tools
are covered uniformly without changing the builder functions.

`configure_telemetry` wires a `TracerProvider` that exports spans over OTLP/HTTP
to a collector (Jaeger all-in-one ingests OTLP directly). Call it once from the
server entry point. IMPORTANT: spans are exported over the network, never to
stdout — under the MCP stdio transport stdout is the JSON-RPC channel.

When `configure_telemetry` is never called (e.g. in tests), `trace.get_tracer`
returns OpenTelemetry's no-op tracer, so `traced` adds negligible overhead and
needs no running collector.
"""

from __future__ import annotations

import contextlib
import functools
import io
import re
from collections.abc import Callable, Iterator, Sequence
from typing import Any, TypeVar

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

tracer = trace.get_tracer("simgen.tools")

F = TypeVar("F", bound=Callable[..., Any])

_configured = False

# FactorySimPy prints per-tick lines like "T=2.00: M1 puts item item3 into B1"
# (a few belt/fleet lines use "T=2.00 ..." with no colon). We pull the leading
# sim-clock time out so it can ride as a span-event attribute; the rest is the
# human-readable message that `_classify` turns into a typed event.
_SIM_LINE = re.compile(r"^T=(?P<time>[\d.]+):?\s*(?P<message>.*)$")

# OpenTelemetry span attributes must be a primitive or a homogeneous sequence
# of primitives. Anything else is rendered to a string.
_PRIMITIVES = (bool, int, float, str)


def _attr_value(value: Any) -> Any:
    if value is None:
        return "None"
    if isinstance(value, _PRIMITIVES):
        return value
    if isinstance(value, Sequence) and all(isinstance(v, _PRIMITIVES) for v in value):
        return list(value)
    return repr(value)


def traced(fn: F) -> F:
    """Wrap a tool call in a span carrying its args, status, and exceptions.

    `functools.wraps` preserves the wrapped function's signature so FastMCP can
    still introspect it to build the tool's input schema.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with tracer.start_as_current_span(fn.__name__) as span:
            span.set_attribute("tool.name", fn.__name__)
            for key, value in kwargs.items():
                span.set_attribute(f"tool.arg.{key}", _attr_value(value))
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            span.set_status(Status(StatusCode.OK))
            return result

    return wrapper  # type: ignore[return-value]


# The raw FactorySimPy narration is mostly scheduler-internal chatter
# (worker-thread handoffs, "waiting for in_edge events", belt-phase bookkeeping).
# For verification & validation only a small item-flow vocabulary matters, so
# each captured line is matched against this ordered table and anything that
# matches nothing is dropped — the Jaeger timeline then shows only the flow
# story.
_EVENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("discard", re.compile(
        r"^(?P<node>\S+) (?:worker )?is discarding (?:item|empty pallet) "
        r"(?P<item>\S+) because out_edge (?P<edge>\S+) is full")),
    ("generated", re.compile(r"^(?P<node>\S+) generated item: (?P<item>\S+)")),
    ("received", re.compile(r"^(?P<node>\S+) got an (?P<item>.+)$")),
    ("get", re.compile(r"^(?P<node>\S+) gets item (?P<item>\S+) from (?P<edge>\S+)")),
    ("put", re.compile(
        r"^(?P<node>\S+) (?:worker )?puts (?:item|empty pallet) "
        r"(?P<item>\S+) into (?P<edge>\S+)")),
    ("put", re.compile(r"^(?P<node>\S+) puts (?P<item>\S+) item into (?P<edge>\S+)")),
    ("put", re.compile(r"^(?P<node>\S+) puts item into (?P<edge>\S+)")),
    ("put", re.compile(r"^(?P<node>\S+) (?P<item>\S+) pushed to buffer (?P<edge>\S+)")),
    ("process_start", re.compile(
        r"^(?P<node>\S+) worker started processing item (?P<item>\S+)")),
    ("process_end", re.compile(
        r"^(?P<node>\S+) worker processed (?:item|empty pallet): (?P<item>\S+)")),
    ("blocked", re.compile(r"^(?P<node>\S+) (?:worker )?is in BLOCKED_STATE")),
    ("state", re.compile(r"^(?P<node>\S+) is in (?P<state>[A-Z][A-Z_]*_STATE)")),
    ("state", re.compile(r"^(?P<node>\S+) is now (?P<state>\S+)")),
    ("state", re.compile(r"^(?P<node>\S+) completed (?P<state>setup)")),
    ("state", re.compile(r"^(?P<node>\S+) state changed from \S+ to (?P<state>\S+)")),
)

# Flip to True to also surface unmatched lines as generic `sim.other` events —
# useful when checking the table above isn't silently dropping something real.
_KEEP_UNMATCHED = False


def _classify(message: str) -> tuple[str, dict[str, Any]] | None:
    """Map one FactorySimPy line to a typed event kind plus its entities.

    Returns `(kind, attributes)` for a recognized line, or None to drop it
    (unless `_KEEP_UNMATCHED`, which routes the leftovers to an `other` kind).
    """
    norm = " ".join(message.split())
    for kind, pattern in _EVENT_PATTERNS:
        match = pattern.match(norm)
        if match:
            attrs = {
                f"sim.{name}": value
                for name, value in match.groupdict().items()
                if value is not None
            }
            return kind, attrs
    return ("other", {}) if _KEEP_UNMATCHED else None


# The sink narrates a received item by printing the object, whose repr is
# `Item(<id>)` / `Pallet(<id>, items=N)`, whereas every upstream event prints the
# bare `item.id`. Strip that wrapper so a delivered item's `received` event keys
# to the same id as its upstream `put` events; anything already bare (or None)
# passes through unchanged.
_ITEM_WRAPPER = re.compile(r"^(?:Item|Pallet)\((?P<id>[^,)]+)")


def _norm_item(item: str | None) -> str | None:
    if item is None:
        return None
    match = _ITEM_WRAPPER.match(item)
    return match.group("id") if match else item


# The node sequence of an item is reconstructed from exactly these event kinds:
# a `put` fires once per hop as the item LEAVES a node (its `node` field is that
# node), and `received` marks arrival at the sink. That pair enumerates the path
# gap-free under any edge-selection; other kinds (process_start/get/…) would only
# duplicate nodes, so they are not appended.
_PATH_EVENT_KINDS = frozenset({"put", "received"})


def _add_sim_event(
    span: trace.Span,
    line: str,
    collector: list[dict[str, Any]] | None = None,
    paths: dict[str, list[str]] | None = None,
) -> None:
    """Attach one captured FactorySimPy line to `span` as a typed event.

    The line is split into its sim-clock time and message; the message is
    classified into one of the item-flow kinds and emitted as a `sim.<kind>`
    span event carrying the entities it touched (node, item, edge, state) as
    `sim.*` attributes. Unclassified scheduler noise is dropped so the Jaeger
    timeline shows only the flow story.

    When `collector` is given, the same classified event is also appended to it
    as a flat dict (`kind` plus the `sim.*` entities with their prefix stripped:
    node/item/edge/state/time/message) for post-run analysis by the validation
    tools.

    When `paths` is given, `put`/`received` events also extend the per-item node
    sequence live (keyed by the normalized item id): this is the item-flow trail
    verify_item_flow validates against the wired graph. Because events arrive in
    causal order, appending as they stream reconstructs each item's path with no
    later sort needed.
    """
    parsed = _SIM_LINE.match(line)
    time = float(parsed.group("time")) if parsed else None
    message = parsed.group("message") if parsed else line

    classified = _classify(message)
    if classified is None:
        return
    kind, attrs = classified
    if time is not None:
        attrs["sim.time"] = time
    attrs["sim.message"] = " ".join(message.split())
    span.add_event(f"sim.{kind}", attributes=attrs)

    if collector is not None:
        event = {"kind": kind}
        event.update((name[len("sim."):], value) for name, value in attrs.items())
        collector.append(event)

    if paths is not None and kind in _PATH_EVENT_KINDS:
        item = _norm_item(attrs.get("sim.item"))
        node = attrs.get("sim.node")
        if item is not None and node is not None:
            paths.setdefault(item, []).append(node)


@contextlib.contextmanager
def traced_stdout(
    collector: list[dict[str, Any]] | None = None,
    paths: dict[str, list[str]] | None = None,
) -> Iterator[None]:
    """Redirect FactorySimPy's stdout into events on the current span.

    FactorySimPy narrates the run with print() (item moves, blocking, discards).
    Under the MCP stdio transport stdout is the JSON-RPC channel, so that output
    must never reach it; we redirect stdout to a buffer and, on exit, classify
    each captured line (`_add_sim_event`) into a typed `sim.<kind>` event on
    whatever span is currently active (the per-tool span opened by `traced`).
    Recognized item-flow lines land on the Jaeger timeline next to the tool span;
    scheduler-internal noise is dropped.

    When telemetry is not configured (e.g. in tests) the current span is
    OpenTelemetry's no-op span, so `add_event` does nothing and stdout is simply
    swallowed — same effect as the old silencing, at negligible cost.

    Pass `collector` (a list) to also capture each classified event as a flat
    dict for post-run analysis by the validation tools. This is independent of
    telemetry: the events are collected whether or not a real span is active.

    Pass `paths` (a dict) to also accumulate each item's node sequence from the
    `put`/`received` events as they stream — the per-item flow trail that
    verify_item_flow reads. Also independent of telemetry.
    """
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            yield
    finally:
        span = trace.get_current_span()
        for line in buf.getvalue().splitlines():
            stripped = line.strip()
            if stripped:
                _add_sim_event(span, stripped, collector, paths)


def configure_telemetry(
    service_name: str = "simgen",
    endpoint: str | None = None,
    span_event_limit: int = 2048,
) -> None:
    """Install an OTLP/HTTP exporting TracerProvider (idempotent).

    Args:
        service_name: value for the `service.name` resource attribute.
        endpoint: OTLP/HTTP traces endpoint. Defaults to the SDK's standard
            resolution (the OTEL_EXPORTER_OTLP_ENDPOINT env var, else
            http://localhost:4318/v1/traces) when None.
        span_event_limit: max events kept per span. OTel's default is 128, but a
            single `run_simulation` emits one flow event per item-hop and can far
            exceed that; raising it keeps the run's trace from being truncated.
            Long (multi-day) runs can still overflow — the digest/query channels
            are the durable answer; this just makes the Jaeger view representative.
    """
    global _configured
    if _configured:
        return

    # Imported lazily so the http exporter's transitive deps (requests, etc.)
    # are only required when telemetry is actually turned on.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(
        resource=resource,
        span_limits=SpanLimits(max_events=span_event_limit),
    )
    exporter = OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _configured = True
