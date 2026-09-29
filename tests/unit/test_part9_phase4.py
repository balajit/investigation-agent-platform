# tests/unit/test_part9_phase4.py
"""Part 9 Phase 4: SQL extraction, trace telemetry, completeness, bridge shim."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from pydantic import ValidationError

from investigation_agent_platform.domain.evidence.completeness import EvidenceCompleteness
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.evidence.requests import TraceTelemetryRequest
from investigation_agent_platform.domain.evidence.telemetry import SqlParseStatus
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)
from investigation_agent_platform.infrastructure.evidence.logs.trace_telemetry import (
    MAX_SQL_STATEMENT_CHARS,
    SqlEvidenceExtractor,
    TraceTelemetryExtractor,
)
from investigation_agent_platform.ports.evidence.gateway import EvidenceQueryResult

TENANT = "tenant-a"


def _evidence(attrs, tenant_id=TENANT, investigation_id=None, evidence_id=None):  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    iid = investigation_id or uuid.uuid4()
    return Evidence(
        tenant_id=tenant_id,
        investigation_id=iid,
        evidence_id=evidence_id or uuid.uuid4(),
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
            tenant_id=tenant_id,
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


def _profile():  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.profile.models import ObservabilityProfile

    return ObservabilityProfile(
        provider="elastic",
        indices=["logs-tenant-a-*-production-*"],
        timestampField="@timestamp",
        serviceField="service.name",
        environmentField="environment",
        sessionField="session.id",
        requestField="request.id",
        traceField="trace.id",
        logLevelField="log.level",
    )


def _extractor(items=None, has_more=False, errors=None):  # type: ignore[no-untyped-def]
    adapter = MagicMock()
    adapter.search_runtime_evidence = AsyncMock(
        return_value=EvidenceQueryResult(
            items=items or [],
            has_more=has_more,
            total_count=len(items or []),
            execution_metadata={
                "execution_time_ms": 0.0,
                "partial_result": bool(errors),
                "errors": errors or [],
                "tokens_consumed": 0,
            },
        )
    )
    return TraceTelemetryExtractor(runtime_provider=adapter), adapter


def _trace_request(**overrides):  # type: ignore[no-untyped-def]
    kwargs = {"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736", "environment": "production"}
    kwargs.update(overrides)
    return TraceTelemetryRequest(**kwargs)


# --- TraceTelemetryRequest ------------------------------------------------------------


def test_trace_request_validation():
    TraceTelemetryRequest(trace_id="abc-123:X.Y", environment="production", max_evidence=100)
    with pytest.raises(ValidationError):
        TraceTelemetryRequest(trace_id="has space", environment="production")
    with pytest.raises(ValidationError):
        TraceTelemetryRequest(trace_id="a*b", environment="production")
    with pytest.raises(ValidationError):
        TraceTelemetryRequest(trace_id="ok", environment="production", max_evidence=0)
    with pytest.raises(ValidationError):
        TraceTelemetryRequest(trace_id="ok", environment="production", max_evidence=101)


# --- SqlEvidenceExtractor -----------------------------------------------------------------


def test_single_statement_tables_and_redaction():
    ext = SqlEvidenceExtractor()
    eid = uuid.uuid4()
    result = ext.extract(
        evidence_id=eid,
        sql="SELECT id, status FROM payments.transactions WHERE id = 123 AND note = 'hi'",
        dialect="postgres",
    )
    assert result.observed == 1 and result.parsed == 1 and result.parse_failures == 0
    (stmt,) = result.statements
    assert stmt.evidence_id == eid
    assert stmt.parse_status == SqlParseStatus.PARSED
    assert stmt.truncated is False
    assert "123" not in stmt.statement and "hi" not in stmt.statement
    # Literals become parameter placeholders (dialect rendering varies).
    assert "?" in stmt.statement or "%s" in stmt.statement
    (table,) = result.tables
    assert (table.catalog, table.schema, table.table) == (None, "payments", "transactions")


def test_multi_statement_field():
    ext = SqlEvidenceExtractor()
    result = ext.extract(
        evidence_id=uuid.uuid4(), sql="SELECT a FROM t1; SELECT b FROM s2.t2", dialect=None
    )
    assert result.parsed == 2 and result.parse_failures == 0
    assert len(result.statements) == 2
    assert all(s.parse_status == SqlParseStatus.PARSED for s in result.statements)
    assert {(t.schema, t.table) for t in result.tables} == {(None, "t1"), ("s2", "t2")}


def test_garbage_sql_records_failure_without_leak():
    ext = SqlEvidenceExtractor()
    raw = "SELECT FROM WHERE ((( 'secret-value-123'"
    result = ext.extract(evidence_id=uuid.uuid4(), sql=raw, dialect=None)
    assert result.parsed == 0 and result.parse_failures >= 1
    assert result.tables == ()
    for stmt in result.statements:
        assert stmt.parse_status == SqlParseStatus.FAILED
        assert "secret-value-123" not in stmt.statement


def test_mixed_field_partial_status():
    ext = SqlEvidenceExtractor()
    result = ext.extract(evidence_id=uuid.uuid4(), sql="SELECT 1; garbage (((", dialect=None)
    assert result.parsed == 1 and result.parse_failures == 1
    statuses = {s.parse_status for s in result.statements}
    assert statuses == {SqlParseStatus.PARTIAL, SqlParseStatus.FAILED}


def test_oversized_sql_bounded_and_flagged():
    ext = SqlEvidenceExtractor()
    result = ext.extract(evidence_id=uuid.uuid4(), sql="SELECT " + "x" * 25000, dialect=None)
    for stmt in result.statements:
        assert len(stmt.statement) <= MAX_SQL_STATEMENT_CHARS
        assert stmt.truncated is True


def test_empty_sql_rejected():
    ext = SqlEvidenceExtractor()
    with pytest.raises(ValueError):
        ext.extract(evidence_id=uuid.uuid4(), sql="   ", dialect=None)


def test_quoted_identifiers_keep_case_and_qualification():
    ext = SqlEvidenceExtractor()
    result = ext.extract(
        evidence_id=uuid.uuid4(), sql='SELECT * FROM "MySchema"."MyTable"', dialect="postgres"
    )
    assert result.parsed == 1
    (table,) = result.tables
    assert (table.schema, table.table) == ("MySchema", "MyTable")


def test_ddl_parses_without_executing():
    ext = SqlEvidenceExtractor()
    result = ext.extract(evidence_id=uuid.uuid4(), sql="SELECT 1; DROP TABLE legacy", dialect=None)
    assert result.parsed == 2
    assert {t.table for t in result.tables} == {"legacy"}


# --- TraceTelemetryExtractor: derivation ---------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_trace_is_complete():
    iid = uuid.uuid4()
    extractor, _ = _extractor(items=[])
    result = await extractor.extract(
        tenant_id=TENANT, investigation_id=iid, request=_trace_request(), profile=_profile()
    )
    assert result.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert result.code_locations == [] and result.tables == [] and result.sql_statements == []
    assert result.evidence_count == 0
    assert result.completeness.complete is True


@pytest.mark.asyncio
async def test_single_event_derivation_with_evidence_refs():
    iid = uuid.uuid4()
    eid = uuid.uuid4()
    item = _evidence(
        {
            "code": {"function": "checkout.submit", "filepath": "/app/checkout.py", "lineno": 142},
            "db": {"statement": "SELECT id FROM payments.transactions WHERE id = 7"},
        },
        investigation_id=iid,
        evidence_id=eid,
    )
    extractor, adapter = _extractor(items=[item])
    result = await extractor.extract(
        tenant_id=TENANT, investigation_id=iid, request=_trace_request(), profile=_profile()
    )
    (loc,) = result.code_locations
    assert (loc.function, loc.file_path, loc.line) == ("checkout.submit", "/app/checkout.py", 142)
    assert loc.evidence_ids == [eid]
    (table,) = result.tables
    assert (table.schema_, table.table) == ("payments", "transactions")
    # Wire format still uses the contract key.
    assert table.model_dump(by_alias=True)["schema"] == "payments"
    assert table.evidence_ids == [eid]
    (stmt,) = result.sql_statements
    assert stmt.evidence_id == eid and stmt.parse_status == SqlParseStatus.PARSED
    assert result.sql_observed == 1 and result.sql_parsed == 1 and result.sql_parse_failures == 0
    assert result.evidence_count == 1 and result.malformed_section_count == 0
    assert result.completeness.complete is True
    # Request scoped to the trace with the extractor budget as the limit.
    _, kwargs = adapter.search_runtime_evidence.call_args
    assert kwargs["request"].identifiers == {"trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"}
    assert kwargs["request"].limit == 100


@pytest.mark.asyncio
async def test_duplicate_locations_deduplicated_with_refs():
    iid = uuid.uuid4()
    eids = [uuid.uuid4() for _ in range(3)]
    attrs = {"code": {"function": "f", "filepath": "/a.py", "lineno": 1}}
    items = [_evidence(dict(attrs), investigation_id=iid, evidence_id=eid) for eid in eids]
    extractor, _ = _extractor(items=items)
    result = await extractor.extract(
        tenant_id=TENANT, investigation_id=iid, request=_trace_request(), profile=_profile()
    )
    (loc,) = result.code_locations
    assert loc.evidence_ids == sorted(eids)


@pytest.mark.asyncio
async def test_ecs_variants_and_line_validation():
    iid = uuid.uuid4()
    items = [
        _evidence(
            {"log": {"origin": {"function": "g", "file": {"name": "/b.py", "line": 9}}}},
            investigation_id=iid,
        ),
        _evidence({"code": {"function": "h", "lineno": -5}}, investigation_id=iid),
        _evidence({"code": {"function": "i", "lineno": "12"}}, investigation_id=iid),
        _evidence({"code": {"function": "j", "lineno": True}}, investigation_id=iid),
    ]
    extractor, _ = _extractor(items=items)
    result = await extractor.extract(
        tenant_id=TENANT, investigation_id=iid, request=_trace_request(), profile=_profile()
    )
    by_func = {loc.function: loc for loc in result.code_locations}
    assert by_func["g"].line == 9 and by_func["g"].file_path == "/b.py"
    assert by_func["h"].line is None
    assert by_func["i"].line is None
    assert by_func["j"].line is None


@pytest.mark.asyncio
async def test_malformed_sections_counted_not_hidden():
    iid = uuid.uuid4()
    items = [
        _evidence({"log": "not-a-dict"}, investigation_id=iid),
        _evidence({"db": ["not", "a", "dict"]}, investigation_id=iid),
        _evidence({"db": {"statement": 123}}, investigation_id=iid),
        _evidence({"message": "no sections at all"}, investigation_id=iid),
    ]
    extractor, _ = _extractor(items=items)
    result = await extractor.extract(
        tenant_id=TENANT, investigation_id=iid, request=_trace_request(), profile=_profile()
    )
    assert result.malformed_section_count == 3
    assert result.code_locations == [] and result.sql_statements == []


@pytest.mark.asyncio
async def test_overlong_function_degrades_with_flag():
    iid = uuid.uuid4()
    item = _evidence({"code": {"function": "f" * 600, "lineno": 3}}, investigation_id=iid)
    extractor, _ = _extractor(items=[item])
    result = await extractor.extract(
        tenant_id=TENANT, investigation_id=iid, request=_trace_request(), profile=_profile()
    )
    # Garbage function degrades to None, but the observed line is preserved
    # honestly and the corruption is flagged.
    (loc,) = result.code_locations
    assert loc.function is None and loc.line == 3
    assert result.malformed_section_count == 1


@pytest.mark.asyncio
async def test_sql_secrets_redacted_in_telemetry():
    iid = uuid.uuid4()
    item = _evidence(
        {"db": {"statement": "SELECT * FROM users WHERE password = 'hunter2'"}},
        investigation_id=iid,
    )
    extractor, _ = _extractor(items=[item])
    result = await extractor.extract(
        tenant_id=TENANT, investigation_id=iid, request=_trace_request(), profile=_profile()
    )
    (stmt,) = result.sql_statements
    assert "hunter2" not in stmt.statement
    assert stmt.parse_status == SqlParseStatus.PARSED


# --- Completeness ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_truncation_reasons():
    iid = uuid.uuid4()
    items = [_evidence({"message": f"m{i}"}, investigation_id=iid) for i in range(100)]
    extractor, _ = _extractor(items=items, has_more=True)
    result = await extractor.extract(
        tenant_id=TENANT,
        investigation_id=iid,
        request=_trace_request(max_evidence=100),
        profile=_profile(),
    )
    assert result.completeness.complete is False
    assert result.completeness.truncated is True
    assert result.completeness.reason == "application_limit"

    extractor, _ = _extractor(items=items[:40], has_more=True)
    result = await extractor.extract(
        tenant_id=TENANT,
        investigation_id=iid,
        request=_trace_request(max_evidence=100),
        profile=_profile(),
    )
    assert result.completeness.reason == "provider_limit"


@pytest.mark.asyncio
async def test_partial_provider_data_marks_incomplete():
    iid = uuid.uuid4()
    item = _evidence({"message": "ok"}, investigation_id=iid)
    errors = [{"code": "MALFORMED_PROVIDER_RECORD", "position": 1, "reason": "missing_document_id"}]
    extractor, _ = _extractor(items=[item], errors=errors)
    result = await extractor.extract(
        tenant_id=TENANT, investigation_id=iid, request=_trace_request(), profile=_profile()
    )
    assert result.evidence_count == 2
    assert result.skipped_record_count == 1
    assert result.completeness.complete is False
    assert result.completeness.truncated is False
    assert result.completeness.reason == "metadata_incomplete"


@pytest.mark.asyncio
async def test_result_lists_bounded():
    iid = uuid.uuid4()
    items = [
        _evidence({"code": {"function": f"f{i}", "lineno": 1}}, investigation_id=iid)
        for i in range(105)
    ]
    extractor, _ = _extractor(items=items)
    result = await extractor.extract(
        tenant_id=TENANT, investigation_id=iid, request=_trace_request(), profile=_profile()
    )
    assert len(result.code_locations) == 100
    # Deterministic order: sorted by function name.
    assert [loc.function for loc in result.code_locations] == sorted(
        loc.function for loc in result.code_locations
    )


@pytest.mark.asyncio
async def test_provider_failure_propagates():
    iid = uuid.uuid4()
    adapter = MagicMock()
    adapter.search_runtime_evidence = AsyncMock(side_effect=TimeoutError("slow"))
    extractor = TraceTelemetryExtractor(runtime_provider=adapter)
    with pytest.raises(TimeoutError):
        await extractor.extract(
            tenant_id=TENANT, investigation_id=iid, request=_trace_request(), profile=_profile()
        )


# --- Bridge shim --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bridge_returns_telemetry_contract():
    from investigation_agent_platform.infrastructure.evidence.logs.elastic_bridge import (
        ElasticIncidentBridge,
    )

    iid = uuid.uuid4()
    item = _evidence(
        {"code": {"function": "f", "lineno": 1}, "db": {"statement": "SELECT 1 FROM t"}},
        investigation_id=iid,
    )
    adapter = MagicMock()
    adapter.search_runtime_evidence = AsyncMock(
        return_value=EvidenceQueryResult(items=[item], has_more=False, total_count=1)
    )
    bridge = ElasticIncidentBridge(adapter)
    result = await bridge.extract_runtime_telemetry_for_trace(
        TENANT, iid, "trace-1", _profile(), environment="production", time_range=None
    )
    assert result.trace_id == "trace-1"
    assert len(result.code_locations) == 1
    assert result.completeness.complete is True
    assert isinstance(result.completeness, EvidenceCompleteness)


@pytest.mark.asyncio
async def test_bridge_rejects_invalid_trace_id():
    from investigation_agent_platform.infrastructure.evidence.logs.elastic_bridge import (
        ElasticIncidentBridge,
    )

    bridge = ElasticIncidentBridge(MagicMock())
    with pytest.raises(ValidationError):
        await bridge.extract_runtime_telemetry_for_trace(
            TENANT, UUID(int=1), "not a trace id!", _profile()
        )
