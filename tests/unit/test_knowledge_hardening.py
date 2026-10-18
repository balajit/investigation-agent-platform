"""Part 6 hardening: ISSUE-8/9/10/11 acceptance tests."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError


def _ttl_artifact(**overrides):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.knowledge.models import KnowledgeArtifact

    base = {
        "tenant_id": "tenant-a",
        "application_id": "app-1",
        "investigation_id": uuid.uuid4(),
        "kind": "evidence_summary",
        "statement": "Summary of observed evidence.",
        "confidence": 0.7,
        "refresh_policy": "TTL",
        "valid_from": datetime.now(UTC),
        "valid_to": datetime.now(UTC) + timedelta(days=90),
    }
    base.update(overrides)
    return KnowledgeArtifact(**base)


class TestIssue8TTL:
    def test_ttl_requires_valid_to(self) -> None:
        with pytest.raises(ValidationError):
            _ttl_artifact(valid_to=None)

    @pytest.mark.asyncio
    async def test_captured_evidence_summary_retrievable(self) -> None:
        from investigation_agent_platform.api.dependencies import AppContext
        from investigation_agent_platform.domain.evidence.models import Evidence
        from investigation_agent_platform.domain.provenance.models import (
            EvidenceFreshness,
            EvidenceProvenance,
            QueryFingerprint,
            SourceLocation,
        )

        ctx = AppContext()
        inv_id = uuid.uuid4()
        now = datetime.now(UTC)
        ev = Evidence(
            tenant_id="tenant-a",
            investigation_id=inv_id,
            evidence_type="LOG",
            provider="test",
            source="test://log/1",
            title="t",
            summary="Null pointer in OrderService",
            observed_at=now,
            retrieved_at=now,
            provenance=EvidenceProvenance(
                tenant_id="tenant-a",
                investigation_id=inv_id,
                provider_type="TEST",
                requested_provider_id="test",
                actual_provider_id="test",
                source_system="test",
                retrieval_timestamp=now,
                query_fingerprint=QueryFingerprint(
                    provider_type="TEST", operation="test", normalized_query_hash="abc"
                ),
                source_location=SourceLocation(system="test", identifier="test://log/1"),
            ),
            freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
            fingerprint="fp-ev-1",
        )
        await ctx.evidence_repo.save("tenant-a", ev, inv_id)
        stored = await ctx.knowledge_capture_service(
            clock_now=lambda: now
        ).capture_for_investigation("tenant-a", inv_id)
        summaries = [a for a in stored if a.kind == "evidence_summary"]
        assert summaries and summaries[0].valid_to is not None
        context = await ctx.knowledge_retrieval_service().retrieve_for_reasoning(
            "tenant-a", summaries[0].application_id, inv_id
        )
        assert summaries[0].id in {v.artifact_id for v in context.verified_facts}

    @pytest.mark.asyncio
    async def test_ttl_time_travel_expires(self) -> None:
        from investigation_agent_platform.api.dependencies import AppContext
        from investigation_agent_platform.application.knowledge.retrieve import (
            KnowledgeRetrievalService,
        )
        from investigation_agent_platform.domain.knowledge.models import ArtifactStatus

        ctx = AppContext()
        now = datetime.now(UTC)
        artifact = _ttl_artifact(valid_from=now, valid_to=now + timedelta(days=1))
        await ctx.artifact_repo.save("tenant-a", artifact)
        future = now + timedelta(days=2)
        retr = KnowledgeRetrievalService(artifact_repo=ctx.artifact_repo, clock_now=lambda: future)
        context = await retr.retrieve_for_reasoning("tenant-a", "app-1", uuid.uuid4())
        assert context.excluded_stale_count == 1
        stored = await ctx.artifact_repo.get_by_id("tenant-a", artifact.id)
        assert stored is not None and stored.status == ArtifactStatus.EXPIRED


class TestIssue9Janitor:
    @pytest.mark.asyncio
    async def test_sweep_transitions_only_past_window(self) -> None:
        from investigation_agent_platform.api.dependencies import AppContext
        from investigation_agent_platform.application.knowledge.janitor import (
            KnowledgeArtifactJanitor,
        )
        from investigation_agent_platform.domain.knowledge.models import ArtifactStatus

        ctx = AppContext()
        now = datetime.now(UTC)
        past = _ttl_artifact(valid_from=now - timedelta(days=10), valid_to=now - timedelta(days=1))
        within = _ttl_artifact(valid_from=now, valid_to=now + timedelta(days=10))
        for a in (past, within):
            await ctx.artifact_repo.save("tenant-a", a)
        janitor = KnowledgeArtifactJanitor(ctx.artifact_repo, ctx.knowledge_retrieval_service())
        result = await janitor.sweep_tenant("tenant-a", cutoff=now)
        assert result.expired_count == 1
        assert result.expired_ids == [past.id]
        stored = await ctx.artifact_repo.get_by_id("tenant-a", past.id)
        assert stored is not None and stored.status == ArtifactStatus.EXPIRED

    @pytest.mark.asyncio
    async def test_sweep_idempotent(self) -> None:
        from investigation_agent_platform.api.dependencies import AppContext
        from investigation_agent_platform.application.knowledge.janitor import (
            KnowledgeArtifactJanitor,
        )

        ctx = AppContext()
        now = datetime.now(UTC)
        past = _ttl_artifact(valid_from=now - timedelta(days=5), valid_to=now - timedelta(days=1))
        await ctx.artifact_repo.save("tenant-a", past)
        janitor = KnowledgeArtifactJanitor(ctx.artifact_repo, ctx.knowledge_retrieval_service())
        first = await janitor.sweep_tenant("tenant-a", cutoff=now)
        second = await janitor.sweep_tenant("tenant-a", cutoff=now)
        assert first.expired_count == 1
        assert second.expired_count == 0

    @pytest.mark.asyncio
    async def test_sweep_reverify_reuses_checker_registry(self) -> None:
        from investigation_agent_platform.api.dependencies import AppContext
        from investigation_agent_platform.application.knowledge.janitor import (
            KnowledgeArtifactJanitor,
        )
        from investigation_agent_platform.domain.knowledge.models import (
            ArtifactStatus,
            KnowledgeArtifact,
        )

        async def disagree(tenant_id: str, spec: object) -> bool:
            return False

        ctx = AppContext()
        artifact = KnowledgeArtifact(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            kind="interpretation",
            statement="Flag X was ON.",
            confidence=0.9,
            refresh_policy="CONDITIONAL",
            reverify={"source_kind": "flag_provider", "source_ref": "x"},
            valid_from=datetime.now(UTC),
        )
        await ctx.artifact_repo.save("tenant-a", artifact)
        # Retrieval path outcome for the same fixture:
        retr_ctx = await ctx.knowledge_retrieval_service(
            {"flag_provider": disagree}
        ).retrieve_for_reasoning("tenant-a", "app-1", uuid.uuid4())
        assert retr_ctx.excluded_stale_count == 1
        # Reset to ACTIVE and sweep-triggered reverification must agree.
        await ctx.artifact_repo.save(
            "tenant-a", artifact.model_copy(update={"status": ArtifactStatus.ACTIVE})
        )
        # Re-save overwrites same id; sweep with the same checker registry.
        janitor = KnowledgeArtifactJanitor(
            ctx.artifact_repo,
            ctx.knowledge_retrieval_service({"flag_provider": disagree}),
        )
        result = await janitor.sweep_tenant("tenant-a", reverify_conditional=True)
        assert result.superseded_count == 1


class TestIssue10Budgets:
    @pytest.mark.asyncio
    async def test_episode_cap_projects_only_up_to_cap(self) -> None:
        from investigation_agent_platform.api.dependencies import AppContext
        from investigation_agent_platform.domain.evidence.models import Evidence
        from investigation_agent_platform.domain.provenance.models import (
            EvidenceFreshness,
            EvidenceProvenance,
            QueryFingerprint,
            SourceLocation,
        )

        ctx = AppContext()
        ctx.temporal_port = AsyncMock()
        ctx.temporal_port.project_episode = AsyncMock(return_value="ep")
        ctx.knowledge_store = None
        inv_id = uuid.uuid4()
        now = datetime.now(UTC)
        for i in range(4):
            ev = Evidence(
                tenant_id="tenant-a",
                investigation_id=inv_id,
                evidence_type="LOG",
                provider="test",
                source=f"test://log/{i}",
                title="t",
                summary=f"event {i}",
                observed_at=now,
                retrieved_at=now,
                provenance=EvidenceProvenance(
                    tenant_id="tenant-a",
                    investigation_id=inv_id,
                    provider_type="TEST",
                    requested_provider_id="test",
                    actual_provider_id="test",
                    source_system="test",
                    retrieval_timestamp=now,
                    query_fingerprint=QueryFingerprint(
                        provider_type="TEST", operation="test", normalized_query_hash="abc"
                    ),
                    source_location=SourceLocation(system="test", identifier=f"test://log/{i}"),
                ),
                freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
                fingerprint=f"fp-{i}",
            )
            await ctx.evidence_repo.save("tenant-a", ev, inv_id)
        service = ctx.knowledge_capture_service()
        # Force a tiny cap: 4 evidence items -> 4 summaries + signatures.
        service.max_episodes_per_investigation = 2
        stored = await service.capture_for_investigation("tenant-a", inv_id)
        assert len(stored) > 2  # all envelopes persisted
        assert ctx.temporal_port.project_episode.await_count == 2

    @pytest.mark.asyncio
    async def test_concurrent_same_group_serialized(self) -> None:

        from investigation_agent_platform.api.dependencies import InMemoryArtifactRepository
        from investigation_agent_platform.application.knowledge.capture import (
            KnowledgeCaptureService,
        )

        order: list[str] = []

        class _Port:
            async def project_episode(self, tenant_id: str, group_id: str, artifact):  # type: ignore[no-untyped-def]
                order.append(f"enter:{artifact.id}")
                await asyncio.sleep(0.05)
                order.append(f"exit:{artifact.id}")
                return "ep"

        inv_id = uuid.uuid4()
        service = KnowledgeCaptureService(
            artifact_repo=InMemoryArtifactRepository(), temporal_port=_Port()
        )
        a1 = _ttl_artifact(investigation_id=inv_id)
        a2 = _ttl_artifact(investigation_id=inv_id)
        await asyncio.gather(service._project("tenant-a", a1), service._project("tenant-a", a2))
        assert order[1].startswith("exit:")
        assert order[0].split(":", 1)[1] == order[1].split(":", 1)[1]

    def test_byte_cap_truncates(self) -> None:
        import json

        from investigation_agent_platform.infrastructure.knowledge.graphiti_adapter import (
            GraphitiTemporalKnowledge,
        )

        adapter = GraphitiTemporalKnowledge(neo4j_uri="bolt://x:7687", max_episode_bytes=512)

        class _Client:
            def __init__(self) -> None:
                self.bodies: list[str] = []

            async def add_episode(self, **kwargs):  # type: ignore[no-untyped-def]
                self.bodies.append(kwargs["episode_body"])

                class _Ep:
                    uuid = "ep-1"

                class _Res:
                    episode = _Ep()

                return _Res()

        adapter._client = _Client()
        artifact = _ttl_artifact(kind="interpretation", statement="x" * 4000)
        body_len = asyncio.run(adapter.project_episode("tenant-a", "inv_abc", artifact))
        assert body_len == "ep-1"
        assert len(adapter._client.bodies[0].encode("utf-8")) <= 512
        payload = json.loads(adapter._client.bodies[0])
        assert payload["_truncated"] is True

    @pytest.mark.asyncio
    async def test_telemetry_emitted(self) -> None:
        from investigation_agent_platform.api.dependencies import AppContext

        ctx = AppContext()

        class _Obs:
            def __init__(self) -> None:
                self.metrics: list[tuple[str, float]] = []

            def record_metric(self, name: str, value: float, tags: dict[str, str]) -> None:
                self.metrics.append((name, value))

        obs = _Obs()
        service = ctx.knowledge_capture_service()
        service.observability = obs
        await service.capture_for_investigation("tenant-a", uuid.uuid4())
        assert any(name == "knowledge.episodes_projected" for name, _ in obs.metrics)


class TestIssue11Join:
    def test_parse_code_ref(self) -> None:
        from investigation_agent_platform.domain.knowledge.models import parse_code_ref

        assert parse_code_ref("org/orders@abc123:src/a.py#42") == (
            "org/orders",
            "abc123",
            "src/a.py",
            42,
        )
        assert parse_code_ref("garbage") is None
        assert parse_code_ref("repo@rev:/abs/path#1") is None

    @pytest.mark.asyncio
    async def test_join_attaches_ownership(self) -> None:
        from investigation_agent_platform.api.dependencies import AppContext
        from investigation_agent_platform.domain.topology.models import (
            AttributionFallbackLevel,
            StaticOwnershipResult,
        )

        ctx = AppContext()
        artifact = _ttl_artifact(code_refs=["org/orders@abc:src/a.py#10"])
        await ctx.artifact_repo.save("tenant-a", artifact)

        class _Port:
            async def resolve_source_location(self, *args):  # type: ignore[no-untyped-def]
                return StaticOwnershipResult(
                    tenant_id="tenant-a",
                    repository_id="org/orders",
                    revision="abc",
                    snapshot_id=uuid.uuid4(),
                    matched_file_path="src/a.py",
                    domain_id="checkout",
                    fallback_level=AttributionFallbackLevel.AST_NODE,
                )

        context = await ctx.knowledge_retrieval_service(
            attribution_port=_Port()
        ).retrieve_for_reasoning("tenant-a", "app-1", uuid.uuid4())
        assert len(context.verified_facts) == 1
        assert (
            context.verified_facts[0].attribution["org/orders@abc:src/a.py#10"]["domain_id"]
            == "checkout"
        )

    @pytest.mark.asyncio
    async def test_degraded_and_malformed_refs_skip(self) -> None:
        from investigation_agent_platform.api.dependencies import AppContext

        ctx = AppContext()
        artifact = _ttl_artifact(code_refs=["garbage", "org/o@r:src/a.py#1"])
        await ctx.artifact_repo.save("tenant-a", artifact)

        class _Failing:
            async def resolve_source_location(self, *args):  # type: ignore[no-untyped-def]
                raise RuntimeError("snapshot collected")

        context = await ctx.knowledge_retrieval_service(
            attribution_port=_Failing()
        ).retrieve_for_reasoning("tenant-a", "app-1", uuid.uuid4())
        assert len(context.verified_facts) == 1
        assert context.verified_facts[0].attribution == {}

    @pytest.mark.asyncio
    async def test_no_port_byte_identical(self) -> None:
        from investigation_agent_platform.api.dependencies import AppContext

        ctx = AppContext()
        artifact = _ttl_artifact(code_refs=["org/orders@abc:src/a.py#10"])
        await ctx.artifact_repo.save("tenant-a", artifact)
        context = await ctx.knowledge_retrieval_service().retrieve_for_reasoning(
            "tenant-a", "app-1", uuid.uuid4()
        )
        assert context.verified_facts[0].code_refs == ["org/orders@abc:src/a.py#10"]
        assert context.verified_facts[0].attribution == {}
