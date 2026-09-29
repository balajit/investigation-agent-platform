# tests/unit/test_part9_phase3.py
"""Part 9 Phase 3: timestamps, projection, response validation, typed errors, timeouts."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from elasticsearch import exceptions as elastic_exceptions

from investigation_agent_platform.domain.common.exceptions import (
    ExecutionError,
    PlatformConfigurationError,
    ProviderTimeoutException,
    ProviderUnavailableException,
)
from investigation_agent_platform.domain.evidence.completeness import EvidenceDataQuality
from investigation_agent_platform.domain.evidence.models import ClassificationLevel
from investigation_agent_platform.domain.evidence.requests import RuntimeEvidenceRequest, TimeRange
from investigation_agent_platform.infrastructure.evidence.runtime.elastic import (
    AsyncElasticAdapter,
    ElasticAdapterSettings,
)
from investigation_agent_platform.infrastructure.evidence.runtime.projection import (
    ElasticEvidenceProjector,
    MalformedProviderRecord,
)

TEST_KEY = b"test-signing-key"
TENANT = "tenant-a"


def _settings(**overrides):  # type: ignore[no-untyped-def]
    kwargs = {"cursor_signing_key": TEST_KEY}
    kwargs.update(overrides)
    return ElasticAdapterSettings(**kwargs)


def _adapter(client=None, request_timeout=30, **overrides):  # type: ignore[no-untyped-def]
    return AsyncElasticAdapter(
        client=client or MagicMock(),
        settings=_settings(**overrides),
        request_timeout=request_timeout,
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


def _request(**overrides):  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    kwargs = {
        "environment": "production",
        "identifiers": {"trace_id": "abc123"},
        "time_range": TimeRange(start_time=now - timedelta(minutes=15), end_time=now),
        "limit": 10,
    }
    kwargs.update(overrides)
    return RuntimeEvidenceRequest(**kwargs)


def _hit(source=None, doc_id="doc1", index="logs-tenant-a-1-production-1", **overrides):  # type: ignore[no-untyped-def]
    hit = {"_id": doc_id, "_index": index, "_source": source, "sort": [1, doc_id]}
    hit.update(overrides)
    return hit


def _project(source, **kwargs):  # type: ignore[no-untyped-def]
    iid = kwargs.pop("investigation_id", uuid.uuid4())
    return ElasticEvidenceProjector().project(
        _hit(source),
        TENANT,
        iid,
        operation="SEARCH",
        query_fingerprint="f" * 64,
        **kwargs,
    )


# --- Timestamp policy ---------------------------------------------------------------


def test_valid_timestamps_parsed():
    ev = _project({"@timestamp": "2026-01-01T00:00:00Z", "message": "m"})
    assert ev.observed_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert ev.data_quality == []
    ev = _project({"@timestamp": "2026-01-01T02:00:00+02:00", "message": "m"})
    assert ev.observed_at is not None and ev.observed_at.utcoffset() is not None
    ev = _project({"@timestamp": "2026-01-01T00:00:00-05:00", "message": "m"})
    assert ev.observed_at is not None


def test_missing_timestamp_never_fabricated():
    before = datetime.now(UTC)
    ev = _project({"message": "no timestamp here"})
    assert ev.observed_at is None
    assert EvidenceDataQuality.TIMESTAMP_MISSING in ev.data_quality
    assert ev.retrieved_at >= before
    assert ev.freshness.observed_at is None


def test_malformed_and_naive_timestamps():
    ev = _project({"@timestamp": "not-a-date", "message": "m"})
    assert ev.observed_at is None
    assert EvidenceDataQuality.TIMESTAMP_INVALID in ev.data_quality
    ev = _project({"@timestamp": "2026-01-01T00:00:00", "message": "m"})
    assert ev.observed_at is None
    assert EvidenceDataQuality.TIMESTAMP_NAIVE in ev.data_quality
    ev = _project({"@timestamp": "", "message": "m"})
    assert ev.observed_at is None
    assert EvidenceDataQuality.TIMESTAMP_INVALID in ev.data_quality


# --- Projection: allowlist ------------------------------------------------------------


def test_source_passthrough_blocked():
    ev = _project(
        {
            "@timestamp": "2026-01-01T00:00:00Z",
            "message": "m",
            "credentials": {"password": "hunter2"},
            "huge_blob": "x" * 50000,
            "tenant_id": "evil-override",
            "authorization": "Bearer abc",
        }
    )
    assert "credentials" not in ev.attributes
    assert "huge_blob" not in ev.attributes
    assert "authorization" not in ev.attributes
    # Trusted domain context wins; provider data cannot smuggle tenant identity.
    assert ev.attributes.get("tenant_id") is None
    assert ev.tenant_id == TENANT


def test_investigation_fields_preserved():
    ev = _project(
        {
            "@timestamp": "2026-01-01T00:00:00Z",
            "message": "m",
            "service": {"name": "checkout"},
            "log": {"level": "ERROR", "origin": {"function": "f"}},
            "labels": {"trace_id": "abc", "session_id": "s1"},
            "code": {"function": "g"},
            "db": {"statement": "SELECT 1"},
            "span": {"id": "sp1"},
            "parent": {"id": "p1"},
            "trace": {"id": "abc"},
            "error": {"message": "boom"},
        }
    )
    assert ev.attributes["service"] == {"name": "checkout"}
    assert ev.attributes["labels"]["trace_id"] == "abc"
    assert ev.attributes["db"] == {"statement": "SELECT 1"}
    assert ev.attributes["span"] == {"id": "sp1"}
    assert EvidenceDataQuality.ATTRIBUTES_TRUNCATED not in ev.data_quality


def test_dotted_keys_folded_to_nested():
    ev = _project(
        {
            "@timestamp": "2026-01-01T00:00:00Z",
            "message": "m",
            "span.id": "sp9",
            "service.name": "billing",
        }
    )
    assert ev.attributes["span"] == {"id": "sp9"}
    assert ev.attributes["service"] == {"name": "billing"}


# --- Projection: secret redaction -------------------------------------------------------


def test_secret_keys_redacted_with_manifest():
    ev = _project(
        {
            "@timestamp": "2026-01-01T00:00:00Z",
            "message": "login failed",
            "log": {"origin": {"function": "auth.login"}},
            "db": {"statement": "SELECT 1", "password": "hunter2"},
            "labels": {"trace_id": "abc", "api_token": "tok123"},
            "user": {"name": "alice", "email": "alice@example.com"},
        }
    )
    assert ev.attributes["db"]["password"] == "[REDACTED]"
    assert ev.attributes["db"]["statement"] == "SELECT 1"
    assert ev.attributes["labels"]["api_token"] == "[REDACTED]"
    assert ev.attributes["labels"]["trace_id"] == "abc"
    assert ev.attributes["user"] == {"name": "alice", "email": "[REDACTED]"}
    assert ev.is_redacted is True
    assert len(ev.redaction_manifest) >= 3
    assert "hunter2" not in ev.content_snippet


def test_session_correlation_ids_survive_redaction():
    ev = _project(
        {
            "@timestamp": "2026-01-01T00:00:00Z",
            "message": "m",
            "labels": {"session_id": "sess-1", "user_id": "u-1"},
        }
    )
    assert ev.attributes["labels"] == {"session_id": "sess-1", "user_id": "u-1"}
    assert ev.is_redacted is False


# --- Projection: bounds -----------------------------------------------------------------


def test_oversized_values_bounded_with_flag():
    ev = _project(
        {
            "@timestamp": "2026-01-01T00:00:00Z",
            "message": "m",
            "log": {"body": "y" * 5000},
            "labels": {f"k{i}": "v" for i in range(150)},
            "tags": [f"t{i}" for i in range(150)],
        }
    )
    assert len(ev.attributes["log"]["body"]) == 2000
    assert len(ev.attributes["labels"]) == 100
    assert len(ev.attributes["tags"]) == 100
    assert EvidenceDataQuality.ATTRIBUTES_TRUNCATED in ev.data_quality


def test_giant_payload_falls_back_to_core():
    ev = _project(
        {
            "@timestamp": "2026-01-01T00:00:00Z",
            "message": "m",
            "service": {"name": "checkout"},
            "log": {"data": "z" * 1900, "more": "w" * 1900, "extra": "v" * 1900},
            "labels": {f"k{i}": "x" * 500 for i in range(30)},
        }
    )
    assert EvidenceDataQuality.ATTRIBUTES_TRUNCATED in ev.data_quality
    assert ev.attributes.get("service") == {"name": "checkout"}
    import json as _json

    assert len(_json.dumps(ev.attributes, default=str).encode()) <= 16384


# --- Projection: presentation --------------------------------------------------------------


def test_summary_normalized_and_bounded():
    ev = _project({"@timestamp": "2026-01-01T00:00:00Z", "message": "  line1\n\nline2\x00  "})
    assert ev.summary == "line1 line2"
    ev = _project({"@timestamp": "2026-01-01T00:00:00Z"})
    assert ev.summary == "Runtime log event"
    ev = _project({"@timestamp": "2026-01-01T00:00:00Z", "message": "q" * 5000})
    assert len(ev.summary) == 200


def test_snippet_bounded_and_safe():
    ev = _project(
        {
            "@timestamp": "2026-01-01T00:00:00Z",
            "message": "m" * 5000,
            "service": {"name": "checkout"},
            "log": {"level": "ERROR"},
            "labels": {"trace_id": "abc", "password": "hunter2"},
        }
    )
    assert len(ev.content_snippet) <= 2000
    assert "hunter2" not in ev.content_snippet
    assert "checkout" in ev.content_snippet


def test_identity_and_source_location():
    iid = uuid.uuid4()
    projector = ElasticEvidenceProjector()
    first = projector.project(
        _hit({"@timestamp": "2026-01-01T00:00:00Z"}), TENANT, iid, "SEARCH", "f" * 64
    )
    second = projector.project(
        _hit({"@timestamp": "2026-01-02T00:00:00Z"}), TENANT, iid, "SEARCH", "f" * 64
    )
    assert first.evidence_id == second.evidence_id
    assert first.fingerprint == "doc1"
    assert first.provenance.source_location.identifier == "logs-tenant-a-1-production-1/doc1"
    assert first.source == "elasticsearch://logs-tenant-a-1-production-1/doc1"


def test_classification_policy():
    ev = _project({"@timestamp": "2026-01-01T00:00:00Z", "message": "m"})
    assert ev.classification == ClassificationLevel.INTERNAL
    ev = _project(
        {
            "@timestamp": "2026-01-01T00:00:00Z",
            "message": "m",
            "labels": {"data_classification": "secret"},
        }
    )
    assert ev.classification == ClassificationLevel.SECRET
    ev = _project(
        {
            "@timestamp": "2026-01-01T00:00:00Z",
            "message": "m",
            "labels": {"classification": "nonsense"},
        }
    )
    assert ev.classification == ClassificationLevel.INTERNAL


# --- Malformed records ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "hit",
    [
        {"_index": "i", "_source": {}},
        {"_id": "d", "_source": {}},
        {"_id": "d", "_index": "i", "_source": None},
        {"_id": "d", "_index": "i", "_source": "nope"},
        {"_id": "d", "_index": "i", "_source": ["x"]},
        {"_id": "", "_index": "i", "_source": {}},
        "not-a-dict",
        None,
    ],
)
def test_malformed_hits_rejected(hit):
    with pytest.raises(MalformedProviderRecord):
        ElasticEvidenceProjector().project(hit, TENANT, uuid.uuid4(), "SEARCH", "f" * 64)


def test_missing_source_maps_with_quality_flag():
    hit = {"_id": "d", "_index": "i"}
    ev = ElasticEvidenceProjector().project(hit, TENANT, uuid.uuid4(), "SEARCH", "f" * 64)
    assert ev.attributes == {}
    assert EvidenceDataQuality.TIMESTAMP_MISSING in ev.data_quality


@pytest.mark.asyncio
async def test_search_skips_malformed_with_partial_signal():
    iid = uuid.uuid4()
    client = MagicMock()
    good = _hit({"@timestamp": "2026-01-01T00:00:00Z", "message": "ok"}, doc_id="good")
    bad = {"_index": "logs-tenant-a-1", "_source": {"message": "no id"}}
    client.search = AsyncMock(return_value={"hits": {"hits": [good, bad], "total": {"value": 2}}})
    adapter = _adapter(client=client)
    result = await adapter.search_runtime_evidence(TENANT, iid, _request(), _profile())
    assert len(result.items) == 1
    assert result.execution_metadata["partial_result"] is True
    (entry,) = result.execution_metadata["errors"]
    assert entry["code"] == "MALFORMED_PROVIDER_RECORD"
    assert entry["reason"] == "missing_document_id"


@pytest.mark.asyncio
async def test_malformed_envelope_fails_page():
    iid = uuid.uuid4()
    client = MagicMock()
    adapter = _adapter(client=client)
    for bad_response in (None, [], {"hits": {"hits": "nope"}}, {"hits": []}):
        client.search = AsyncMock(return_value=bad_response)
        with pytest.raises(ExecutionError):
            await adapter.search_runtime_evidence(TENANT, iid, _request(), _profile())


# --- Typed provider errors ---------------------------------------------------------------------


def _meta(status):
    from elastic_transport import ApiResponseMeta

    return ApiResponseMeta(status=status, http_version="1.1", headers={}, duration=0.0, node=None)


def _auth_error():
    try:
        raise elastic_exceptions.AuthenticationException(
            "bad creds", _meta(401), {"error": "unauthorized"}
        )
    except elastic_exceptions.AuthenticationException as exc:
        return exc
    raise AssertionError("unreachable")


def _api_error(status):
    return elastic_exceptions.ApiError("provider failure", _meta(status), {"error": "x"})


@pytest.mark.asyncio
async def test_provider_error_taxonomy():
    iid = uuid.uuid4()
    cases = [
        (elastic_exceptions.ConnectionTimeout("slow"), ProviderTimeoutException, True),
        (elastic_exceptions.ConnectionError("down"), ProviderUnavailableException, True),
        (_api_error(503), ProviderUnavailableException, True),
        (_api_error(400), ExecutionError, False),
        (ValueError("weird"), ExecutionError, True),
    ]
    for exc, expected, retryable in cases:
        client = MagicMock()
        client.search = AsyncMock(side_effect=exc)
        adapter = _adapter(client=client)
        with pytest.raises(expected) as caught:
            await adapter.search_runtime_evidence(TENANT, iid, _request(), _profile())
        assert caught.value.retryable is retryable
        assert "https://es:9200" not in str(caught.value)

    client = MagicMock()
    client.search = AsyncMock(side_effect=_auth_error())
    adapter = _adapter(client=client)
    with pytest.raises(ExecutionError) as caught:
        await adapter.search_runtime_evidence(TENANT, iid, _request(), _profile())
    assert caught.value.retryable is False
    assert "bad creds" not in str(caught.value)


@pytest.mark.asyncio
async def test_asyncio_timeout_maps_to_provider_timeout():
    iid = uuid.uuid4()
    client = MagicMock()
    client.search = AsyncMock(side_effect=TimeoutError("outer"))
    adapter = _adapter(client=client)
    with pytest.raises(ProviderTimeoutException):
        await adapter.search_runtime_evidence(TENANT, iid, _request(), _profile())


@pytest.mark.asyncio
async def test_cancellation_propagates_unmapped():
    iid = uuid.uuid4()
    client = MagicMock()
    client.search = AsyncMock(side_effect=asyncio.CancelledError())
    adapter = _adapter(client=client)
    with pytest.raises(asyncio.CancelledError):
        await adapter.search_runtime_evidence(TENANT, iid, _request(), _profile())


# --- Timeout trio ---------------------------------------------------------------------------------


def test_timeout_trio_validated_at_construction():
    with pytest.raises(PlatformConfigurationError):
        _adapter(query_timeout_seconds=30.0, request_timeout=30)
    with pytest.raises(PlatformConfigurationError):
        _settings(query_timeout_seconds=60.0)
        AsyncElasticAdapter(
            client=MagicMock(), settings=_settings(query_timeout_seconds=60.0), request_timeout=30
        )
    _adapter(query_timeout_seconds=10.0, request_timeout=30)


@pytest.mark.asyncio
async def test_query_timeout_reaches_provider_body():
    iid = uuid.uuid4()
    client = MagicMock()
    client.search = AsyncMock(
        return_value={"hits": {"hits": [_hit({"@timestamp": "2026-01-01T00:00:00Z"})]}}
    )
    adapter = _adapter(client=client, query_timeout_seconds=10.5)
    await adapter.search_runtime_evidence(TENANT, iid, _request(), _profile())
    assert client.search.call_args.kwargs["body"]["timeout"] == "10.5s"
