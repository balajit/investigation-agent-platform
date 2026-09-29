# tests/unit/test_part10_phase5.py
"""Part 10 Phase 5: mapping-driven projection against real-shaped documents."""

import uuid
from pathlib import Path

from investigation_agent_platform.domain.evidence.completeness import EvidenceDataQuality
from investigation_agent_platform.domain.evidence.models import ClassificationLevel
from investigation_agent_platform.domain.observability.mapping import generic_ecs_mapping
from investigation_agent_platform.infrastructure.configuration.mapping_registry import (
    load_mapping_profiles,
)
from investigation_agent_platform.infrastructure.evidence.runtime.projection import (
    ElasticEvidenceProjector,
)

SHIPPED_DIR = Path(__file__).resolve().parents[2] / "config" / "observability-mappings"


def _project(source, mapping=None, **kwargs):  # type: ignore[no-untyped-def]
    hit = {"_id": "doc1", "_index": "idx-1", "_source": source}
    hit.update(kwargs)
    return ElasticEvidenceProjector().project(
        hit,
        "tenant-a",
        uuid.uuid4(),
        operation="SEARCH",
        query_fingerprint="f" * 64,
        mapping=mapping,
    )


def _xcos_mappings():  # type: ignore[no-untyped-def]
    registry = load_mapping_profiles(SHIPPED_DIR)
    return registry.resolve("gen-verbose"), registry.resolve("gen-root-cause")


def test_xcos_verbose_document_projects_resolved_fields():
    verbose, _ = _xcos_mappings()
    ev = _project(
        {
            "@timestamp": "2026-01-01T00:00:00Z",
            "message": "checkout timeout",
            "appName": "checkout",
            "traceId": "abc123",
            "Service": "ignored-position",
            "error": False,
            "RequestBody": '{"secret": "hunter2"}',
            "labels": {"trace_id": "should-not-appear"},
        },
        mapping=verbose,
    )
    assert ev.observed_at is not None
    assert ev.data_quality == []
    # Resolved presentation fields come from the mapping, not ECS paths.
    assert ev.attributes["appName"] == "checkout"
    assert ev.attributes["traceId"] == "abc123"
    # Non-allowlisted giants never enter, even un-truncated.
    assert "RequestBody" not in ev.attributes
    # ECS-namespace keys absent from this mapping's allowlist are dropped.
    assert "labels" not in ev.attributes
    import json as _json

    snippet = _json.loads(ev.content_snippet)
    assert snippet["service"] == "checkout"
    assert snippet["trace_id"] == "abc123"
    assert snippet["severity"] == "INFO"
    assert snippet["message"] == "checkout timeout"


def test_xcos_verbose_error_flag_severity():
    verbose, _ = _xcos_mappings()
    ev = _project({"@timestamp": "2026-01-01T00:00:00Z", "error": True}, mapping=verbose)
    import json as _json

    assert _json.loads(ev.content_snippet)["severity"] == "ERROR"


def test_root_cause_epoch_timestamp_and_unsupported_extras():
    _, root_cause = _xcos_mappings()
    ev = _project(
        {
            "updatedAt": 1767225600000,
            "message": "triage failed",
            "XCOSSessionId": "sess-9",
            "errorReason": "timeout",
        },
        mapping=root_cause,
    )
    assert ev.observed_at is not None
    assert ev.observed_at.year == 2026
    assert ev.data_quality == []
    assert ev.attributes["XCOSSessionId"] == "sess-9"
    assert ev.classification == ClassificationLevel.INTERNAL


def test_root_cause_missing_timestamp_is_flagged_not_invalid():
    _, root_cause = _xcos_mappings()
    ev = _project({"message": "no time here"}, mapping=root_cause)
    assert ev.observed_at is None
    assert EvidenceDataQuality.TIMESTAMP_MISSING in ev.data_quality
    assert EvidenceDataQuality.TIMESTAMP_INVALID not in ev.data_quality


def test_classification_override_from_mapping():
    verbose, _ = _xcos_mappings()
    assert (
        _project({"@timestamp": "2026-01-01T00:00:00Z"}, mapping=verbose).classification
        == ClassificationLevel.INTERNAL
    )
    generic = generic_ecs_mapping()
    ev = _project(
        {
            "@timestamp": "2026-01-01T00:00:00Z",
            "labels": {"data_classification": "secret"},
        },
        mapping=generic,
    )
    assert ev.classification == ClassificationLevel.SECRET


def test_xcos_core_fallback_keeps_identity_scalars():
    verbose, _ = _xcos_mappings()
    ev = _project(
        {
            "@timestamp": "2026-01-01T00:00:00Z",
            "message": "m",
            "appName": "checkout",
            "traceId": "abc123",
            "extract": {"request": {"ORDERID": "x" * 20000}},
        },
        mapping=verbose,
    )
    assert EvidenceDataQuality.ATTRIBUTES_TRUNCATED in ev.data_quality
    assert ev.attributes.get("appName") == "checkout"
    assert ev.attributes.get("traceId") == "abc123"
    import json as _json

    assert len(_json.dumps(ev.attributes, default=str).encode()) <= 16384


def test_generic_ecs_default_preserved_without_mapping_arg():
    ev = _project(
        {
            "@timestamp": "2026-01-01T00:00:00Z",
            "message": "boom",
            "service": {"name": "checkout"},
            "log": {"level": "ERROR"},
            "labels": {"trace_id": "abc"},
        }
    )
    assert ev.attributes["service"] == {"name": "checkout"}
    import json as _json

    snippet = _json.loads(ev.content_snippet)
    assert snippet == {
        "observed_at": "2026-01-01T00:00:00+00:00",
        "service": "checkout",
        "severity": "ERROR",
        "trace_id": "abc",
        "message": "boom",
    }
