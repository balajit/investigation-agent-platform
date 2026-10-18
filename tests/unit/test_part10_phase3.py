# tests/unit/test_part10_phase3.py
"""Part 10 Phase 3: pure field resolution against real-shaped documents."""

from datetime import UTC, datetime

from investigation_agent_platform.domain.evidence.completeness import EvidenceDataQuality
from investigation_agent_platform.domain.evidence.models import LogSeverity
from investigation_agent_platform.domain.observability.mapping import (
    FieldMapping,
    FieldValueType,
    SeverityDerivation,
    SeverityStrategy,
)
from investigation_agent_platform.infrastructure.evidence.runtime.field_resolver import (
    resolve_identifier_field,
    resolve_severity,
    resolve_timestamp,
    resolve_value,
)


def _mapping(candidates, value_type=FieldValueType.KEYWORD) -> FieldMapping:
    return FieldMapping(candidates=list(candidates), value_type=value_type)


def test_resolve_value_first_present_wins():
    mapping = _mapping(["Service", "appName", "System"])
    assert resolve_value({"appName": "checkout", "System": "x"}, mapping) == "checkout"
    assert resolve_value({"System": "x"}, mapping) == "x"
    assert resolve_value({}, mapping) is None
    assert resolve_value({"Service": ""}, mapping) is None


def test_resolve_value_never_treats_zero_or_false_as_absent():
    assert resolve_value({"errorCode": 0}, _mapping(["errorCode"], FieldValueType.LONG)) == 0
    assert resolve_value({"error": False}, _mapping(["error"], FieldValueType.BOOLEAN)) is False


def test_resolve_value_dotted_traversal_with_guards():
    mapping = _mapping(["code.file.name"])
    assert resolve_value({"code": {"file": {"name": "a.py"}}}, mapping) == "a.py"
    assert resolve_value({"code": "not-a-dict"}, mapping) is None
    assert resolve_value({"code": {"file": ["x"]}}, mapping) is None
    assert resolve_value({"code": {"other": 1}}, mapping) is None


def test_resolve_timestamp_iso_variants():
    mapping = _mapping(["@timestamp"], FieldValueType.DATE_ISO)
    observed, flags = resolve_timestamp({"@timestamp": "2026-01-01T00:00:00Z"}, mapping)
    assert observed == datetime(2026, 1, 1, tzinfo=UTC) and flags == []
    observed, flags = resolve_timestamp({"@timestamp": "2026-01-01T02:00:00+02:00"}, mapping)
    assert observed is not None and flags == []
    observed, flags = resolve_timestamp({"@timestamp": "2026-01-01T00:00:00"}, mapping)
    assert observed is None and flags == [EvidenceDataQuality.TIMESTAMP_NAIVE]
    observed, flags = resolve_timestamp({"@timestamp": "not-a-date"}, mapping)
    assert observed is None and flags == [EvidenceDataQuality.TIMESTAMP_INVALID]
    observed, flags = resolve_timestamp({}, mapping)
    assert observed is None and flags == [EvidenceDataQuality.TIMESTAMP_MISSING]


def test_resolve_timestamp_epoch_variants():
    millis = _mapping(["updatedAt"], FieldValueType.DATE_EPOCH_MILLIS)
    observed, flags = resolve_timestamp({"updatedAt": 1767225600000}, millis)
    assert observed == datetime(2026, 1, 1, tzinfo=UTC) and flags == []
    observed, flags = resolve_timestamp({"updatedAt": 1767225600000.5}, millis)
    assert observed is not None and flags == []
    for bad in (True, "1767225600000", None):
        doc = {} if bad is None else {"updatedAt": bad}
        observed, flags = resolve_timestamp(doc, millis)
        assert observed is None
        assert flags == [
            EvidenceDataQuality.TIMESTAMP_MISSING
            if bad is None
            else EvidenceDataQuality.TIMESTAMP_INVALID
        ]
    seconds = _mapping(["createdAt"], FieldValueType.DATE_EPOCH_SECONDS)
    observed, flags = resolve_timestamp({"createdAt": 1767225600}, seconds)
    assert observed == datetime(2026, 1, 1, tzinfo=UTC) and flags == []


