# tests/unit/test_structured_logging.py
"""Structured JSON logging + date-seq rotation (infrastructure/telemetry/logging.py).

Formatter: extras merge, trace injection, exception envelope, fallback.
Handler: date-stamped active file + _v1.._vN cascade on size rollover.
"""

import json
import logging

from investigation_agent_platform.infrastructure.telemetry.logging import (
    DateSeqRotatingFileHandler,
    StructuredJsonFormatter,
    _resolve_file_target,
)


def _record(**extra):
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="hello %s",
        args=("world",),
        exc_info=None,
        func="_record",
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_extras_merge_and_envelope_shape():
    out = json.loads(
        StructuredJsonFormatter(service_name="svc", environment="dev").format(
            _record(tenant_id="t1", custom=42)
        )
    )
    assert out["level"] == "INFO"
    assert out["logger"] == "test.logger"
    assert out["message"] == "hello world"
    assert out["service"] == {"name": "svc", "environment": "dev"}
    assert out["code"]["function"] == "_record"
    assert out["extra"]["tenant_id"] == "t1"
    assert out["extra"]["custom"] == 42
    assert "trace_id" not in out  # no active span


def test_trace_context_injected():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("s"):
        out = json.loads(StructuredJsonFormatter().format(_record()))
    assert len(out["trace_id"]) == 32
    assert len(out["span_id"]) == 16


def test_exception_envelope():
    try:
        raise ValueError("bad thing")
    except ValueError:
        import sys

        record = _record()
        record.exc_info = sys.exc_info()
    out = json.loads(StructuredJsonFormatter().format(record))
    assert "ValueError" in out["exception"]["details"]
    assert "bad thing" in out["exception"]["details"]


def test_reserved_attrs_not_duplicated_in_extra():
    out = json.loads(StructuredJsonFormatter().format(_record()))
    assert "extra" not in out or "levelname" not in out.get("extra", {})


def test_date_seq_cascade_on_size(tmp_path):
    handler = DateSeqRotatingFileHandler(
        tmp_path, app_name="iap", max_bytes=200, backup_count=5
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("cascade-test")
    log.handlers = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False
    for i in range(30):
        log.info("line %03d padding padding padding padding", i)
    handler.close()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert any(n.startswith("iap_") and n.endswith(".log") and "_v" not in n for n in names)
    assert any("_v1.log" in n for n in names)


def test_resolve_file_target():
    base, app = _resolve_file_target("data/iap.log")
    assert app == "iap" and str(base) == "data"
    base, app = _resolve_file_target("data/logs")
    assert str(base) == "data/logs"
