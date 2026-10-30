# tests/evaluation/test_mcp_fanout.py
"""P3-2: QUERY_STATE fans out across MCP state providers with per-source degrade."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from investigation_agent_platform.application.evidence.gateway import (
    AsyncEvidenceGateway,
    EvidenceDeduplicator,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.evidence.requests import ApplicationStateRequest
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)
from investigation_agent_platform.ports.evidence.gateway import EvidenceQueryResult


def _ev(source="mcp-a"):
    iid = uuid4()
    now = datetime.now(UTC)
    return Evidence(
        tenant_id="tenant-a",
        investigation_id=iid,
        evidence_type=EvidenceType.DATABASE_STATE,
        provider=source,
        source=f"urn:{source}",
        title="t",
        summary="s",
        fingerprint=f"fp-{source}-{uuid4()}",
        observed_at=now,
        retrieved_at=now,
        provenance=EvidenceProvenance(
            tenant_id="tenant-a",
            investigation_id=iid,
            provider_type="test",
            requested_provider_id="test",
            actual_provider_id="test",
            source_system="test",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="test", operation="test", normalized_query_hash="abc"
            ),
            source_location=SourceLocation(system="test", identifier="x"),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
    )


class _Provider:
    def __init__(self, key, items=None, fail=False):
        self._key = key
        self._items = items or []
        self._fail = fail
        self.calls = 0

    def serves_tenant(self, tenant_id):
        return True

    async def get_application_state(self, tenant_id, investigation_id, request, profile):
        self.calls += 1
        if self._fail:
            raise RuntimeError("source down")
        return EvidenceQueryResult(items=list(self._items), total_count=len(self._items))

    async def search_application_state(self, tenant_id, investigation_id, request, profile):
        return await self.get_application_state(tenant_id, investigation_id, request, profile)


def _gateway(providers, timeout_seconds=30.0, fanout_concurrency=None):
    from investigation_agent_platform.application.evidence.selector import (
        EvidenceProviderSelector,
    )

    sel = EvidenceProviderSelector()
    primary = None
    for key, provider in providers:
        sel.register_state_provider(key, provider)
        if key == "pg-primary":
            primary = provider
    # Mirror production: registry fallback resolves the primary.
    sel.attach_datasource_registry(SimpleNamespace(resolve_default=AsyncMock(return_value=primary)))
    gw = AsyncEvidenceGateway(
        provider_selector=sel,
        timeout_seconds=timeout_seconds,
        fanout_concurrency=fanout_concurrency,
        query_safety_policy=AsyncMock(),
        sanitizer=AsyncMock(**{"sanitize_evidence.side_effect": lambda t, e: e}),
        authorizer=AsyncMock(),
        capability_registry=AsyncMock(),
        profile_repo=AsyncMock(),
    )
    gw._enforce_authorization = AsyncMock()
    gw._effective_profile = AsyncMock(return_value=SimpleNamespace(id="app"))
    deduplicator = AsyncMock(spec=EvidenceDeduplicator)
    deduplicator.deduplicate.side_effect = lambda tenant_id, items, investigation_id=None: items
    gw._deduplicator = deduplicator
    return gw


def _req():
    return ApplicationStateRequest(
        environment="development", template_id="t", parameters={}, limit=5
    )


@pytest.mark.asyncio
async def test_fanout_queries_all_mcp_sources():
    gw = _gateway(
        [
            ("pg-primary", _Provider("pg", items=[_ev("pg")])),
            ("postgres-mcp-a", _Provider("a", items=[_ev("a")])),
            ("oracle-mcp-b", _Provider("b", items=[_ev("b")])),
        ]
    )
    res = await gw.get_application_state("n-a", uuid4(), "app", _req())
    assert len(res.items) == 3
    sources = {s["source"]: s["status"] for s in res.provider_metadata["per_source"]}
    assert sources["postgres-mcp-a"] == "SUCCEEDED"
    assert sources["oracle-mcp-b"] == "SUCCEEDED"


@pytest.mark.asyncio
async def test_failing_source_degrades_without_failing_action():
    gw = _gateway(
        [
            ("pg-primary", _Provider("pg", items=[_ev("pg")])),
            ("oracle-mcp-b", _Provider("b", fail=True)),
        ]
    )
    res = await gw.get_application_state("n-a", uuid4(), "app", _req())
    assert len(res.items) == 1
    sources = {s["source"]: s["status"] for s in res.provider_metadata["per_source"]}
    assert sources["oracle-mcp-b"] == "DEGRADED"


class _SlowProvider(_Provider):
    def __init__(self, key, delay, items=None):
        super().__init__(key, items=items)
        self._delay = delay

    async def get_application_state(self, tenant_id, investigation_id, request, profile):
        import asyncio as _asyncio

        self.calls += 1
        await _asyncio.sleep(self._delay)
        return EvidenceQueryResult(items=list(self._items), total_count=len(self._items))


@pytest.mark.asyncio
async def test_fanout_runs_concurrently_and_preserves_order():
    import time as _time

    gw = _gateway(
        [
            ("pg-primary", _Provider("pg", items=[_ev("pg")])),
            ("postgres-mcp-a", _SlowProvider("a", 0.4, items=[_ev("a")])),
            ("oracle-mcp-b", _SlowProvider("b", 0.4, items=[_ev("b")])),
        ]
    )
    started = _time.monotonic()
    res = await gw.get_application_state("n-a", uuid4(), "app", _req())
    elapsed = _time.monotonic() - started
    # Serial would take >= 0.8s; concurrent takes ~0.4s + overhead.
    assert elapsed < 0.75, f"fan-out looks serial: {elapsed:.2f}s"
    assert len(res.items) == 3
    ordered = [s["source"] for s in res.provider_metadata["per_source"]]
    assert ordered == ["primary", "postgres-mcp-a", "oracle-mcp-b"]


@pytest.mark.asyncio
async def test_fanout_concurrency_one_restores_serial():
    import time as _time

    gw = _gateway(
        [
            ("pg-primary", _Provider("pg", items=[_ev("pg")])),
            ("postgres-mcp-a", _SlowProvider("a", 0.4, items=[_ev("a")])),
            ("oracle-mcp-b", _SlowProvider("b", 0.4, items=[_ev("b")])),
        ],
        fanout_concurrency=1,
    )
    started = _time.monotonic()
    res = await gw.get_application_state("n-a", uuid4(), "app", _req())
    elapsed = _time.monotonic() - started
    assert elapsed >= 0.7, f"concurrency=1 should serialize: {elapsed:.2f}s"
    assert len(res.items) == 3
    sources = {s["source"]: s["status"] for s in res.provider_metadata["per_source"]}
    assert sources["postgres-mcp-a"] == "SUCCEEDED"


@pytest.mark.asyncio
async def test_slow_source_degrades_on_timeout():
    gw = _gateway(
        [
            ("pg-primary", _Provider("pg", items=[_ev("pg")])),
            ("postgres-mcp-slow", _SlowProvider("s", 0.5)),
        ],
        timeout_seconds=0.15,
    )
    res = await gw.get_application_state("n-a", uuid4(), "app", _req())
    assert len(res.items) == 1
    sources = {s["source"]: s["status"] for s in res.provider_metadata["per_source"]}
    assert sources["postgres-mcp-slow"] == "DEGRADED"


def test_resolve_fanout_concurrency_env(monkeypatch):
    from investigation_agent_platform.application.evidence.gateway import (
        _resolve_fanout_concurrency,
    )

    assert _resolve_fanout_concurrency(None) is None
    assert _resolve_fanout_concurrency(1) == 1
    assert _resolve_fanout_concurrency(0) is None
    monkeypatch.setenv("IAP_MCP_FANOUT_CONCURRENCY", "2")
    assert _resolve_fanout_concurrency(None) == 2
    monkeypatch.setenv("IAP_MCP_FANOUT_CONCURRENCY", "bogus")
    assert _resolve_fanout_concurrency(None) is None
    monkeypatch.setenv("IAP_MCP_FANOUT_CONCURRENCY", "1")
    assert _resolve_fanout_concurrency(None) == 1
    # Explicit ctor value wins over env.
    assert _resolve_fanout_concurrency(4) == 4
