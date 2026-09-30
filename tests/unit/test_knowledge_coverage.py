"""Part 6 Slice 0 coverage: fingerprint determinism, envelope validation,
merge-or-fork intake, validity gating, session redaction, intake endpoint,
and migration chain integrity."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import (
    AppContext,
    set_app_context,
)


def _intake_kwargs(**overrides):  # type: ignore[no-untyped-def]
    base = {
        "tenant_id": "tenant-a",
        "application_id": "app-1",
        "error_class": "NullPointerException in OrderService.create",
        "repository": "org/orders",
        "revision": "abc123",
        "failing_symbol": "OrderService.create",
        "caller_symbol": "CheckoutController.place",
        "top_frame_file": "src/orders/service.py",
        "problem_description": "NPE in orders",
    }
    base.update(overrides)
    return base


# ===========================================================================
# Fingerprint
# ===========================================================================


class TestFingerprint:
    def test_deterministic_two_tenant_invariant(self) -> None:
        from investigation_agent_platform.domain.knowledge.fingerprint import (
            build_code_issue_fingerprint,
        )

        fp_a = build_code_issue_fingerprint(
            error_class="NPE",
            repository="org/svc",
            revision="abc",
            failing_symbol="A.b",
            caller_symbol="C.d",
            top_frame_file="src/a.py",
        )
        fp_b = build_code_issue_fingerprint(
            error_class="NPE",
            repository="org/svc",
            revision="abc",
            failing_symbol="A.b",
            caller_symbol="C.d",
            top_frame_file="src/a.py",
        )
        assert fp_a == fp_b
        assert len(fp_a) == 64

    def test_sensitive_inputs_change_nothing_or_reject(self) -> None:
        # Tenant/customer/timestamp data must never be fingerprint inputs:
        # the builder signature accepts no such parameters by construction.
        import inspect

        from investigation_agent_platform.domain.knowledge.fingerprint import (
            build_code_issue_fingerprint,
        )

        params = set(inspect.signature(build_code_issue_fingerprint).parameters)
        assert params.isdisjoint(
            {"tenant_id", "customer", "timestamp", "session_id", "environment"}
        )

    def test_distinct_issues_differ(self) -> None:
        from investigation_agent_platform.domain.knowledge.fingerprint import (
            build_code_issue_fingerprint,
        )

        base = {
            "error_class": "NPE",
            "repository": "org/svc",
            "revision": "abc",
            "failing_symbol": "A.b",
        }
        assert build_code_issue_fingerprint(**base) != build_code_issue_fingerprint(
            **{**base, "failing_symbol": "A.c"}
        )

    def test_unsafe_paths_rejected(self) -> None:
        from investigation_agent_platform.domain.knowledge.fingerprint import (
            build_code_issue_fingerprint,
        )

        with pytest.raises(ValueError):
            build_code_issue_fingerprint(
                error_class="E",
                repository="r",
                revision="rev",
                failing_symbol="f",
                top_frame_file="../etc/passwd",
            )


# ===========================================================================
# Envelope validation
# ===========================================================================


class TestEnvelopeValidation:
    def _artifact_kwargs(self, **overrides):  # type: ignore[no-untyped-def]
        base = {
            "tenant_id": "tenant-a",
            "application_id": "app-1",
            "investigation_id": uuid.uuid4(),
            "kind": "interpretation",
            "statement": "Flag checkout_v2 was ON during the incident window.",
            "confidence": 0.8,
            "refresh_policy": "CONDITIONAL",
            "reverify": {"source_kind": "flag_provider", "source_ref": "checkout_v2"},
            "valid_from": datetime.now(UTC),
        }
        base.update(overrides)
        return base

    def test_conditional_requires_reverify(self) -> None:
        from investigation_agent_platform.domain.knowledge.models import KnowledgeArtifact

        with pytest.raises(ValidationError):
            KnowledgeArtifact(**self._artifact_kwargs(reverify=None))

    def test_shared_runtime_kind_rejected(self) -> None:
        from investigation_agent_platform.domain.knowledge.models import KnowledgeArtifact

        with pytest.raises(ValidationError):
            KnowledgeArtifact(
                **self._artifact_kwargs(
                    visibility="SHARED_CODE_ISSUE", code_issue_fingerprint="abc"
                )
            )

    def test_shared_static_kind_accepted(self) -> None:
        from investigation_agent_platform.domain.knowledge.models import KnowledgeArtifact

        artifact = KnowledgeArtifact(
            **self._artifact_kwargs(
                kind="error_signature",
                refresh_policy="IMMUTABLE",
                reverify=None,
                visibility="SHARED_CODE_ISSUE",
                code_issue_fingerprint="abc",
            )
        )
        assert artifact.visibility.value == "SHARED_CODE_ISSUE"

    def test_shared_requires_fingerprint(self) -> None:
        from investigation_agent_platform.domain.knowledge.models import KnowledgeArtifact

        with pytest.raises(ValidationError):
            KnowledgeArtifact(
                **self._artifact_kwargs(
                    kind="error_signature",
                    refresh_policy="IMMUTABLE",
                    reverify=None,
                    visibility="SHARED_CODE_ISSUE",
                )
            )

    def test_secret_in_statement_rejected(self) -> None:
        from investigation_agent_platform.domain.knowledge.models import KnowledgeArtifact

        with pytest.raises(ValidationError):
            KnowledgeArtifact(
                **self._artifact_kwargs(statement="api_key = sk-abcdef1234567890 exposed")
            )


# ===========================================================================
# Intake merge-or-fork
# ===========================================================================


class TestIntakeMerge:
    def _service(self, ctx: AppContext):  # type: ignore[no-untyped-def]
        return ctx.error_intake_service()

    @pytest.mark.asyncio
    async def test_first_session_forks(self) -> None:
        ctx = AppContext()
        service = self._service(ctx)
        result = await service.intake(**_intake_kwargs())
        assert result.merged is False
        assert result.session_number == 1
        assert len(result.code_issue_fingerprint) == 64

    @pytest.mark.asyncio
    async def test_second_tenant_merges_and_annotates(self) -> None:
        # Merge = shared lineage + global session numbering, NOT a shared
        # row: tenant B gets its own investigation row (RLS requires it)
        # linked by fingerprint, numbered session 2 globally.
        ctx = AppContext()
        service = self._service(ctx)
        first = await service.intake(**_intake_kwargs())
        second = await service.intake(**_intake_kwargs(tenant_id="tenant-b"))
        assert second.merged is True
        assert second.investigation_id != first.investigation_id
        assert second.session_number == 2
        assert second.code_issue_fingerprint == first.code_issue_fingerprint

    @pytest.mark.asyncio
    async def test_different_issue_forks(self) -> None:
        ctx = AppContext()
        service = self._service(ctx)
        first = await service.intake(**_intake_kwargs())
        other = await service.intake(**_intake_kwargs(failing_symbol="Other.thing"))
        assert other.merged is False
        assert other.investigation_id != first.investigation_id

    @pytest.mark.asyncio
    async def test_closed_target_reopens(self) -> None:
        from investigation_agent_platform.domain.investigation.models import (
            ActorType,
            InvestigationStatus,
        )

        ctx = AppContext()
        service = self._service(ctx)
        first = await service.intake(**_intake_kwargs())
        inv = await ctx.investigation_repo.get_by_id("tenant-a", first.investigation_id)
        assert inv is not None
        # Walk to a terminal state through the legal path.
        current = inv
        for target in (
            InvestigationStatus.CONTEXTUALIZING,
            InvestigationStatus.INVESTIGATING,
            InvestigationStatus.CONCLUDING,
            InvestigationStatus.COMPLETED,
        ):
            current, _ = current.transition_to(target, ActorType.SYSTEM, "test")
        await ctx.investigation_repo.save("tenant-a", current, inv.version)
        second = await service.intake(**_intake_kwargs())
        assert second.merged is True
        reopened = await ctx.investigation_repo.get_by_id("tenant-a", first.investigation_id)
        assert reopened is not None
        assert reopened.status == InvestigationStatus.INVESTIGATING

    @pytest.mark.asyncio
    async def test_cancelled_target_forks_new(self) -> None:
        # Cancelled rows are never resurrected: a fresh row is forked, but it
        # still joins the existing lineage (merged=True, next session number).
        from investigation_agent_platform.domain.investigation.models import (
            ActorType,
            InvestigationStatus,
        )

        ctx = AppContext()
        service = self._service(ctx)
        first = await service.intake(**_intake_kwargs())
        inv = await ctx.investigation_repo.get_by_id("tenant-a", first.investigation_id)
        assert inv is not None
        cancelled, _ = inv.transition_to(InvestigationStatus.CANCELLED, ActorType.USER, "stop")
        await ctx.investigation_repo.save("tenant-a", cancelled, inv.version)
        second = await service.intake(**_intake_kwargs())
        assert second.merged is True
        assert second.investigation_id != first.investigation_id
        assert second.session_number == 2


# ===========================================================================
# Validity gating
# ===========================================================================


def _artifact(tenant_id="tenant-a", **overrides):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.knowledge.models import KnowledgeArtifact

    base = {
        "tenant_id": tenant_id,
        "application_id": "app-1",
        "investigation_id": uuid.uuid4(),
        "kind": "interpretation",
        "statement": "Flag checkout_v2 was ON.",
        "confidence": 0.9,
        "refresh_policy": "CONDITIONAL",
        "reverify": {"source_kind": "flag_provider", "source_ref": "checkout_v2"},
        "valid_from": datetime.now(UTC) - timedelta(hours=1),
    }
    base.update(overrides)
    return KnowledgeArtifact(**base)


class TestValidityGating:
    def _service(self, ctx: AppContext, checkers=None):  # type: ignore[no-untyped-def]
        return ctx.knowledge_retrieval_service(checkers=checkers)

    @pytest.mark.asyncio
    async def test_immutable_passes(self) -> None:
        from investigation_agent_platform.domain.knowledge.models import KnowledgeArtifact

        ctx = AppContext()
        artifact = KnowledgeArtifact(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            kind="error_signature",
            statement="NPE at OrderService.create.",
            confidence=1.0,
            refresh_policy="IMMUTABLE",
            valid_from=datetime.now(UTC) - timedelta(days=30),
        )
        await ctx.artifact_repo.save("tenant-a", artifact)
        context = await self._service(ctx).retrieve_for_reasoning("tenant-a", "app-1", uuid.uuid4())
        assert len(context.verified_facts) == 1
        assert context.excluded_stale_count == 0

    @pytest.mark.asyncio
    async def test_conditional_match_included(self) -> None:
        async def agree(tenant_id: str, spec: object) -> bool:
            return True

        ctx = AppContext()
        await ctx.artifact_repo.save("tenant-a", _artifact())
        context = await self._service(ctx, {"flag_provider": agree}).retrieve_for_reasoning(
            "tenant-a", "app-1", uuid.uuid4()
        )
        assert len(context.verified_facts) == 1
        assert context.verified_facts[0].verification_source.startswith("live_reverify:")

    @pytest.mark.asyncio
    async def test_conditional_mismatch_superseded_and_excluded(self) -> None:
        async def disagree(tenant_id: str, spec: object) -> bool:
            return False

        ctx = AppContext()
        artifact = _artifact()
        await ctx.artifact_repo.save("tenant-a", artifact)
        context = await self._service(ctx, {"flag_provider": disagree}).retrieve_for_reasoning(
            "tenant-a", "app-1", uuid.uuid4()
        )
        assert len(context.verified_facts) == 0
        assert context.excluded_stale_count == 1
        stored = await ctx.artifact_repo.get_by_id("tenant-a", artifact.id)
        assert stored is not None
        assert stored.status.value == "SUPERSEDED"

    @pytest.mark.asyncio
    async def test_missing_checker_quarantines(self) -> None:
        ctx = AppContext()
        artifact = _artifact()
        await ctx.artifact_repo.save("tenant-a", artifact)
        context = await self._service(ctx, {}).retrieve_for_reasoning(
            "tenant-a", "app-1", uuid.uuid4()
        )
        assert len(context.verified_facts) == 0
        assert context.excluded_stale_count == 1
        stored = await ctx.artifact_repo.get_by_id("tenant-a", artifact.id)
        assert stored is not None
        assert stored.status.value == "QUARANTINED"

    @pytest.mark.asyncio
    async def test_expired_valid_to_excluded(self) -> None:
        from investigation_agent_platform.domain.knowledge.models import KnowledgeArtifact

        ctx = AppContext()
        artifact = KnowledgeArtifact(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            kind="interpretation",
            statement="Old fact.",
            confidence=0.5,
            refresh_policy="IMMUTABLE",
            valid_from=datetime.now(UTC) - timedelta(days=2),
            valid_to=datetime.now(UTC) - timedelta(days=1),
        )
        await ctx.artifact_repo.save("tenant-a", artifact)
        context = await self._service(ctx).retrieve_for_reasoning("tenant-a", "app-1", uuid.uuid4())
        assert len(context.verified_facts) == 0
        assert context.excluded_stale_count == 1

    @pytest.mark.asyncio
    async def test_shared_visible_across_tenants_with_membership(self) -> None:
        from investigation_agent_platform.domain.knowledge.models import KnowledgeArtifact

        ctx = AppContext()
        shared = KnowledgeArtifact(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            kind="error_signature",
            statement="NPE at OrderService.create.",
            confidence=1.0,
            refresh_policy="IMMUTABLE",
            valid_from=datetime.now(UTC),
            visibility="SHARED_CODE_ISSUE",
            code_issue_fingerprint="fp-123",
        )
        await ctx.artifact_repo.save("tenant-a", shared)
        # Tenant B holds a session on fp-123: membership proof passed explicitly.
        context = await self._service(ctx).retrieve_for_reasoning(
            "tenant-b", "app-1", uuid.uuid4(), code_issue_fingerprints=["fp-123"]
        )
        assert [v.artifact_id for v in context.verified_facts] == [shared.id]

    @pytest.mark.asyncio
    async def test_shared_hidden_without_membership(self) -> None:
        from investigation_agent_platform.domain.knowledge.models import KnowledgeArtifact

        ctx = AppContext()
        shared = KnowledgeArtifact(
            tenant_id="tenant-a",
            application_id="app-1",
            investigation_id=uuid.uuid4(),
            kind="error_signature",
            statement="NPE at OrderService.create.",
            confidence=1.0,
            refresh_policy="IMMUTABLE",
            valid_from=datetime.now(UTC),
            visibility="SHARED_CODE_ISSUE",
            code_issue_fingerprint="fp-123",
        )
        await ctx.artifact_repo.save("tenant-a", shared)
        context = await self._service(ctx).retrieve_for_reasoning("tenant-b", "app-1", uuid.uuid4())
        assert context.verified_facts == []
        assert context.excluded_stale_count == 0


# ===========================================================================
# Capture
# ===========================================================================


class TestCapture:
    @pytest.mark.asyncio
    async def test_capture_never_fails_investigation(self) -> None:
        ctx = AppContext()
        service = ctx.knowledge_capture_service()
        stored = await service.capture_for_investigation("tenant-a", uuid.uuid4())
        assert stored == []

    @pytest.mark.asyncio
    async def test_capture_distills_evidence(self) -> None:
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
        evidence = Evidence(
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
        await ctx.evidence_repo.save("tenant-a", evidence, inv_id)
        service = ctx.knowledge_capture_service()
        stored = await service.capture_for_investigation("tenant-a", inv_id)
        assert len(stored) >= 2  # error_signature + evidence_summary
        kinds = {a.kind for a in stored}
        assert {"error_signature", "evidence_summary"} <= kinds


# ===========================================================================
# Intake + knowledge HTTP endpoints
# ===========================================================================


class TestKnowledgeEndpoints:
    def _client(self, monkeypatch, principal="svc-monitor"):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.api.tenant import require_principal

        monkeypatch.setenv("IAP_INTAKE_SERVICE_PRINCIPALS", "svc-monitor")
        ctx = AppContext()
        set_app_context(ctx)
        app = create_app()
        app.dependency_overrides[require_principal] = lambda: principal
        return TestClient(app), ctx

    def _auth(self, monkeypatch, tenant="tenant-a"):  # type: ignore[no-untyped-def]
        monkeypatch.setenv("IAP_ENVIRONMENT", "development")
        return {"X-Tenant-ID": tenant}

    def test_intake_forks_and_starts(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        client, ctx = self._client(monkeypatch)
        ctx.temporal_client = MagicMock()
        ctx.temporal_client.start_workflow = AsyncMock()
        resp = client.post(
            "/api/v1/intake/errors",
            json={
                "application_id": "app-1",
                "error_class": "NullPointerException",
                "repository": "org/orders",
                "revision": "abc123",
                "failing_symbol": "OrderService.create",
            },
            headers=self._auth(monkeypatch),
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["merged"] is False
        assert body["session_number"] == 1
        assert len(body["code_issue_fingerprint"]) == 64

    def test_intake_rejects_non_service_principal(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        client, _ = self._client(monkeypatch, principal="mallory")
        resp = client.post(
            "/api/v1/intake/errors",
            json={"application_id": "app-1", "error_class": "E"},
            headers=self._auth(monkeypatch),
        )
        assert resp.status_code == 403

    def test_intake_merges_second_tenant(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        client, ctx = self._client(monkeypatch)
        ctx.temporal_client = MagicMock()
        ctx.temporal_client.start_workflow = AsyncMock()
        payload = {
            "application_id": "app-1",
            "error_class": "NullPointerException",
            "repository": "org/orders",
            "revision": "abc123",
            "failing_symbol": "OrderService.create",
        }
        first = client.post(
            "/api/v1/intake/errors", json=payload, headers=self._auth(monkeypatch, "tenant-a")
        )
        assert first.status_code == 202
        second = client.post(
            "/api/v1/intake/errors", json=payload, headers=self._auth(monkeypatch, "tenant-b")
        )
        assert second.status_code == 202
        assert second.json()["merged"] is True
        assert second.json()["session_number"] == 2
        # Linked lineage, separate rows: tenant RLS forbids shared rows.
        assert second.json()["investigation_id"] != first.json()["investigation_id"]
        assert second.json()["code_issue_fingerprint"] == first.json()["code_issue_fingerprint"]

    def test_knowledge_view_redacts_other_sessions(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        client, ctx = self._client(monkeypatch)
        ctx.temporal_client = MagicMock()
        ctx.temporal_client.start_workflow = AsyncMock()
        payload = {
            "application_id": "app-1",
            "error_class": "NullPointerException",
            "repository": "org/orders",
            "revision": "abc123",
            "failing_symbol": "OrderService.create",
        }
        first = client.post(
            "/api/v1/intake/errors", json=payload, headers=self._auth(monkeypatch, "tenant-a")
        )
        inv_id = first.json()["investigation_id"]
        client.post(
            "/api/v1/intake/errors", json=payload, headers=self._auth(monkeypatch, "tenant-b")
        )
        body = client.get(
            f"/api/v1/investigations/{inv_id}/knowledge",
            headers=self._auth(monkeypatch, "tenant-a"),
        ).json()
        assert len(body["sessions"]) == 1
        assert body["sessions"][0]["session_number"] == 1
        assert len(body["other_tenant_sessions"]) == 1
        assert body["other_tenant_sessions"][0]["session_number"] == 2
        assert body["other_tenant_sessions"][0]["tenant"] == "[redacted]"
        assert "log_refs" not in body["other_tenant_sessions"][0]

    def test_knowledge_search_scoped(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        client, ctx = self._client(monkeypatch)
        ctx.temporal_client = MagicMock()
        ctx.temporal_client.start_workflow = AsyncMock()
        client.post(
            "/api/v1/intake/errors",
            json={
                "application_id": "app-1",
                "error_class": "NullPointerException",
                "repository": "org/orders",
                "revision": "abc123",
                "failing_symbol": "OrderService.create",
            },
            headers=self._auth(monkeypatch, "tenant-a"),
        )
        mine = client.get(
            "/api/v1/knowledge/search?application_id=app-1",
            headers=self._auth(monkeypatch, "tenant-a"),
        ).json()
        theirs = client.get(
            "/api/v1/knowledge/search?application_id=app-1",
            headers=self._auth(monkeypatch, "tenant-b"),
        ).json()
        assert mine["total"] >= 0
        assert theirs["total"] == 0


# ===========================================================================
# Migration chain
# ===========================================================================


class TestKnowledgeMigrationChain:
    def test_006_follows_005(self) -> None:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config("alembic.ini")
        script = ScriptDirectory.from_config(cfg)
        rev = script.get_revision("005_knowledge_layer")
        assert rev is not None
        assert rev.down_revision == "004_topology_snapshots"
        head = script.get_revision("007_profile_id_128")
        assert head is not None
        assert head.down_revision == "006_pgvector_extension"
        latest = script.get_revision("008_evidence_observed_at_nullable")
        assert latest is not None
        assert latest.down_revision == "007_profile_id_128"
        new_head = script.get_revision("009_profile_revisions_and_investigation_metadata")
        assert new_head is not None
        assert new_head.down_revision == "008_evidence_observed_at_nullable"
        newest = script.get_revision("010_background_jobs_and_quotas")
        assert newest is not None
        assert newest.down_revision == "009_profile_revisions_and_investigation_metadata"
        newest11 = script.get_revision("011_input_requirements")
        assert newest11 is not None
        assert newest11.down_revision == "010_background_jobs_and_quotas"
        newest12 = script.get_revision("012_finding_clusters")
        assert newest12 is not None
        assert newest12.down_revision == "011_input_requirements"
        assert script.get_heads() == ["012_finding_clusters"]

    def test_orm_tables_registered(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.models import Base

        for table in ("knowledge_artifacts", "investigation_sessions", "code_issue_index"):
            assert table in Base.metadata.tables
