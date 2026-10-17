# tests/unit/test_tracing.py
"""P0 golden tests: shared @traced decorator contract (OTEL_PLAN §1)."""

import asyncio

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from investigation_agent_platform.infrastructure.observability.tracing import (
    MAX_ATTRIBUTE_CHARS,
    extract_scope,
    span_attributes,
    traced,
)


@pytest.fixture(scope="module")
def _provider():
    # Hermetic: patch get_tracer so decorations bind to OUR provider.
    # (The global provider may already be claimed by an earlier test module
    # constructing the OTel adapter — set_tracer_provider refuses overrides.)
    from unittest.mock import patch as _patch

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with _patch.object(trace, "get_tracer", provider.get_tracer):
        yield exporter


@pytest.fixture()
def exporter(_provider):
    _provider.clear()
    yield _provider
    _provider.clear()


def run(coro):
    return asyncio.run(coro)


def test_span_name_and_required_attrs(exporter):
    @traced("worker")
    async def execute_action_activity(tenant_id, investigation_id):
        return {"ok": True}

    run(execute_action_activity("t1", "i1"))
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "worker.test_tracing.execute_action_activity"
    attrs = dict(span.attributes or {})
    assert attrs["iap.tenant_id"] == "t1"
    assert attrs["iap.investigation_id"] == "i1"
    assert attrs["iap.component"] == "test_tracing"
    assert attrs["iap.outcome"] == "ok"
    assert span.status.status_code == StatusCode.OK


def test_sync_variant(exporter):
    @traced("reason", component="compiler")
    def compile_it(tenant_id):
        return 1

    assert compile_it("t9") == 1
    spans = exporter.get_finished_spans()
    assert spans[0].name == "reason.compiler.compile_it"
    assert dict(spans[0].attributes or {})["iap.outcome"] == "ok"


def test_exception_path_errors_and_reraises(exporter):
    @traced("gateway")
    async def boom(tenant_id, investigation_id):
        raise RuntimeError("kablam")

    with pytest.raises(RuntimeError, match="kablam"):
        run(boom("t1", "i1"))
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs["iap.outcome"] == "error"
    assert attrs["iap.error_type"] == "RuntimeError"
    assert "kablam" in attrs["iap.error_message"]
    assert spans[0].status.status_code == StatusCode.ERROR


def test_outcome_and_extra_mapping(exporter):
    @traced(
        "provider",
        outcome_from=lambda r: "degraded" if r.get("partial") else "ok",
        extra_from=lambda r: {"iap.items_count": r.get("n", 0)},
    )
    async def query(tenant_id, investigation_id):
        return {"partial": True, "n": 3}

    run(query("t1", "i1"))
    attrs = dict(exporter.get_finished_spans()[0].attributes or {})
    assert attrs["iap.outcome"] == "degraded"
    assert attrs["iap.items_count"] == 3


def test_missing_tenant_still_emits(exporter):
    @traced("knowledge")
    async def retrieve():
        return None

    run(retrieve())
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert dict(spans[0].attributes or {})["iap.outcome"] == "ok"


def test_scope_extraction_from_dataclass_params():
    from dataclasses import dataclass

    @dataclass
    class Params:
        tenant_id: str
        investigation_id: str

    # Positional dataclass (Temporal activity calling convention).
    assert extract_scope((Params("pt", "pi"),), {}) == ("pt", "pi")
    assert extract_scope((), {"params": Params("pt", "pi")}) == ("pt", "pi")
    assert extract_scope((), {"params": {"tenant_id": "dt", "investigation_id": "di"}}) == ("dt", "di")


def test_scope_extraction_paths():
    class Ctx:
        tenant_id = "ctx-t"
        investigation_id = "ctx-i"

    assert extract_scope((), {"tenant_id": "a", "investigation_id": "b"}) == ("a", "b")
    assert extract_scope((), {"context": Ctx()}) == ("ctx-t", "ctx-i")

    class Params:
        investigation_id = "p-i"

    assert extract_scope((), {"tenant_id": "a", "params": Params()}) == ("a", "p-i")
    assert extract_scope((), {}) == (None, None)


def test_long_attributes_truncated(exporter):
    @traced("worker", attributes={"iap.note": "x" * 500})
    async def act(tenant_id):
        return None

    run(act("t1"))
    attrs = dict(exporter.get_finished_spans()[0].attributes or {})
    assert len(attrs["iap.note"]) == MAX_ATTRIBUTE_CHARS


def test_wraps_preserved():
    @traced("worker")
    async def my_activity(tenant_id):
        """Docstring."""

    assert my_activity.__name__ == "my_activity"
    assert my_activity.__doc__ == "Docstring."


def test_span_attributes_helper():
    attrs = span_attributes("t", "i", component="manual", **{"iap.items_count": 5})
    assert attrs == {
        "iap.tenant_id": "t",
        "iap.investigation_id": "i",
        "iap.component": "manual",
        "iap.items_count": 5,
    }
