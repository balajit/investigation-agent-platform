# tests/unit/test_part9_phase2.py
"""Part 9 Phase 2: cursor security/binding, query fingerprint, index scope, fetch scoping."""

import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from investigation_agent_platform.domain.common.exceptions import (
    DomainValidationException,
    EvidenceNotFoundException,
    ExecutionError,
    InvalidCursorException,
    PlatformConfigurationError,
    SecurityPolicyViolationException,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.evidence.requests import RuntimeEvidenceRequest, TimeRange
from investigation_agent_platform.domain.observability.mapping import generic_ecs_mapping
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)
from investigation_agent_platform.infrastructure.evidence.runtime.elastic import (
    AsyncElasticAdapter,
    ElasticAdapterSettings,
    build_index_pattern,
)
from investigation_agent_platform.infrastructure.evidence.runtime.query import normalize_request
from investigation_agent_platform.infrastructure.evidence.runtime.validation import (
    ProviderCeilings,
)

TEST_KEY = b"test-signing-key"
TENANT = "tenant-a"


def _source_mapping():  # type: ignore[no-untyped-def]
    return generic_ecs_mapping()


def _settings(**overrides):  # type: ignore[no-untyped-def]
    kwargs = {"cursor_signing_key": TEST_KEY}
    kwargs.update(overrides)
    return ElasticAdapterSettings(**kwargs)


def _adapter(client=None, **overrides):  # type: ignore[no-untyped-def]
    return AsyncElasticAdapter(client=client or MagicMock(), settings=_settings(**overrides))


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


def _request(**overrides):  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    kwargs = {
        "environment": "production",
        "identifiers": {"trace_id": "abc123"},
        "keywords": ["timeout"],
        "services": ["checkout"],
        "severities": ["ERROR"],
        "time_range": TimeRange(start_time=now - timedelta(minutes=15), end_time=now),
        "limit": 2,
    }
    kwargs.update(overrides)
    return RuntimeEvidenceRequest(**kwargs)


def _hit(doc_id="doc1", index="logs-tenant-a-1-production-1", sort=None):  # type: ignore[no-untyped-def]
    return {
        "_id": doc_id,
        "_index": index,
        "_source": {"@timestamp": "2026-01-01T00:00:00Z", "message": "boom"},
        "sort": sort if sort is not None else [1767225600000, doc_id],
    }


def _search_response(hits, total=None):  # type: ignore[no-untyped-def]
    return {
        "hits": {
            "hits": hits,
            "total": {"value": total if total is not None else len(hits)},
        }
    }


def _mapping(
    tenant_id=TENANT, investigation_id=None, evidence_id=None, source=None, provider="ELASTIC"
):  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    iid = investigation_id or uuid.uuid4()
    return Evidence(
        tenant_id=tenant_id,
        investigation_id=iid,
        evidence_id=evidence_id or uuid.uuid4(),
        evidence_type=EvidenceType.RUNTIME_LOG,
        provider=provider,
        source=source or "elasticsearch://logs-tenant-a-1-production-1/doc1",
        title="t",
        summary="s",
        fingerprint="doc1",
        observed_at=now,
        retrieved_at=now,
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
            source_location=SourceLocation(system="Elasticsearch", identifier="doc1"),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
    )


class _Lookup:
    def __init__(self, evidence=None):  # type: ignore[no-untyped-def]
        self._evidence = evidence
        self.calls = []

    async def get_evidence(self, tenant_id, evidence_id):  # type: ignore[no-untyped-def]
        self.calls.append((tenant_id, evidence_id))
        return self._evidence


# --- Settings / construction ----------------------------------------------------


def test_adapter_requires_signing_key():
    with pytest.raises(PlatformConfigurationError):
        AsyncElasticAdapter(client=MagicMock(), settings=_settings(cursor_signing_key=b""))


