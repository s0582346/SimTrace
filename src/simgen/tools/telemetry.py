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

import functools
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

tracer = trace.get_tracer("simgen.tools")

F = TypeVar("F", bound=Callable[..., Any])

_configured = False

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


def configure_telemetry(service_name: str = "simgen", endpoint: str | None = None) -> None:
    """Install an OTLP/HTTP exporting TracerProvider (idempotent).

    Args:
        service_name: value for the `service.name` resource attribute.
        endpoint: OTLP/HTTP traces endpoint. Defaults to the SDK's standard
            resolution (the OTEL_EXPORTER_OTLP_ENDPOINT env var, else
            http://localhost:4318/v1/traces) when None.
    """
    global _configured
    if _configured:
        return

    # Imported lazily so the http exporter's transitive deps (requests, etc.)
    # are only required when telemetry is actually turned on.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _configured = True