def test_resolve_timestamp_first_present_candidate_parsed():
    mapping = _mapping(["updatedAt", "createdAt"], FieldValueType.DATE_EPOCH_MILLIS)
    observed, flags = resolve_timestamp({"createdAt": 1767225600000}, mapping)
    assert observed == datetime(2026, 1, 1, tzinfo=UTC) and flags == []


def test_resolve_timestamp_present_empty_never_falls_through():
    # An empty present value claims the field: INVALID, not MISSING, and no
    # fallback to later candidates.
    mapping = _mapping(["@timestamp", "createdAt"], FieldValueType.DATE_ISO)
    observed, flags = resolve_timestamp(
        {"@timestamp": "", "createdAt": "2026-01-01T00:00:00Z"}, mapping
    )
    assert observed is None
    assert flags == [EvidenceDataQuality.TIMESTAMP_INVALID]


def test_resolve_severity_strategies():
    field = SeverityDerivation(
        strategy=SeverityStrategy.FIELD,
        field=_mapping(["log.level"]),
    )
    assert resolve_severity({"log": {"level": "error"}}, field) == LogSeverity.ERROR
    assert resolve_severity({"log": {"level": "BOGUS"}}, field) is None
    assert resolve_severity({}, field) is None
    assert resolve_severity({"log": {"level": 5}}, field) is None

    assert (
        resolve_severity({"a": 1}, SeverityDerivation(strategy=SeverityStrategy.UNSUPPORTED))
        is None
    )

    flag = SeverityDerivation(strategy=SeverityStrategy.ERROR_FLAG, error_field="error")
    assert resolve_severity({"error": True}, flag) == LogSeverity.ERROR
    assert resolve_severity({"error": False}, flag) == LogSeverity.INFO
    assert resolve_severity({"error": 1}, flag) == LogSeverity.ERROR
    assert resolve_severity({"error": 0}, flag) == LogSeverity.INFO
    assert resolve_severity({"error": "yes"}, flag) == LogSeverity.ERROR
    assert resolve_severity({"error": "weird"}, flag) is None
    assert resolve_severity({}, flag) is None

    custom = SeverityDerivation(
        strategy=SeverityStrategy.ERROR_FLAG,
        error_field="error",
        true_severity=LogSeverity.FATAL,
        false_severity=LogSeverity.DEBUG,
    )
    assert resolve_severity({"error": True}, custom) == LogSeverity.FATAL
    assert resolve_severity({"error": False}, custom) == LogSeverity.DEBUG


def test_resolve_identifier_field_qualification():
    assert resolve_identifier_field("trace_id", _mapping(["traceId"])) == "traceId"
    assert (
        resolve_identifier_field("q", _mapping(["RequestBody"], FieldValueType.TEXT))
        == "RequestBody.keyword"
    )
    assert resolve_identifier_field("q", _mapping(["a", "b"])) == "a"


def test_resolve_against_real_fixture_shapes():
    import json
    from pathlib import Path

    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "elastic_mappings"
        / "gen_verbose_prod.json"
    )
    properties = json.loads(fixture.read_text(encoding="utf-8"))["properties"]
    assert "traceId" in properties and "appName" in properties
    assert "code" not in properties
    document = {
        "traceId": "abc123",
        "appName": "checkout",
        "error": False,
        "errorCode": 0,
        "@timestamp": "2026-01-01T00:00:00Z",
    }
    assert resolve_value(document, _mapping(["appName", "Service"])) == "checkout"
    assert resolve_value(document, _mapping(["Service"])) is None
    flag = SeverityDerivation(strategy=SeverityStrategy.ERROR_FLAG, error_field="error")
    assert resolve_severity(document, flag) == LogSeverity.INFO
