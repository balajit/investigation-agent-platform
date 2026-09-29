# tests/unit/test_part10_phase6.py
"""Part 10 Phase 6: gated trace extraction, support flags, bridge stamping, MCP flags."""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.profile.models import ObservabilityProfile
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)
from investigation_agent_platform.infrastructure.configuration.mapping_registry import (
    load_mapping_profiles,
)
from investigation_agent_platform.infrastructure.evidence.logs.elastic_bridge import (
    ElasticIncidentBridge,
)
from investigation_agent_platform.infrastructure.evidence.logs.trace_telemetry import (
    TraceTelemetryExtractor,
)
from investigation_agent_platform.ports.evidence.gateway import EvidenceQueryResult

SHIPPED_DIR = Path(__file__).resolve().parents[2] / "config" / "observability-mappings"


def _profile(mapping_source_id="generic-ecs"):  # type: ignore[no-untyped-def]
    return ObservabilityProfile(
        provider="elastic",
        indices=["logs-app"],
        timestampField="@timestamp",
        serviceField="service.name",
        environmentField="deployment.environment",
        sessionField="session.id",
        requestField="request.id",
        traceField="trace.id",
        logLevelField="severity",
        mappingSourceId=mapping_source_id,
    )


def _evidence(attrs, iid=None):  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    iid = iid or uuid.uuid4()
    return Evidence(
        tenant_id="tenant-a",
        investigation_id=iid,
        evidence_type=EvidenceType.RUNTIME_LOG,
        provider="ELASTIC",
        source="elasticsearch://idx/doc",
        title="t",
        summary="s",
        fingerprint="fp",
        observed_at=now,
        retrieved_at=now,
        attributes=attrs,
        provenance=EvidenceProvenance(
            tenant_id="tenant-a",
            investigation_id=iid,
            provider_type="ELASTIC",
            requested_provider_id="elastic-primary",
            actual_provider_id="elastic-primary",
            source_system="Elasticsearch",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="ELASTIC", operation="SEARCH", normalized_query_hash="f" * 64
            ),
            source_location=SourceLocation(system="Elasticsearch", identifier="idx/doc"),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
    )


def _extractor(**overrides):  # type: ignore[no-untyped-def]
    kwargs = {"mapping_registry": load_mapping_profiles(SHIPPED_DIR)}
    kwargs.update(overrides)
    return TraceTelemetryExtractor(**kwargs)


def test_unsupported_source_reports_flags_not_silent_empty():
    extractor = _extractor()
    items = [
        _evidence(
            {
                "@timestamp": "2026-01-01T00:00:00Z",
                "message": "timeout",
                "appName": "checkout",
                "traceId": "abc",
            }
        )
    ]
    telemetry = extractor.extract_from_items(
        trace_id="abc",
        items=items,
        skipped=0,
        has_more=False,
        max_evidence=100,
        mapping_source_id="gen-verbose",
    )
    assert telemetry.code_locations == []
    assert telemetry.sql_statements == []
    assert telemetry.code_location_supported is False
    assert telemetry.sql_supported is False
    assert telemetry.completeness.complete is True


def test_supported_but_empty_stays_distinguishable():
    extractor = _extractor()
    items = [_evidence({"message": "no code here"})]
    telemetry = extractor.extract_from_items(
        trace_id="abc",
        items=items,
        skipped=0,
        has_more=False,
        max_evidence=100,
        mapping_source_id=None,
    )
    assert telemetry.code_locations == []
    assert telemetry.code_location_supported is True
    assert telemetry.sql_supported is True


def test_mapped_code_extraction_from_gen_shaped_attributes():
    # gen-verbose has no code mapping: extraction stays skipped even when
    # ECS-shaped code keys are present (mapping wins over shape sniffing).
    extractor = _extractor()
    items = [
        _evidence(
            {
                "code": {"function": "f", "filepath": "/a.py", "lineno": 3},
                "traceId": "abc",
            }
        )
    ]
    telemetry = extractor.extract_from_items(
        trace_id="abc",
        items=items,
        skipped=0,
        has_more=False,
        max_evidence=100,
        mapping_source_id="gen-verbose",
    )
    assert telemetry.code_locations == []
    assert telemetry.code_location_supported is False


def test_unknown_mapping_id_fails_closed_before_derivation():
    from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError

    extractor = _extractor()
    with pytest.raises(PlatformConfigurationError) as exc_info:
        extractor.extract_from_items(
            trace_id="abc",
            items=[],
            skipped=0,
            has_more=False,
            max_evidence=100,
            mapping_source_id="no-such-source",
        )
    assert "no-such-source" in str(exc_info.value)


def test_named_mapping_without_registry_fails_closed():
    from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError

    extractor = TraceTelemetryExtractor()
    with pytest.raises(PlatformConfigurationError):
        extractor.extract_from_items(
            trace_id="abc",
            items=[],
            skipped=0,
            has_more=False,
            max_evidence=100,
            mapping_source_id="gen-verbose",
        )


@pytest.mark.asyncio
async def test_bridge_stamps_mapping_id_from_profile():
    provider = MagicMock()
    provider.search_runtime_evidence = AsyncMock(
        return_value=EvidenceQueryResult(items=[], has_more=False, total_count=0)
    )
    bridge = ElasticIncidentBridge(
        provider,  # type: ignore[arg-type]
        mapping_registry=load_mapping_profiles(SHIPPED_DIR),
    )
    telemetry = await bridge.extract_runtime_telemetry_for_trace(
        "tenant-a",
        uuid.uuid4(),
        "abc",
        _profile("gen-verbose"),
        environment="production",
    )
    (_, kwargs) = provider.search_runtime_evidence.call_args
    assert kwargs["request"].mapping_source_id == "gen-verbose"
    assert telemetry.code_location_supported is False
    assert telemetry.sql_supported is False


@pytest.mark.asyncio
async def test_bridge_default_profile_resolves_generic_ecs():
    provider = MagicMock()
    provider.search_runtime_evidence = AsyncMock(
        return_value=EvidenceQueryResult(items=[], has_more=False, total_count=0)
    )
    bridge = ElasticIncidentBridge(provider)  # type: ignore[arg-type]
    telemetry = await bridge.extract_runtime_telemetry_for_trace(
        "tenant-a", uuid.uuid4(), "abc", _profile(), environment="production"
    )
    (_, kwargs) = provider.search_runtime_evidence.call_args
    assert kwargs["request"].mapping_source_id == "generic-ecs"
    assert telemetry.code_location_supported is True


def test_mcp_output_carries_support_flags():
    from investigation_agent_platform.mcp.schemas.trace import InvestigateTraceOutput

    extractor = _extractor()
    telemetry = extractor.extract_from_items(
        trace_id="abc",
        items=[_evidence({"traceId": "abc"})],
        skipped=0,
        has_more=False,
        max_evidence=100,
        mapping_source_id="gen-verbose",
    )
    output = InvestigateTraceOutput.from_domain(telemetry, [])
    assert output.code_location_supported is False
    assert output.sql_supported is False
    assert output.code_locations == []
