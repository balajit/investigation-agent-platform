# tests/unit/test_part10_phase9.py
"""Part 10 Phase 9: mapping-driven sort contract and cursor arity."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from investigation_agent_platform.domain.common.exceptions import (
    ExecutionError,
    InvalidCursorException,
)
from investigation_agent_platform.domain.evidence.requests import RuntimeEvidenceRequest, TimeRange
from investigation_agent_platform.domain.observability.mapping import generic_ecs_mapping
from investigation_agent_platform.domain.profile.models import ObservabilityProfile
from investigation_agent_platform.infrastructure.configuration.mapping_registry import (
    load_mapping_profiles,
)
from investigation_agent_platform.infrastructure.evidence.runtime.cursor import (
    CURSOR_SORT_ARITY,
    ElasticCursorCodec,
)
from investigation_agent_platform.infrastructure.evidence.runtime.elastic import (
    AsyncElasticAdapter,
    ElasticAdapterSettings,
)
from investigation_agent_platform.infrastructure.evidence.runtime.query import (
    build_query_body,
    normalize_request,
)
from investigation_agent_platform.infrastructure.evidence.runtime.validation import (
    ProviderCeilings,
)

SHIPPED_DIR = Path(__file__).resolve().parents[2] / "config" / "observability-mappings"
TENANT = "tenant-a"


def _settings(**overrides):  # type: ignore[no-untyped-def]
    kwargs = {"cursor_signing_key": b"test-signing-key"}
    kwargs.update(overrides)
    return ElasticAdapterSettings(**kwargs)


def _request(**overrides):  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    kwargs = {
        "environment": "production",
        "time_range": TimeRange(start_time=now - timedelta(minutes=5), end_time=now),
        "limit": 10,
    }
    kwargs.update(overrides)
    return RuntimeEvidenceRequest(**kwargs)


def _hit(doc_id, sort):  # type: ignore[no-untyped-def]
    return {
        "_id": doc_id,
        "_index": "logs-tenant-a-1",
        "_source": {"@timestamp": "2026-01-01T00:00:00Z", "message": "m"},
        "sort": sort,
    }


def _response(hits):  # type: ignore[no-untyped-def]
    return {"hits": {"hits": hits, "total": {"value": len(hits)}}}


def test_sort_clause_comes_from_mapping():
    registry = load_mapping_profiles(SHIPPED_DIR)
    iid = uuid.uuid4()
    ceilings = ProviderCeilings()
    generic = normalize_request(TENANT, iid, _request(), ceilings, generic_ecs_mapping())
    body = build_query_body(
        generic, generic_ecs_mapping(), search_after=None, query_timeout_seconds=25.0
    )
    assert body["sort"][0] == {
        "@timestamp": {"order": "desc", "format": "epoch_millis", "unmapped_type": "long"}
    }
    assert body["sort"][1] == {"_id": "asc"}

    root_cause = registry.resolve("gen-root-cause")
    normalized = normalize_request(TENANT, iid, _request(), ceilings, root_cause)
    body = build_query_body(normalized, root_cause, search_after=None, query_timeout_seconds=25.0)
    # Epoch-long sort field: no date format assumptions, tiebreak preserved.
    assert body["sort"][0] == {"updatedAt": {"order": "desc"}}
    assert body["sort"][1] == {"_id": "asc"}


def test_fingerprint_binds_mapping_and_sort():
    iid = uuid.uuid4()
    ceilings = ProviderCeilings()
    registry = load_mapping_profiles(SHIPPED_DIR)
    base = normalize_request(TENANT, iid, _request(), ceilings, generic_ecs_mapping()).fingerprint()
    other = normalize_request(
        TENANT,
        iid,
        _request().model_copy(update={"mapping_source_id": "gen-verbose"}),
        ceilings,
        registry.resolve("gen-verbose"),
    ).fingerprint()
    # Same logical query, different mapping: different pagination state.
    assert base != other


def test_cursor_arity_is_structurally_two():
    assert CURSOR_SORT_ARITY == 2
    codec = ElasticCursorCodec(signing_key=b"k")
    iid = uuid.uuid4()
    token = codec.encode(
        sort_values=[123, "doc1"],
        tenant_id=TENANT,
        investigation_id=iid,
        query_fingerprint="f" * 64,
    )
    assert codec.decode(
        cursor=token,
        expected_query_fingerprint="f" * 64,
        tenant_id=TENANT,
        investigation_id=iid,
    ) == [123, "doc1"]
    with pytest.raises(ValueError):
        codec.encode(
            sort_values=[123],
            tenant_id=TENANT,
            investigation_id=iid,
            query_fingerprint="f" * 64,
        )
    with pytest.raises(ValueError):
        codec.encode(
            sort_values=[1, "a", "extra"],
            tenant_id=TENANT,
            investigation_id=iid,
            query_fingerprint="f" * 64,
        )


def _craft_token(codec, sort_values, iid):  # type: ignore[no-untyped-def]
    import base64
    import hashlib
    import hmac
    import json

    payload = json.dumps(
        {
            "v": 1,
            "tenant_id": TENANT,
            "investigation_id": str(iid),
            "query_fingerprint": "f" * 64,
            "sort": sort_values,
            "issued_at": 1_700_000_000.0,
            "expires_at": 9_999_999_999.0,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    sig = hmac.new(b"k", payload.encode(), hashlib.sha256).hexdigest()
    token = json.dumps({"p": payload, "s": sig}, separators=(",", ":"))
    return base64.urlsafe_b64encode(token.encode()).decode()


@pytest.mark.parametrize("sort_values", [[123], [1, "a", "extra"], "nope", [None]])
def test_decode_rejects_wrong_arity(sort_values):
    codec = ElasticCursorCodec(signing_key=b"k")
    iid = uuid.uuid4()
    token = _craft_token(codec, sort_values, iid)
    with pytest.raises(InvalidCursorException):
        codec.decode(
            cursor=token,
            expected_query_fingerprint="f" * 64,
            tenant_id=TENANT,
            investigation_id=iid,
        )


@pytest.mark.asyncio
async def test_has_more_without_issuable_cursor_fails_loud():
    iid = uuid.uuid4()
    client = MagicMock()
    # Full page of hits carrying no usable sort state.
    client.search = AsyncMock(return_value=_response([_hit("d1", None), _hit("d2", "not-a-list")]))
    adapter = AsyncElasticAdapter(client=client, settings=_settings())
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
    with pytest.raises(ExecutionError) as exc_info:
        await adapter.search_runtime_evidence(TENANT, iid, _request(limit=2), profile)
    assert exc_info.value.retryable is False
    assert client.search.call_count == 1


@pytest.mark.asyncio
async def test_cross_mapping_cursor_rejected():
    registry = load_mapping_profiles(SHIPPED_DIR)
    iid = uuid.uuid4()
    client = MagicMock()
    client.search = AsyncMock(
        return_value=_response([_hit("d1", [1, "d1"]), _hit("d2", [2, "d2"])])
    )
    adapter = AsyncElasticAdapter(client=client, settings=_settings(), mapping_registry=registry)
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
    first = await adapter.search_runtime_evidence(TENANT, iid, _request(limit=2), profile)
    assert first.cursor is not None
    # Same logical query, different mapping: the fingerprint binds the
    # mapping, so the cursor is rejected instead of resuming a new sort.
    with pytest.raises(InvalidCursorException):
        await adapter.search_runtime_evidence(
            TENANT,
            iid,
            _request(limit=2, cursor=first.cursor, mapping_source_id="gen-verbose"),
            profile,
        )
