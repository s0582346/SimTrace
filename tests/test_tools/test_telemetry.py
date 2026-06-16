"""Tests for the OpenTelemetry `traced` decorator.

Uses an in-memory span exporter so no collector/Jaeger is required.
"""

import inspect

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

from simgen.tools.telemetry import traced


@pytest.fixture(scope="module")
def exporter() -> InMemorySpanExporter:
    # set_tracer_provider only takes effect once per process; this module owns it.
    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    trace.set_tracer_provider(provider)
    return exp


@pytest.fixture(autouse=True)
def _clear(exporter):
    exporter.clear()
    yield


def test_traced_preserves_name_and_signature():
    @traced
    def sample(a: int, b: str = "x") -> int:
        return a

    assert sample.__name__ == "sample"
    assert list(inspect.signature(sample).parameters) == ["a", "b"]


def test_traced_creates_span_with_attributes(exporter):
    @traced
    def create_demo(id, capacity=1):
        return {"id": id}

    assert create_demo(id="B1", capacity=4) == {"id": "B1"}

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "create_demo"
    assert span.attributes["tool.name"] == "create_demo"
    assert span.attributes["tool.arg.id"] == "B1"
    assert span.attributes["tool.arg.capacity"] == 4
    assert span.status.status_code == StatusCode.OK


def test_traced_records_exception_and_sets_error(exporter):
    @traced
    def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        boom()

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR
    assert any(event.name == "exception" for event in span.events)


def test_traced_renders_nonprimitive_args(exporter):
    @traced
    def create_combiner(target_quantity_of_each_item=None):
        return {}

    create_combiner(target_quantity_of_each_item=[1, 2, 3])

    span = exporter.get_finished_spans()[0]
    # Homogeneous list of primitives is kept as a sequence attribute.
    assert list(span.attributes["tool.arg.target_quantity_of_each_item"]) == [1, 2, 3]
