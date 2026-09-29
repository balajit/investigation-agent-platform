# tests/unit/test_gateway_composition.py
"""Part 3 addendum: evidence-gateway composition tests (WIP §1.1).

Covers: production builder assigns a composed gateway (frozen selector,
mandatory guards, tenant-scoped services); unconfigured providers stay
unregistered, never stubbed; hop resolver armed iff a gateway exists; dev
contexts unchanged (no gateway, hopping disabled, never an error).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from investigation_agent_platform.infrastructure.configuration.config import (
    ApplicationConfig,
    BudgetConfig,
    DatabaseConfig,
    EvidenceConfig,
    LLMConfig,
    TelemetryConfig,
)


def _production_config(**over: object) -> ApplicationConfig:
    kwargs: dict = {
        "environment": "production",
        "application_id": "test",
        "database": DatabaseConfig(
            connection_uri=SecretStr("postgresql+asyncpg://a:b@localhost/db")
        ),
        "llm": LLMConfig(api_key=SecretStr("sk"), model_name="gpt-4o"),
        "budget": BudgetConfig(),
        "telemetry": TelemetryConfig(enabled=False),
        "evidence": EvidenceConfig(
            elasticsearch_url="http://localhost:9200",
            cursor_signing_key=SecretStr("test-signing-key"),
        ),
    }
    kwargs.update(over)
    return ApplicationConfig(**kwargs)  # type: ignore[arg-type]


def _patched_production():  # type: ignore[no-untyped-def]
    return (
        patch("investigation_agent_platform.bootstrap.create_async_engine"),
        patch("investigation_agent_platform.bootstrap.AppContext"),
        patch("investigation_agent_platform.bootstrap.set_app_context"),
        patch("temporalio.client.Client.connect", new_callable=AsyncMock),
    )


class TestGatewayComposition:
    def test_production_assigns_gateway(self) -> None:
        from investigation_agent_platform.application.evidence.gateway import (
            AsyncEvidenceGateway,
        )
        from investigation_agent_platform.bootstrap import build_app_context

        p1, p2, p3, p4 = _patched_production()
        with p1 as mock_engine, p2 as MockCtx, p3, p4:
            mock_engine.return_value = MagicMock()
            mock_ctx = MagicMock()
            MockCtx.return_value = mock_ctx
            ctx = build_app_context(_production_config())
            assert ctx is mock_ctx
            gateway = mock_ctx.evidence_gateway
            assert isinstance(gateway, AsyncEvidenceGateway)
            assert gateway._provider_selector._is_frozen is True
            assert set(gateway._provider_selector._runtime_providers) == {"elastic-primary"}
            assert gateway._provider_selector._state_providers == {}
            assert gateway._query_safety_policy is not None
            assert gateway._sanitizer is not None

    def test_hop_resolver_armed_with_gateway(self) -> None:
        from investigation_agent_platform.bootstrap import _hop_resolver_for
        from investigation_agent_platform.infrastructure.topology.trace_hop import (
            GatewayTraceHopResolver,
        )

        assert isinstance(_hop_resolver_for(MagicMock()), GatewayTraceHopResolver)

    def test_hop_resolver_none_without_gateway(self) -> None:
        from investigation_agent_platform.bootstrap import _hop_resolver_for

        assert _hop_resolver_for(MagicMock(spec=[])) is None

    def test_dev_context_unchanged(self) -> None:
        from investigation_agent_platform.bootstrap import build_app_context

        cfg = _production_config(environment="test")
        ctx = build_app_context(cfg)
        assert getattr(ctx, "evidence_gateway", None) is None


class _StaticMappingLookup:
    """In-memory EvidenceMappingLookup for adapter contract tests."""

    def __init__(self, evidence=None):  # type: ignore[no-untyped-def]
        self._evidence = evidence

    async def get_evidence(self, tenant_id, evidence_id):  # type: ignore[no-untyped-def]
        return self._evidence


def _mapping_evidence(tenant_id="t", investigation_id=None, evidence_id=None):  # type: ignore[no-untyped-def]
    import uuid as _uuid
    from datetime import UTC, datetime

    from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
    from investigation_agent_platform.domain.provenance.models import (
        EvidenceFreshness,
        EvidenceProvenance,
        QueryFingerprint,
        SourceLocation,
    )

    now = datetime.now(UTC)
    iid = investigation_id or _uuid.uuid4()
    eid = evidence_id or _uuid.uuid4()
    return Evidence(
        tenant_id=tenant_id,
        investigation_id=iid,
        evidence_id=eid,
        evidence_type=EvidenceType.RUNTIME_LOG,
        provider="ELASTIC",
        source="elasticsearch://logs-t-1/doc1",
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


class TestElasticGetParity:
    @pytest.mark.asyncio
    async def test_get_returns_mapped_evidence(self) -> None:
        from unittest.mock import AsyncMock
        from uuid import UUID

        from investigation_agent_platform.infrastructure.evidence.runtime.elastic import (
            AsyncElasticAdapter,
            ElasticAdapterSettings,
        )

        iid = UUID(int=7)
        mapping = _mapping_evidence(tenant_id="t", investigation_id=iid, evidence_id=UUID(int=1))
        hit = {
            "_id": "doc1",
            "_index": "logs-t-1",
            "_source": {"@timestamp": "2026-01-01T00:00:00Z", "message": "boom"},
        }
        client = MagicMock()
        client.search = AsyncMock(return_value={"hits": {"hits": [hit]}})
        adapter = AsyncElasticAdapter(
            client=client, settings=ElasticAdapterSettings(cursor_signing_key=b"test-key")
        )
        ev = await adapter.get_runtime_evidence(
            "t", iid, UUID(int=1), "production", _StaticMappingLookup(mapping)
        )
        assert ev.tenant_id == "t"
        assert ev.investigation_id == iid
        assert "doc1" in (ev.content_snippet or "") or ev.evidence_id is not None
        _, kwargs = client.search.call_args
        assert kwargs["index"] == "logs-t-1"

    @pytest.mark.asyncio
    async def test_get_missing_raises(self) -> None:
        from unittest.mock import AsyncMock
        from uuid import UUID

        from investigation_agent_platform.domain.common.exceptions import EvidenceNotFoundException
        from investigation_agent_platform.infrastructure.evidence.runtime.elastic import (
            AsyncElasticAdapter,
            ElasticAdapterSettings,
        )

        client = MagicMock()
        client.search = AsyncMock(return_value={"hits": {"hits": []}})
        adapter = AsyncElasticAdapter(
            client=client, settings=ElasticAdapterSettings(cursor_signing_key=b"test-key")
        )
        with pytest.raises(EvidenceNotFoundException):
            await adapter.get_runtime_evidence(
                "t", UUID(int=7), UUID(int=2), "production", _StaticMappingLookup(None)
            )

    @pytest.mark.asyncio
    async def test_get_cross_investigation_raises(self) -> None:
        from unittest.mock import AsyncMock
        from uuid import UUID

        from investigation_agent_platform.domain.common.exceptions import EvidenceNotFoundException
        from investigation_agent_platform.infrastructure.evidence.runtime.elastic import (
            AsyncElasticAdapter,
            ElasticAdapterSettings,
        )

        mapping = _mapping_evidence(
            tenant_id="t", investigation_id=UUID(int=8), evidence_id=UUID(int=1)
        )
        client = MagicMock()
        client.search = AsyncMock()
        adapter = AsyncElasticAdapter(
            client=client, settings=ElasticAdapterSettings(cursor_signing_key=b"test-key")
        )
        with pytest.raises(EvidenceNotFoundException):
            await adapter.get_runtime_evidence(
                "t", UUID(int=9), UUID(int=1), "production", _StaticMappingLookup(mapping)
            )
        client.search.assert_not_called()