def test_settings_from_evidence_config():
    from pydantic import SecretStr

    from investigation_agent_platform.infrastructure.configuration.config import EvidenceConfig

    cfg = EvidenceConfig(
        elasticsearch_url="http://localhost:9200", cursor_signing_key=SecretStr("k")
    )
    settings = ElasticAdapterSettings.from_evidence_config(cfg)
    assert settings.cursor_signing_key == b"k"
    assert settings.max_hits == 200
    assert settings.query_timeout_seconds == 25.0

    with pytest.raises(PlatformConfigurationError):
        ElasticAdapterSettings.from_evidence_config(EvidenceConfig())


# --- Index pattern ---------------------------------------------------------------


def test_index_pattern_valid():
    assert build_index_pattern("tenant-a", "production") == "logs-tenant-a-*-production-*"


@pytest.mark.parametrize("tenant", ["*", "tenant-*", "t/e", "t e", "", "t\n", "../x"])
def test_index_pattern_rejects_bad_tenant(tenant):
    with pytest.raises(SecurityPolicyViolationException):
        build_index_pattern(tenant, "production")


@pytest.mark.parametrize("env", ["*", "prod-*", "p/e", "", "prod\n"])
def test_index_pattern_rejects_bad_environment(env):
    with pytest.raises(SecurityPolicyViolationException):
        build_index_pattern(TENANT, env)


# --- Fingerprint ------------------------------------------------------------------


def test_fingerprint_stable_and_order_insensitive():
    iid = uuid.uuid4()
    now = datetime.now(UTC)
    time_range = TimeRange(start_time=now - timedelta(minutes=15), end_time=now)
    a = normalize_request(
        TENANT,
        iid,
        _request(keywords=["b", "a"], time_range=time_range),
        ProviderCeilings(),
        _source_mapping(),
    )
    b = normalize_request(
        TENANT,
        iid,
        _request(keywords=["a", "b"], time_range=time_range),
        ProviderCeilings(),
        _source_mapping(),
    )
    assert a.fingerprint() == b.fingerprint()
    assert len(a.fingerprint()) == 64


def test_fingerprint_changes_with_query():
    iid = uuid.uuid4()
    now = datetime.now(UTC)
    time_range = TimeRange(start_time=now - timedelta(minutes=15), end_time=now)

    def _fp(**overrides):  # type: ignore[no-untyped-def]
        return normalize_request(
            TENANT,
            iid,
            _request(time_range=time_range, **overrides),
            ProviderCeilings(),
            _source_mapping(),
        ).fingerprint()

    base = _fp()
    assert _fp(services=["other"]) != base
    assert _fp(keywords=["other"]) != base
    assert _fp(environment="staging") != base


# --- Search + cursor round-trip -----------------------------------------------------


@pytest.mark.asyncio
async def test_search_paginates_with_bound_cursor():
    iid = uuid.uuid4()
    client = MagicMock()
    client.search = AsyncMock(return_value=_search_response([_hit("d1"), _hit("d2")], total=5))
    adapter = _adapter(client=client)
    req = _request()
    first = await adapter.search_runtime_evidence(TENANT, iid, req, _profile())
    assert first.has_more is True
    assert first.cursor is not None

    body1 = client.search.call_args.kwargs["body"]
    assert "search_after" not in body1
    assert client.search.call_args.kwargs["index"] == "logs-tenant-a-*-production-*"

    second = await adapter.search_runtime_evidence(
        TENANT, iid, req.model_copy(update={"cursor": first.cursor}), _profile()
    )
    body2 = client.search.call_args.kwargs["body"]
    assert body2["search_after"] == [1767225600000, "d2"]
    assert len(second.items) == 2
    # Provenance carries a real query fingerprint, not a document ID.
    fp = second.items[0].provenance.query_fingerprint.normalized_query_hash
    assert len(fp) == 64 and fp != "d1"


@pytest.mark.asyncio
async def test_generated_body_uses_only_safe_clauses():
    iid = uuid.uuid4()
    client = MagicMock()
    client.search = AsyncMock(return_value=_search_response([_hit("d1")]))
    adapter = _adapter(client=client)
    await adapter.search_runtime_evidence(TENANT, iid, _request(), _profile())
    body = client.search.call_args.kwargs["body"]
    assert set(body) == {"query", "size", "sort", "track_total_hits", "timeout", "_source"}
    assert body["_source"] == {"includes": sorted(generic_ecs_mapping().top_level_allowlist)}
    multi = next(c["multi_match"] for c in body["query"]["bool"]["must"] if "multi_match" in c)
    assert multi["operator"] == "OR"
    assert body["query"]["bool"]["must"][1] == {"terms": {"log.level": ["ERROR"]}}


