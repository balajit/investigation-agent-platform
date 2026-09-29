# tests/unit/test_part10_phase1.py
"""Part 10 Phase 1: mapping domain models, profile link, trace flags, request fields."""

import pytest
from pydantic import ValidationError

from investigation_agent_platform.domain.evidence.models import LogSeverity
from investigation_agent_platform.domain.observability.mapping import (
    FieldValueType,
    SeverityDerivation,
    SeverityStrategy,
    SortOrder,
    SortSpec,
    TenantScope,
    TenantScopeStrategy,
    generic_ecs_mapping,
)


def test_severity_derivation_requires_strategy_fields():
    with pytest.raises(ValidationError):
        SeverityDerivation(strategy=SeverityStrategy.FIELD)
    with pytest.raises(ValidationError):
        SeverityDerivation(strategy=SeverityStrategy.ERROR_FLAG)
    ok = SeverityDerivation(strategy=SeverityStrategy.ERROR_FLAG, error_field="error")
    assert ok.true_severity == LogSeverity.ERROR
    assert ok.false_severity == LogSeverity.INFO
    assert SeverityDerivation(strategy=SeverityStrategy.UNSUPPORTED).field is None


def test_tenant_scope_field_strategy_requires_field():
    with pytest.raises(ValidationError):
        TenantScope(strategy=TenantScopeStrategy.FIELD)
    assert TenantScope(strategy=TenantScopeStrategy.NONE_REQUIRED).field is None


def test_sort_spec_defaults():
    spec = SortSpec(field="@timestamp")
    assert spec.order == SortOrder.DESC
    assert spec.format is None and spec.unmapped_type is None


def test_generic_ecs_snapshot_pins_pre_part10_behavior():
    """Any change here must be deliberate: this is the regression baseline."""
    mapping = generic_ecs_mapping()
    assert mapping.source_id == "generic-ecs"
    assert mapping.index_patterns == [] and mapping.legacy_index_synthesis is True
    assert mapping.timestamp.candidates == ["@timestamp"]
    assert mapping.timestamp.value_type == FieldValueType.DATE_ISO
    assert mapping.message is not None
    assert mapping.message.candidates == ["message", "error.message"]
    assert mapping.service is not None
    assert mapping.service.candidates == ["service.name"]
    assert mapping.severity.strategy == SeverityStrategy.FIELD
    assert mapping.severity.field is not None
    assert mapping.severity.field.candidates == ["log.level"]
    assert set(mapping.identifiers) == {
        "trace_id",
        "span_id",
        "service_name",
        "user_id",
        "session_id",
        "container_id",
    }
    assert mapping.identifiers["trace_id"].candidates == ["labels.trace_id"]
    assert mapping.classification is not None
    assert mapping.classification.candidates == [
        "labels.data_classification",
        "labels.classification",
        "event.classification",
    ]
    assert mapping.code_location is not None
    assert mapping.code_location.function.candidates == ["code.function", "log.origin.function"]
    assert mapping.code_location.file_path is not None
    assert mapping.code_location.file_path.candidates == [
        "code.filepath",
        "code.file.name",
        "log.origin.filepath",
        "log.origin.file.name",
    ]
    assert mapping.sql_statement is not None
    assert mapping.sql_statement.candidates == ["db.statement"]
    assert mapping.sort.field == "@timestamp"
    assert mapping.sort.order == SortOrder.DESC
    assert mapping.sort.format == "epoch_millis"
    assert mapping.sort.unmapped_type == "long"
    assert mapping.tenant_scope.strategy == TenantScopeStrategy.INDEX_PATTERN
    for key in ("@timestamp", "message", "service", "log", "labels", "code", "db"):
        assert key in mapping.top_level_allowlist


def test_observability_profile_mapping_link_defaults():
    from investigation_agent_platform.domain.profile.models import ObservabilityProfile

    profile = ObservabilityProfile(
        provider="elastic",
        indices=["logs-app"],
        timestampField="@timestamp",
        serviceField="service.name",
        environmentField="deployment.environment",
        sessionField="session.id",
        requestField="request.id",
        traceField="trace.id",
        logLevelField="severity",
    )
    assert profile.mapping_source_id == "generic-ecs"
    legacy = profile.model_dump(by_alias=True)
    assert legacy["timestampField"] == "@timestamp"


def test_requests_carry_internal_mapping_source_id():
    from investigation_agent_platform.domain.evidence.requests import (
        RuntimeEvidenceRequest,
        TraceTelemetryRequest,
    )

    assert RuntimeEvidenceRequest(environment="production").mapping_source_id is None
    assert TraceTelemetryRequest(trace_id="abc", environment="production").mapping_source_id is None
    stamped = RuntimeEvidenceRequest(environment="production").model_copy(
        update={"mapping_source_id": "gen-verbose"}
    )
    assert stamped.mapping_source_id == "gen-verbose"


def test_trace_telemetry_support_flags_default_true():
    from investigation_agent_platform.domain.evidence.completeness import EvidenceCompleteness
    from investigation_agent_platform.domain.evidence.telemetry import TraceTelemetry

    telemetry = TraceTelemetry(
        trace_id="abc",
        completeness=EvidenceCompleteness(complete=True, returned_count=0, examined_count=0),
    )
    assert telemetry.code_location_supported is True
    assert telemetry.sql_supported is True
