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
from collections.abc import Callable, Collection, Iterator, Sequence
from typing import Any, TypeVar

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

tracer = trace.get_tracer("simtrace.tools")

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
    # The edge's own view of the same hand-off. A non-blocking Source narrates its
    # put WITHOUT the item id ("src puts item into B1"), so for that one hop these
    # are the only lines naming which item moved. Buffers and fleets share the
    # first wording; conveyors use the second, where "on belt" is required to
    # avoid also matching the class-level "Conveyor:put:" line that carries no
    # edge id. `build_item_paths` drops any edge id the model doesn't know.
    ("edge_put", re.compile(
        r"^(?P<edge>\S+) is putting item (?P<item>\S+) with delay")),
    ("edge_put", re.compile(
        r"^(?P<edge>\S+):put: putting item (?P<item>\S+) on belt")),
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


def build_item_paths(
    events: Sequence[dict[str, Any]],
    edge_ids: Collection[str] = (),
) -> dict[str, list[str]]:
    """Reconstruct each item's trail through the plant from a run's events.

    A trail alternates the things an item actually touched, in travel order:

        ["src", "B1", "M1", "C1", "M2", "B2", "snk"]

    Node ids and edge ids share one namespace (`FactoryModel.has_id`), so no
    element is ambiguous and a reader can tell node from edge by looking the id
    up in `model.nodes` / `model.edges`.

    A `put` event names the node the item left *and* the edge it left into, which
    is one whole hop. `received` closes the trail at the sink. A **non-blocking
    Source** is the one hop narrated without an item id, so there the edge's own
    `edge_put` line is the only witness: such a trail begins with an edge id
    rather than a node, and the reader resolves the Source from that edge's
    `src_node` (see `verify_item_flow`).

    Both lines describe every hop and the edge's comes first, so an `edge_put` is
    held *pending* and only placed if no `put` follows to name the node behind it.
    Holding one per item (rather than pre-indexing the pairs) keeps this right when
    an item crosses the same edge twice in a rework loop.

    Pass `edge_ids` (the model's wired edge ids) so an `edge_put` line whose id is
    not a real edge cannot enter a trail. Pure function over already-collected
    events: no simulation needed to test it.
    """
    trails: dict[str, list[str]] = {}
    pending: dict[str, str] = {}

    def emit(item: str, element: str) -> None:
        trail = trails.setdefault(item, [])
        # An item narrated twice at the same place is not a move.
        if not trail or trail[-1] != element:
            trail.append(element)

    def flush(item: str) -> None:
        """Place a held edge, i.e. one no `put` came along to attribute."""
        edge = pending.pop(item, None)
        if edge is not None:
            emit(item, edge)

    for event in events:
        kind = event.get("kind")
        item = _norm_item(event.get("item"))
        if item is None:
            continue

        if kind == "edge_put":
            edge = event.get("edge")
            if edge not in edge_ids:
                continue
            flush(item)
            pending[item] = edge
        elif kind == "put":
            node = event.get("node")
            if node is None:
                continue
            edge = event.get("edge")
            if pending.get(item) == edge:
                # This is the node behind the held edge: the node goes first.
                del pending[item]
            else:
                flush(item)
            emit(item, node)
            if edge is not None:
                emit(item, edge)
        else:
            # received / process_start / get / …: nothing more is coming to
            # attribute a held edge, so it stands on its own.
            flush(item)
            if kind == "received":
                node = event.get("node")
                if node is not None:
                    emit(item, node)

    for item in list(pending):
        flush(item)

    return trails


def _add_sim_event(
    span: trace.Span,
    line: str,
    collector: list[dict[str, Any]] | None = None,
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
    tools. Per-item trails are built from that collection afterwards by
    `build_item_paths`, not accumulated here.
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


@contextlib.contextmanager
def traced_stdout(
    collector: list[dict[str, Any]] | None = None,
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
    Hand that list to `build_item_paths` afterwards for the per-item trails.
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
                _add_sim_event(span, stripped, collector)


def configure_telemetry(
    service_name: str = "simtrace",
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