@pytest.mark.asyncio
async def test_no_cursor_means_first_page():
    iid = uuid.uuid4()
    client = MagicMock()
    client.search = AsyncMock(return_value=_search_response([_hit("d1")]))
    adapter = _adapter(client=client)
    result = await adapter.search_runtime_evidence(TENANT, iid, _request(limit=5), _profile())
    assert result.has_more is False
    assert result.cursor is None


# --- Invalid cursors fail (never fall back) ------------------------------------------


async def _cursor_for(adapter, iid, req):  # type: ignore[no-untyped-def]
    client = adapter._client
    client.search = AsyncMock(return_value=_search_response([_hit("d1"), _hit("d2")]))
    result = await adapter.search_runtime_evidence(TENANT, iid, req, _profile())
    assert result.cursor is not None
    return result.cursor


@pytest.mark.asyncio
async def test_tampered_cursor_rejected_without_provider_call():
    iid = uuid.uuid4()
    client = MagicMock()
    adapter = _adapter(client=client)
    cursor = await _cursor_for(adapter, iid, _request())
    bad = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    client.search = AsyncMock()
    with pytest.raises(InvalidCursorException):
        await adapter.search_runtime_evidence(TENANT, iid, _request(cursor=bad), _profile())
    client.search.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_cursor_rejected():
    iid = uuid.uuid4()
    client = MagicMock()
    client.search = AsyncMock()
    adapter = _adapter(client=client)
    with pytest.raises(InvalidCursorException):
        await adapter.search_runtime_evidence(
            TENANT, iid, _request(cursor="!!!not-a-cursor!!!"), _profile()
        )
    client.search.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        {"services": ["billing"]},
        {"keywords": ["other"]},
        {"identifiers": {"trace_id": "other"}},
        {"severities": ["INFO"]},
        {"environment": "staging"},
    ],
)
async def test_cursor_reused_with_different_query_rejected(mutate):
    iid = uuid.uuid4()
    client = MagicMock()
    adapter = _adapter(client=client)
    cursor = await _cursor_for(adapter, iid, _request())
    client.search = AsyncMock()
    with pytest.raises(InvalidCursorException):
        await adapter.search_runtime_evidence(
            TENANT, iid, _request(cursor=cursor, **mutate), _profile()
        )
    client.search.assert_not_called()


@pytest.mark.asyncio
async def test_cursor_reused_with_different_time_range_rejected():
    iid = uuid.uuid4()
    client = MagicMock()
    adapter = _adapter(client=client)
    cursor = await _cursor_for(adapter, iid, _request())
    now = datetime.now(UTC)
    other_range = TimeRange(start_time=now - timedelta(hours=2), end_time=now - timedelta(hours=1))
    client.search = AsyncMock()
    with pytest.raises(InvalidCursorException):
        await adapter.search_runtime_evidence(
            TENANT, iid, _request(cursor=cursor, time_range=other_range), _profile()
        )
    client.search.assert_not_called()


@pytest.mark.asyncio
async def test_cursor_bound_to_tenant_and_investigation():
    iid = uuid.uuid4()
    client = MagicMock()
    adapter = _adapter(client=client)
    cursor = await _cursor_for(adapter, iid, _request())
    client.search = AsyncMock(return_value=_search_response([_hit("d1")]))
    with pytest.raises(InvalidCursorException):
        await adapter.search_runtime_evidence(
            "other-tenant", iid, _request(cursor=cursor), _profile()
        )
    with pytest.raises(InvalidCursorException):
        await adapter.search_runtime_evidence(
            TENANT, uuid.uuid4(), _request(cursor=cursor), _profile()
        )


