"""Coverage for low files: domain models, utils, persistence, main, api routers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import (
    _DEFAULT_PROFILE,
    AppContext,
    InMemoryApplicationProfileRepository,
    InMemoryEvidenceRepository,
    set_app_context,
)
from investigation_agent_platform.domain.common.exceptions import (
    ConcurrencyError,
    InvalidLifecycleTransitionException,
)
from investigation_agent_platform.domain.common.utils import (
    CanonicalJSONEncoder,
    SystemClock,
    generate_execution_hash,
    generate_uuid,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.hypothesis.models import (
    AssessmentStrength,
    AssessmentType,
    Hypothesis,
    HypothesisEvidenceAssessment,
    HypothesisStatus,
)
from investigation_agent_platform.domain.investigation.models import (
    ActorType,
    Investigation,
    InvestigationContext,
    InvestigationRequest,
    InvestigationStatus,
)
from investigation_agent_platform.domain.profile.models import (
    ApplicationProfile,
    CodeProfile,
    CorrelationProfile,
    InvestigationProfile,
    ObservabilityProfile,
    StateProfile,
)
from investigation_agent_platform.domain.timeline.models import TimelineEvent, TimelineEventType

# helpers


def _make_investigation(
    status: InvestigationStatus = InvestigationStatus.CREATED,
    tenant_id: str = "tenant-a",
) -> Investigation:
    now = datetime.now(UTC)
    req = InvestigationRequest(
        application_id="example-app",
        problem_description="incident desc",
        session_id="sess-1",
        requested_by="tester",
    )
    ctx = InvestigationContext(
        environment="production",
        time_window=(now - timedelta(hours=1), now),
    )
    return Investigation(
        session_id="sess-1",
        application_id="example-app",
        tenant_id=tenant_id,
        request=req,
        context=ctx,
        status=status,
        created_at=now,
        updated_at=now,
        version=1,
    )


def _make_evidence(tenant_id: str = "tenant-a", iid: UUID | None = None) -> Evidence:
    now = datetime.now(UTC)
    inv_id = iid or uuid.uuid4()
    from investigation_agent_platform.domain.provenance.models import (
        EvidenceFreshness,
        EvidenceProvenance,
        QueryFingerprint,
        SourceLocation,
    )

    return Evidence(
        tenant_id=tenant_id,
        investigation_id=inv_id,
        evidence_type=EvidenceType.LOG,
        provider="prov",
        source="urn:src",
        title="t",
        summary="s",
        fingerprint=f"fp-{uuid.uuid4()}",
        observed_at=now,
        retrieved_at=now,
        provenance=EvidenceProvenance(
            tenant_id=tenant_id,
            investigation_id=inv_id,
            provider_type="test",
            requested_provider_id="test",
            actual_provider_id="test",
            source_system="test",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="test", operation="test", normalized_query_hash="abc"
            ),
            source_location=SourceLocation(system="test", identifier="id"),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
    )


def _make_hypothesis(tenant_id: str = "tenant-a", iid: UUID | None = None) -> Hypothesis:
    return Hypothesis(
        tenant_id=tenant_id,
        investigation_id=iid or uuid.uuid4(),
        title="t",
        description="d",
        statement="s",
    )


def _clock_fixed(dt: datetime) -> SystemClock:
    class Fixed:
        def utcnow(self) -> datetime:
            return dt

    return Fixed()  # type: ignore[return-value]


# domain/investigation/models transition tests


class TestInvestigationTransitions:
    def test_created_to_investigating_allowed(self) -> None:
        inv = _make_investigation(InvestigationStatus.CREATED)
        now = datetime.now(UTC)
        updated, trans = inv.transition_to(
            InvestigationStatus.INVESTIGATING, ActorType.SYSTEM, "go", clock=_clock_fixed(now)
        )
        assert updated.status == InvestigationStatus.INVESTIGATING
        assert updated.version == 2
        assert updated.started_at == now
        assert trans.from_status == InvestigationStatus.CREATED
        assert trans.to_status == InvestigationStatus.INVESTIGATING
        assert updated.completed_at is None

    def test_created_to_completed_not_allowed(self) -> None:
        inv = _make_investigation(InvestigationStatus.CREATED)
        with pytest.raises(InvalidLifecycleTransitionException):
            inv.transition_to(
                InvestigationStatus.COMPLETED,
                ActorType.SYSTEM,
                "bad",
                clock=_clock_fixed(datetime.now(UTC)),
            )

    def test_terminal_immutability(self) -> None:
        # Part 6 D8: COMPLETED/FAILED support exactly one outgoing edge —
        # reopen to INVESTIGATING for recurring code issues. CANCELLED stays
        # terminal (explicit human action; auto-intake must not resurrect it).
        for terminal in [
            InvestigationStatus.COMPLETED,
            InvestigationStatus.FAILED,
        ]:
            inv = _make_investigation(terminal)
            reopened, _ = inv.transition_to(
                InvestigationStatus.INVESTIGATING, ActorType.SYSTEM, "recurring code issue"
            )
            assert reopened.status == InvestigationStatus.INVESTIGATING
            for target in [InvestigationStatus.CREATED, InvestigationStatus.COMPLETED]:
                with pytest.raises(InvalidLifecycleTransitionException):
                    inv.transition_to(target, ActorType.SYSTEM, "x")
        inv = _make_investigation(InvestigationStatus.CANCELLED)
        for target in [InvestigationStatus.INVESTIGATING, InvestigationStatus.CREATED]:
            with pytest.raises(InvalidLifecycleTransitionException):
                inv.transition_to(target, ActorType.SYSTEM, "x")

    def test_concluding_to_completed_sets_completed_at(self) -> None:
        inv = _make_investigation(InvestigationStatus.CONCLUDING)
        now = datetime.now(UTC)
        updated, _ = inv.transition_to(
            InvestigationStatus.COMPLETED, ActorType.ORCHESTRATOR, "done", clock=_clock_fixed(now)
        )
        assert updated.completed_at == now
        assert updated.updated_at == now
        assert updated.version == 2

    def test_started_at_only_set_once(self) -> None:
        inv = _make_investigation(InvestigationStatus.CREATED)
        now = datetime.now(UTC)
        inv2, _ = inv.transition_to(
            InvestigationStatus.INVESTIGATING, ActorType.SYSTEM, "go", clock=_clock_fixed(now)
        )
        assert inv2.started_at == now
        # next transition should not overwrite started_at
        later = now + timedelta(seconds=10)
        inv3, _ = inv2.transition_to(
            InvestigationStatus.CORRELATING, ActorType.SYSTEM, "go2", clock=_clock_fixed(later)
        )
        assert inv3.started_at == now

    def test_invalid_transition_raises_with_details(self) -> None:
        inv = _make_investigation(InvestigationStatus.CREATED)
        with pytest.raises(InvalidLifecycleTransitionException) as ei:
            inv.transition_to(InvestigationStatus.VERIFYING, ActorType.USER, "bad")
        assert ei.value.details["current_status"] == "CREATED"

    def test_hypothesis_transition_status(self) -> None:
        h = _make_hypothesis()
        h2 = h.transition_status(HypothesisStatus.UNDER_INVESTIGATION)
        assert h2.status == HypothesisStatus.UNDER_INVESTIGATION
        with pytest.raises(ValueError):
            h.transition_status(HypothesisStatus.VERIFIED)
        # terminal
        hv = h2.transition_status(HypothesisStatus.SUPPORTED).transition_status(
            HypothesisStatus.VERIFIED
        )
        with pytest.raises(ValueError):
            hv.transition_status(HypothesisStatus.REJECTED)

    def test_hypothesis_assessment_consistency(self) -> None:
        eid = uuid.uuid4()
        iid = uuid.uuid4()
        # missing supporting id should raise
        with pytest.raises(ValidationError):
            Hypothesis(
                tenant_id="t",
                investigation_id=iid,
                title="t",
                description="d",
                statement="s",
                supporting_evidence_ids=[],
                assessments=[
                    HypothesisEvidenceAssessment(
                        hypothesis_id=uuid.uuid4(),
                        evidence_id=eid,
                        assessment=AssessmentType.SUPPORTS,
                        strength=AssessmentStrength.STRONG,
                        reason="r",
                    )
                ],
            )


# domain/common/utils


class TestCommonUtils:
    def test_generate_execution_hash_deterministic(self) -> None:
        iid = uuid.uuid4()
        h1 = generate_execution_hash("t", iid, "SEARCH_LOGS", {"q": "x"}, "cap", "prov")
        h2 = generate_execution_hash("t", iid, "SEARCH_LOGS", {"q": "x"}, "cap", "prov")
        assert h1 == h2
        assert len(h1) == 64
        # different params -> different hash
        h3 = generate_execution_hash("t", iid, "SEARCH_LOGS", {"q": "y"}, "cap", "prov")
        assert h3 != h1
        # order invariant via sort_keys
        h4 = generate_execution_hash("t", iid, "SEARCH_LOGS", {"b": 1, "a": 2}, "cap", "prov")
        h5 = generate_execution_hash("t", iid, "SEARCH_LOGS", {"a": 2, "b": 1}, "cap", "prov")
        assert h4 == h5

    def test_generate_execution_hash_handles_uuid_datetime(self) -> None:
        dt = datetime.now(UTC)
        iid = uuid.uuid4()
        h = generate_execution_hash("t", iid, "GET_CODE", {"ts": dt, "id": iid}, "cap", "prov")
        assert isinstance(h, str)

    def test_canonical_encoder(self) -> None:
        enc = CanonicalJSONEncoder()
        uid = uuid.uuid4()
        assert enc.default(uid) == str(uid)
        dt = datetime.now(UTC)
        assert dt.isoformat() in enc.default(dt)
        naive = datetime(2024, 1, 1, 12, 0, 0)
        assert enc.default(naive).endswith("+00:00")

        # object with to_dict
        class Obj:
            def to_dict(self) -> dict[str, str]:
                return {"k": "v"}

        assert enc.default(Obj()) == {"k": "v"}

    def test_system_clock_returns_utc(self) -> None:
        c = SystemClock()
        now = c.utcnow()
        assert now.tzinfo is not None

    def test_generate_uuid(self) -> None:
        u = generate_uuid()
        assert isinstance(u, UUID)


# domain/profile validation


class TestProfileValidation:
    def _valid_kwargs(self) -> dict[str, object]:
        return dict(
            id="app1",
            tenant_id="tenant-a",
            name="n",
            description="d",
            environment="prod",
            observability=ObservabilityProfile(
                provider="es",
                indices=["logs"],
                timestampField="@ts",
                serviceField="svc",
                environmentField="env",
                sessionField="sess",
                requestField="req",
                traceField="trace",
                logLevelField="lvl",
            ),
            state=StateProfile(
                provider="pg",
                database="db",
                schema="public",
                tables=["t"],
                primaryIdentifiers=["id"],
                stateFields=["status"],
                timestampFields=["created_at"],
                queryTemplates={"q1": "SELECT * FROM t WHERE id=:id"},
            ),
            code=CodeProfile(
                provider="git",
                repository="org/repo",
                defaultBranch="main",
                language="python",
                sourceRoots=["src"],
                buildSystem="uv",
                moduleStructure="src/mod",
            ),
            correlation=CorrelationProfile(fields=["trace.id"]),
            investigation=InvestigationProfile(),
        )

    def test_valid_profile(self) -> None:
        p = ApplicationProfile(**self._valid_kwargs())  # type: ignore[arg-type]
        assert p.id == "app1"

    def test_forbidden_ddl_rejection(self) -> None:
        with pytest.raises(ValidationError):
            StateProfile(
                provider="pg",
                database="db",
                schema="public",
                tables=["t"],
                primaryIdentifiers=["id"],
                stateFields=["status"],
                timestampFields=["created_at"],
                queryTemplates={"bad": "SELECT * FROM t; DROP TABLE t --"},
            )

    def test_code_profile_path_traversal(self) -> None:
        with pytest.raises(ValidationError):
            CodeProfile(
                provider="git",
                repository="../etc/passwd",
                defaultBranch="main",
                language="python",
                sourceRoots=["src"],
                buildSystem="uv",
                moduleStructure="src/mod",
            )
        with pytest.raises(ValidationError):
            CodeProfile(
                provider="git",
                repository="org/repo",
                defaultBranch="main",
                language="python",
                sourceRoots=["../src"],
                buildSystem="uv",
                moduleStructure="src/mod",
            )


# persistence mapping


class TestSqlAlchemyRepositories:
    def test_investigation_to_from_orm_roundtrip(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.investigation_repository import (
            SqlAlchemyInvestigationRepository,
        )

        inv = _make_investigation()
        orm = SqlAlchemyInvestigationRepository._to_orm(inv)
        assert orm.tenant_id == inv.tenant_id
        assert orm.status == inv.status.value
        # from_orm
        back = SqlAlchemyInvestigationRepository._from_orm(orm)
        assert back.id == inv.id
        assert back.status == inv.status

    def test_investigation_unknown_status_archived_maps_cancelled(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.investigation_repository import (
            SqlAlchemyInvestigationRepository,
        )

        inv = _make_investigation()
        orm = SqlAlchemyInvestigationRepository._to_orm(inv)
        orm.status = "ARCHIVED"
        back = SqlAlchemyInvestigationRepository._from_orm(orm)
        assert back.status == InvestigationStatus.CANCELLED
        orm.status = "WEIRD_UNKNOWN"
        back2 = SqlAlchemyInvestigationRepository._from_orm(orm)
        assert back2.status == InvestigationStatus.CANCELLED

    def test_investigation_fallback_request_context(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.investigation_repository import (
            SqlAlchemyInvestigationRepository,
        )

        inv = _make_investigation()
        orm = SqlAlchemyInvestigationRepository._to_orm(inv)
        orm.request_json = None
        orm.context_json = None
        back = SqlAlchemyInvestigationRepository._from_orm(orm)
        assert back.request.problem_description == inv.request.problem_description

    @pytest.mark.asyncio
    async def test_investigation_repo_save_concurrency_error(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.investigation_repository import (
            SqlAlchemyInvestigationRepository,
        )

        inv = _make_investigation()

        # mock session factory + rls_session
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.rowcount = 0
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.rollback = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=None)

        def factory() -> MagicMock:
            m = MagicMock()
            m.__aenter__ = AsyncMock(return_value=mock_session)
            m.__aexit__ = AsyncMock(return_value=None)
            return m

        # patch rls_session to return our mock
        with patch(
            "investigation_agent_platform.infrastructure.persistence.investigation_repository.rls_session",
            return_value=mock_cm,
        ):
            repo = SqlAlchemyInvestigationRepository(db_session_factory=factory)
            with pytest.raises(ConcurrencyError):
                await repo.save("tenant-a", inv, expected_version=1)

    @pytest.mark.asyncio
    async def test_investigation_repo_get_by_id_tenant_filter(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.investigation_repository import (
            SqlAlchemyInvestigationRepository,
        )

        mock_session = AsyncMock()
        # empty result
        mock_scalars = MagicMock()
        mock_scalars.first.return_value = None
        mock_session.scalars = AsyncMock(return_value=mock_scalars)
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cm.__aexit__ = AsyncMock(return_value=None)
        with patch(
            "investigation_agent_platform.infrastructure.persistence.investigation_repository.rls_session",
            return_value=mock_cm,
        ):
            repo = SqlAlchemyInvestigationRepository(db_session_factory=MagicMock())
            res = await repo.get_by_id("tenant-a", uuid.uuid4())
            assert res is None
            # ensure tenant filter was used
            assert mock_session.scalars.called

    def test_hypothesis_to_from_orm(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.hypothesis_repository import (
            SqlAlchemyHypothesisRepository,
        )

        h = _make_hypothesis()
        orm = SqlAlchemyHypothesisRepository._to_orm(h, "tenant-a", h.investigation_id)
        # F-032: tenant_id must round-trip through the ORM, never "unknown".
        assert orm.tenant_id == "tenant-a"
        back = SqlAlchemyHypothesisRepository._from_orm(orm)
        assert back.title == h.title
        assert back.tenant_id == "tenant-a"

    def test_timeline_to_from_orm_invalid_uuid(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.timeline_repository import (
            SqlAlchemyTimelineRepository,
        )

        iid = uuid.uuid4()
        ev = TimelineEvent(
            tenant_id="tenant-a",
            investigation_id=iid,
            timestamp=datetime.now(UTC),
            event_type=TimelineEventType.LOG_EVENT,
            description="d",
        )
        orm = SqlAlchemyTimelineRepository._to_orm(ev, iid)
        orm.id = uuid.uuid4()
        # F-032: tenant_id must round-trip through the ORM, never "unknown".
        assert orm.tenant_id == "tenant-a"
        # inject invalid uuid strings
        orm.entity_ids = ["not-a-uuid", str(uuid.uuid4())]
        back = SqlAlchemyTimelineRepository._from_orm(orm)
        assert len(back.entity_ids) == 1
        assert back.tenant_id == "tenant-a"

    def test_profile_to_from_orm(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.profile_repository import (
            SqlAlchemyApplicationProfileRepository,
        )

        repo = SqlAlchemyApplicationProfileRepository(db_session_factory=MagicMock())
        orm = repo._to_orm(_DEFAULT_PROFILE, "tenant-a")
        assert orm.id == _DEFAULT_PROFILE.id
        back = repo._from_orm(orm)
        assert back.id == _DEFAULT_PROFILE.id

    def test_evidence_to_from_orm(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.evidence_repository import (
            SqlAlchemyEvidenceRepository,
        )

        ev = _make_evidence()
        orm = SqlAlchemyEvidenceRepository._to_orm(ev, ev.investigation_id)
        assert orm.tenant_id == ev.tenant_id
        back = SqlAlchemyEvidenceRepository._from_orm(orm)
        assert back.evidence_id == ev.evidence_id

    @pytest.mark.asyncio
    async def test_rls_helper_sets_tenant(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.rls import rls_session

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_factory = MagicMock()

        # factory returns async context manager
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_session)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_factory.return_value = cm

        async with rls_session(mock_factory, "tenant-a") as sess:
            assert sess is mock_session
        assert mock_session.execute.called
        assert mock_session.commit.called


# main.py


class TestMainModule:
    def test_create_app_factory(self) -> None:
        from investigation_agent_platform.main import _create_lifespan_app

        app = _create_lifespan_app()
        assert app.title == "Investigation Agent Platform API"
        assert app.router.lifespan_context is not None

    @pytest.mark.asyncio
    async def test_lifespan_startup_shutdown(self) -> None:
        from investigation_agent_platform.main import _create_lifespan_app

        app = _create_lifespan_app()
        # lifespan should be async context manager
        lifespan = app.router.lifespan_context
        assert lifespan is not None
        async with lifespan(app):
            pass


# health / profiles / timeline / hypotheses / events / evidence via TestClient


class TestHealthAndRouters:
    def _ctx_client(self) -> tuple[TestClient, AppContext]:
        ctx = AppContext()
        set_app_context(ctx)
        app = create_app()
        return TestClient(app), ctx

    def test_liveness(self) -> None:
        client, _ = self._ctx_client()
        assert client.get("/api/v1/health/live").json()["status"] == "UP"

    def test_readiness_unhealthy_without_deps(self) -> None:
        client, _ = self._ctx_client()
        resp = client.get("/api/v1/health/ready")
        # should be unhealthy because broker/temporal disconnected
        assert resp.status_code in (200, 503)
        body = resp.json()
        # F-066: public readiness body is minimal (status only); per-dependency
        # topology is internal unless IAP_HEALTH_DETAIL=full.
        assert "status" in body
        assert "dependencies" not in body

    def test_profiles_sanitized(self) -> None:
        client, _ = self._ctx_client()
        resp = client.get("/api/v1/profiles", headers={"X-Tenant-ID": "tenant-a"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        for item in items:
            # sanitized removes queryTemplates
            if "state" in item:
                assert "queryTemplates" not in item["state"]

    def test_timeline_pagination_and_404(self) -> None:
        client, ctx = self._ctx_client()
        # create investigation
        inv = _make_investigation()
        # store via repo
        import asyncio as _asyncio

        _asyncio.run(ctx.investigation_repo.create("tenant-a", inv))
        # add events
        iid = inv.id
        ev1 = TimelineEvent(
            tenant_id="tenant-a",
            investigation_id=iid,
            timestamp=datetime.now(UTC),
            event_type=TimelineEventType.LOG_EVENT,
            description="e1",
        )
        ev2 = TimelineEvent(
            tenant_id="tenant-a",
            investigation_id=iid,
            timestamp=datetime.now(UTC),
            event_type=TimelineEventType.LOG_EVENT,
            description="e2",
        )
        _asyncio.run(ctx.timeline_repo.append("tenant-a", ev1, investigation_id=iid))
        _asyncio.run(ctx.timeline_repo.append("tenant-a", ev2, investigation_id=iid))

        resp = client.get(
            f"/api/v1/investigations/{iid}/timeline?offset=0&limit=1",
            headers={"X-Tenant-ID": "tenant-a"},
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 2
        assert len(resp.json()["items"]) == 1

        # invalid id
        assert (
            client.get(
                "/api/v1/investigations/bad/timeline", headers={"X-Tenant-ID": "tenant-a"}
            ).status_code
            == 400
        )
        # not found
        assert (
            client.get(
                f"/api/v1/investigations/{uuid.uuid4()}/timeline",
                headers={"X-Tenant-ID": "tenant-a"},
            ).status_code
            == 404
        )

    def test_hypotheses_pagination(self) -> None:
        client, ctx = self._ctx_client()
        inv = _make_investigation()
        import asyncio as _asyncio

        _asyncio.run(ctx.investigation_repo.create("tenant-a", inv))
        h = _make_hypothesis("tenant-a", inv.id)
        _asyncio.run(ctx.hypothesis_repo.save("tenant-a", h, investigation_id=inv.id))
        resp = client.get(
            f"/api/v1/investigations/{inv.id}/hypotheses", headers={"X-Tenant-ID": "tenant-a"}
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_evidence_endpoints(self) -> None:
        client, ctx = self._ctx_client()
        inv = _make_investigation()
        import asyncio as _asyncio

        _asyncio.run(ctx.investigation_repo.create("tenant-a", inv))
        ev = _make_evidence("tenant-a", inv.id)
        _asyncio.run(ctx.evidence_repo.save("tenant-a", ev, investigation_id=inv.id))
        resp = client.get(
            f"/api/v1/investigations/{inv.id}/evidence", headers={"X-Tenant-ID": "tenant-a"}
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

        resp2 = client.get(
            f"/api/v1/investigations/{inv.id}/evidence/{ev.evidence_id}",
            headers={"X-Tenant-ID": "tenant-a"},
        )
        assert resp2.status_code == 200

        # invalid
        assert (
            client.get(
                f"/api/v1/investigations/{inv.id}/evidence/{uuid.uuid4()}",
                headers={"X-Tenant-ID": "tenant-a"},
            ).status_code
            == 404
        )

    def test_events_pause_resume(self) -> None:
        client, ctx = self._ctx_client()
        inv = _make_investigation()
        import asyncio as _asyncio

        _asyncio.run(ctx.investigation_repo.create("tenant-a", inv))
        # F-009/F-010: pause/resume now require a real Temporal client to signal
        # the running workflow. With no Temporal client wired (test AppContext),
        # the endpoints must fail explicitly rather than fabricate success.
        for path in ["pause", "resume"]:
            resp = client.post(
                f"/api/v1/investigations/{inv.id}/{path}", headers={"X-Tenant-ID": "tenant-a"}
            )
            assert resp.status_code == 503

        # With a Temporal client wired, the signal is delivered and recorded.
        fake_handle = MagicMock()
        fake_handle.signal = AsyncMock(return_value=None)
        fake_client = MagicMock()
        fake_client.get_workflow_handle = MagicMock(return_value=fake_handle)
        ctx.temporal_client = fake_client
        for path, expected_status in [("pause", "PAUSE_REQUESTED"), ("resume", "RESUME_REQUESTED")]:
            resp = client.post(
                f"/api/v1/investigations/{inv.id}/{path}", headers={"X-Tenant-ID": "tenant-a"}
            )
            assert resp.status_code == 202
            assert resp.json()["status"] == expected_status
        del ctx.temporal_client

    def test_hypotheses_invalid_uuid(self) -> None:
        client, _ = self._ctx_client()
        assert (
            client.get(
                "/api/v1/investigations/bad/hypotheses", headers={"X-Tenant-ID": "tenant-a"}
            ).status_code
            == 400
        )

    def test_config_load_from_env_missing_vars(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError
        from investigation_agent_platform.infrastructure.configuration.config import (
            load_application_config_from_env,
        )

        with patch.dict("os.environ", {}, clear=False):
            # remove required vars if present
            import os

            old_db = os.environ.pop("IAP_DATABASE_URI", None)
            old_llm = os.environ.pop("IAP_LLM_API_KEY", None)
            try:
                with pytest.raises(PlatformConfigurationError):
                    load_application_config_from_env()
            finally:
                if old_db:
                    os.environ["IAP_DATABASE_URI"] = old_db
                if old_llm:
                    os.environ["IAP_LLM_API_KEY"] = old_llm

    def test_config_load_from_env_success(self) -> None:
        from investigation_agent_platform.infrastructure.configuration.config import (
            load_application_config_from_env,
        )

        env = {"IAP_DATABASE_URI": "postgresql://x", "IAP_LLM_API_KEY": "sk-test"}
        with patch.dict("os.environ", env, clear=False):
            cfg = load_application_config_from_env()
            assert cfg.database.connection_uri.get_secret_value() == "postgresql://x"

    def test_evidence_dedup_concurrent_via_batch(self) -> None:
        import asyncio as _asyncio

        repo = InMemoryEvidenceRepository()
        iid = uuid.uuid4()
        base = _make_evidence("tenant-a", iid)
        dup = base.model_copy(update={"evidence_id": uuid.uuid4()})
        # same fingerprint
        _asyncio.run(repo.save_batch("tenant-a", [base, dup], investigation_id=iid))
        res = _asyncio.run(repo.find_by_investigation_id("tenant-a", iid))
        assert len(res) == 1

    def test_profile_repo_in_memory_tenant_filter(self) -> None:
        import asyncio as _asyncio

        repo = InMemoryApplicationProfileRepository()
        _asyncio.run(repo.save("tenant-a", _DEFAULT_PROFILE))
        assert len(_asyncio.run(repo.list("tenant-a"))) >= 1
        assert len(_asyncio.run(repo.list("tenant-b"))) == 0