@pytest.mark.asyncio
async def test_legacy_unversioned_cursor_rejected():
    iid = uuid.uuid4()
    client = MagicMock()
    adapter = _adapter(client=client)
    legacy_payload = json.dumps(
        {"sort": [1, "d1"], "tenant_id": TENANT, "app_id": str(iid)},
        sort_keys=True,
        separators=(",", ":"),
    )
    sig = hmac.new(TEST_KEY, legacy_payload.encode(), hashlib.sha256).hexdigest()
    legacy = base64.urlsafe_b64encode(
        json.dumps({"p": legacy_payload, "s": sig}, separators=(",", ":")).encode()
    ).decode()
    client.search = AsyncMock()
    with pytest.raises(InvalidCursorException):
        await adapter.search_runtime_evidence(TENANT, iid, _request(cursor=legacy), _profile())
    client.search.assert_not_called()


@pytest.mark.asyncio
async def test_expired_cursor_rejected():
    iid = uuid.uuid4()
    client = MagicMock()
    adapter = _adapter(client=client)
    fp = normalize_request(
        TENANT, iid, _request(), ProviderCeilings(), _source_mapping()
    ).fingerprint()
    now = datetime.now(UTC).timestamp()
    payload = json.dumps(
        {
            "v": 1,
            "tenant_id": TENANT,
            "investigation_id": str(iid),
            "query_fingerprint": fp,
            "sort": [1, "d1"],
            "issued_at": now - 7200,
            "expires_at": now - 3600,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    sig = hmac.new(TEST_KEY, payload.encode(), hashlib.sha256).hexdigest()
    expired = base64.urlsafe_b64encode(
        json.dumps({"p": payload, "s": sig}, separators=(",", ":")).encode()
    ).decode()
    client.search = AsyncMock()
    with pytest.raises(InvalidCursorException):
        await adapter.search_runtime_evidence(TENANT, iid, _request(cursor=expired), _profile())
    client.search.assert_not_called()


# --- Provider-boundary enforcement (independent of domain) -------------------------------


def test_provider_rejects_oversized_filters_bypassing_domain():
    iid = uuid.uuid4()
    base = _request()
    with pytest.raises(SecurityPolicyViolationException):
        normalize_request(
            TENANT,
            iid,
            base.model_copy(update={"keywords": ["x"] * 21}),
            ProviderCeilings(),
            _source_mapping(),
        )
    with pytest.raises(SecurityPolicyViolationException):
        normalize_request(
            TENANT,
            iid,
            base.model_copy(update={"services": ["x"] * 11}),
            ProviderCeilings(),
            _source_mapping(),
        )
    with pytest.raises(DomainValidationException):
        normalize_request(
            TENANT,
            iid,
            base.model_copy(update={"identifiers": {"evil.key": "x"}}),
            ProviderCeilings(),
            _source_mapping(),
        )
    now = datetime.now(UTC)
    wide = TimeRange(start_time=now - timedelta(days=30), end_time=now)
    with pytest.raises(SecurityPolicyViolationException):
        normalize_request(
            TENANT,
            iid,
            base.model_copy(update={"time_range": wide}),
            ProviderCeilings(),
            _source_mapping(),
        )


def test_provider_honors_lowered_operator_ceiling():
    iid = uuid.uuid4()
    base = _request()
    with pytest.raises(SecurityPolicyViolationException):
        normalize_request(
            TENANT,
            iid,
            base.model_copy(update={"keywords": ["x"] * 6}),
            ProviderCeilings(max_keyword_terms=5),
            _source_mapping(),
        )


@pytest.mark.asyncio
async def test_search_rejects_injection_scopes_before_provider_call():
    iid = uuid.uuid4()
    client = MagicMock()
    client.search = AsyncMock()
    adapter = _adapter(client=client)
    with pytest.raises(SecurityPolicyViolationException):
        await adapter.search_runtime_evidence("t-*", iid, _request(), _profile())
    client.search.assert_not_called()


# --- Direct fetch ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_uses_mapping_and_real_investigation():
    iid = UUID(int=7)
    eid = UUID(int=1)
    client = MagicMock()
    client.search = AsyncMock(
        return_value=_search_response([_hit("doc1", "logs-tenant-a-1-production-1")])
    )
    adapter = _adapter(client=client)
    lookup = _Lookup(_mapping(tenant_id=TENANT, investigation_id=iid, evidence_id=eid))
    ev = await adapter.get_runtime_evidence(TENANT, iid, eid, "production", lookup)
    assert ev.investigation_id == iid
    assert ev.provenance.investigation_id == iid
    assert ev.provenance.query_fingerprint.operation == "GET"
    assert len(ev.provenance.query_fingerprint.normalized_query_hash) == 64
    _, kwargs = client.search.call_args
    assert kwargs["index"] == "logs-tenant-a-1-production-1"


@pytest.mark.asyncio
async def test_fetch_unknown_or_foreign_mapping_not_found():
    iid = UUID(int=7)
    client = MagicMock()
    client.search = AsyncMock()
    adapter = _adapter(client=client)
    with pytest.raises(EvidenceNotFoundException):
        await adapter.get_runtime_evidence(TENANT, iid, UUID(int=1), "production", _Lookup(None))
    foreign = _mapping(tenant_id=TENANT, investigation_id=UUID(int=8), evidence_id=UUID(int=1))
    with pytest.raises(EvidenceNotFoundException):
        await adapter.get_runtime_evidence(TENANT, iid, UUID(int=1), "production", _Lookup(foreign))
    client.search.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_non_elastic_mapping_not_found():
    iid = UUID(int=7)
    eid = UUID(int=1)
    client = MagicMock()
    client.search = AsyncMock()
    adapter = _adapter(client=client)
    mapping = _mapping(
        tenant_id=TENANT,
        investigation_id=iid,
        evidence_id=eid,
        source="mcp://result/abc",
        provider="MCP",
    )
    with pytest.raises(EvidenceNotFoundException):
        await adapter.get_runtime_evidence(TENANT, iid, eid, "production", _Lookup(mapping))
    client.search.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_corrupt_mapping_reference_fails_closed():
    iid = UUID(int=7)
    eid = UUID(int=1)
    client = MagicMock()
    client.search = AsyncMock()
    adapter = _adapter(client=client)
    mapping = _mapping(
        tenant_id=TENANT,
        investigation_id=iid,
        evidence_id=eid,
        source="elasticsearch://no-slash-here",
    )
    with pytest.raises(ExecutionError):
        await adapter.get_runtime_evidence(TENANT, iid, eid, "production", _Lookup(mapping))
    client.search.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_out_of_scope_index_not_found():
    iid = UUID(int=7)
    eid = UUID(int=1)
    client = MagicMock()
    client.search = AsyncMock()
    adapter = _adapter(client=client)
    mapping = _mapping(
        tenant_id=TENANT,
        investigation_id=iid,
        evidence_id=eid,
        source="elasticsearch://logs-evil-9/doc1",
    )
    with pytest.raises(EvidenceNotFoundException):
        await adapter.get_runtime_evidence(TENANT, iid, eid, "production", _Lookup(mapping))
    client.search.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_validates_scope_before_lookup():
    iid = UUID(int=7)
    client = MagicMock()
    client.search = AsyncMock()
    adapter = _adapter(client=client)
    lookup = _Lookup(_mapping())
    with pytest.raises(SecurityPolicyViolationException):
        await adapter.get_runtime_evidence("t-*", iid, UUID(int=1), "production", lookup)
    assert lookup.calls == []


@pytest.mark.asyncio
async def test_fetch_empty_hits_not_found():
    iid = UUID(int=7)
    eid = UUID(int=1)
    client = MagicMock()
    client.search = AsyncMock(return_value=_search_response([]))
    adapter = _adapter(client=client)
    lookup = _Lookup(_mapping(tenant_id=TENANT, investigation_id=iid, evidence_id=eid))
    with pytest.raises(EvidenceNotFoundException):
        await adapter.get_runtime_evidence(TENANT, iid, eid, "production", lookup)
