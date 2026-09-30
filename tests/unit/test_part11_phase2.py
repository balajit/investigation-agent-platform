# tests/unit/test_part11_phase2.py
"""Part 11 Phase 11.2 tests: finding/conclusion persistence + profile revisions.

Covers: in-memory finding/conclusion behavior + tenant isolation, SQL repo
tenant scoping (mocked rls_session, repo convention), immutable profile
revisions, investigation-to-profile binding, conclusion/findings endpoints,
profile sanitization, and the 009 migration chain. No live infrastructure:
SQL tests use AsyncMock sessions; RLS behavior is proven by asserting the
tenant is bound at the session and in every predicate path exercised.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import (
    _DEFAULT_PROFILE,
    AppContext,
    set_app_context,
)
from investigation_agent_platform.domain.common.exceptions import ConcurrencyError
from investigation_agent_platform.domain.finding.models import (
    ConclusionStatus,
    Finding,
    FindingType,
    InvestigationConclusion,
)
from investigation_agent_platform.domain.investigation.models import InvestigationRequest


@pytest.fixture
def _restore_context():
    import investigation_agent_platform.api.dependencies as deps

    prev = deps._context
    yield
    deps._context = prev


def _finding(tenant_id: str = "tenant-a", investigation_id=None, **overrides) -> Finding:
    params: dict = {
        "tenant_id": tenant_id,
        "investigation_id": investigation_id or uuid4(),
        "finding_type": FindingType.OBSERVATION,
        "title": "Odd latency spike",
        "statement": "p99 latency tripled after deploy.",
    }
    params.update(overrides)
    return Finding(**params)


def _conclusion(
    tenant_id: str = "tenant-a", investigation_id=None, **overrides
) -> InvestigationConclusion:
    params: dict = {
        "tenant_id": tenant_id,
        "investigation_id": investigation_id or uuid4(),
        "status": ConclusionStatus.ROOT_CAUSE_LIKELY,
        "root_cause": "Connection pool exhausted by retry storm.",
        "supporting_evidence_ids": [uuid4()],
        "confidence": 0.72,
    }
    params.update(overrides)
    return InvestigationConclusion(**params)


def _profile(version: int = 1, app_id: str = "example-app"):
    return _DEFAULT_PROFILE.model_copy(update={"id": app_id, "version": version})


@asynccontextmanager
async def _mock_rls(module_path: str, session: MagicMock, tenant_id: str = "tenant-a"):
    """Patch a repo module's rls_session; assert the tenant is bound."""
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)
    with patch(f"{module_path}.rls_session", return_value=cm) as mock_rls:
        yield session
        mock_rls.assert_called()
        assert mock_rls.call_args.args[1] == tenant_id


def _mock_session(scalars_first=None, scalars_all=None, scalar_value=None) -> MagicMock:
    session = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.first.return_value = scalars_first
    mock_scalars.all.return_value = scalars_all or []
    session.scalars = AsyncMock(return_value=mock_scalars)
    session.scalar = AsyncMock(return_value=scalar_value)
    session.execute = AsyncMock(return_value=MagicMock())
    session.add = MagicMock()
    return session


# ===========================================================================
# In-memory finding/conclusion repository
# ===========================================================================


class TestInMemoryFindingRepo:
    @pytest.mark.asyncio
    async def test_conclusion_round_trip(self) -> None:
        from investigation_agent_platform.api.dependencies import (
            InMemoryFindingConclusionRepository,
        )

        repo = InMemoryFindingConclusionRepository()
        inv_id = uuid4()
        await repo.save_conclusion("tenant-a", _conclusion("tenant-a", inv_id))
        got = await repo.get_conclusion("tenant-a", inv_id)
        assert got is not None
        assert got.root_cause == "Connection pool exhausted by retry storm."
        assert got.confidence == 0.72

    @pytest.mark.asyncio
    async def test_conclusion_cross_tenant_isolated(self) -> None:
        from investigation_agent_platform.api.dependencies import (
            InMemoryFindingConclusionRepository,
        )

        repo = InMemoryFindingConclusionRepository()
        inv_id = uuid4()
        await repo.save_conclusion("tenant-a", _conclusion("tenant-a", inv_id))
        assert await repo.get_conclusion("tenant-b", inv_id) is None

    @pytest.mark.asyncio
    async def test_conclusion_upsert_latest_wins(self) -> None:
        from investigation_agent_platform.api.dependencies import (
            InMemoryFindingConclusionRepository,
        )

        repo = InMemoryFindingConclusionRepository()
        inv_id = uuid4()
        await repo.save_conclusion("tenant-a", _conclusion("tenant-a", inv_id))
        await repo.save_conclusion("tenant-a", _conclusion("tenant-a", inv_id, confidence=0.91))
        got = await repo.get_conclusion("tenant-a", inv_id)
        assert got is not None
        assert got.confidence == 0.91

    @pytest.mark.asyncio
    async def test_conclusion_tenant_mismatch_rejected(self) -> None:
        from investigation_agent_platform.api.dependencies import (
            InMemoryFindingConclusionRepository,
        )

        repo = InMemoryFindingConclusionRepository()
        with pytest.raises(ConcurrencyError):
            await repo.save_conclusion("tenant-b", _conclusion("tenant-a", uuid4()))

    @pytest.mark.asyncio
    async def test_findings_list_pagination_and_filter(self) -> None:
        from investigation_agent_platform.api.dependencies import (
            InMemoryFindingConclusionRepository,
        )

        repo = InMemoryFindingConclusionRepository()
        inv_id = uuid4()
        for i in range(3):
            await repo.save_finding_record(
                "tenant-a", _finding("tenant-a", inv_id, title=f"obs-{i}")
            )
        await repo.save_finding_record(
            "tenant-a",
            _finding(
                "tenant-a",
                inv_id,
                finding_type=FindingType.ANOMALY,
                title="anomaly-0",
            ),
        )
        items, total = await repo.list_findings("tenant-a", inv_id)
        assert total == 4
        assert len(items) == 4
        page, total2 = await repo.list_findings("tenant-a", inv_id, limit=2, offset=2)
        assert total2 == 4
        assert len(page) == 2
        anomalies, anomaly_total = await repo.list_findings(
            "tenant-a", inv_id, finding_type="ANOMALY"
        )
        assert anomaly_total == 1
        assert anomalies[0].title == "anomaly-0"

    @pytest.mark.asyncio
    async def test_findings_cross_tenant_isolated(self) -> None:
        from investigation_agent_platform.api.dependencies import (
            InMemoryFindingConclusionRepository,
        )

        repo = InMemoryFindingConclusionRepository()
        inv_id = uuid4()
        finding = _finding("tenant-a", inv_id)
        await repo.save_finding_record("tenant-a", finding)
        items, total = await repo.list_findings("tenant-b", inv_id)
        assert total == 0
        assert items == []
        assert await repo.get_findings_by_ids("tenant-b", [finding.id]) == []

    @pytest.mark.asyncio
    async def test_get_findings_by_ids(self) -> None:
        from investigation_agent_platform.api.dependencies import (
            InMemoryFindingConclusionRepository,
        )

        repo = InMemoryFindingConclusionRepository()
        inv_id = uuid4()
        first = _finding("tenant-a", inv_id, title="first")
        second = _finding("tenant-a", inv_id, title="second")
        await repo.save_finding_record("tenant-a", first)
        await repo.save_finding_record("tenant-a", second)
        got = await repo.get_findings_by_ids("tenant-a", [first.id, second.id])
        assert {f.title for f in got} == {"first", "second"}
        assert await repo.get_findings_by_ids("tenant-a", []) == []

    @pytest.mark.asyncio
    async def test_finding_tenant_mismatch_rejected(self) -> None:
        from investigation_agent_platform.api.dependencies import (
            InMemoryFindingConclusionRepository,
        )

        repo = InMemoryFindingConclusionRepository()
        with pytest.raises(ConcurrencyError):
            await repo.save_finding_record("tenant-b", _finding("tenant-a", uuid4()))

    @pytest.mark.asyncio
    async def test_legacy_dict_path_stores_minimal_row(self) -> None:
        from investigation_agent_platform.api.dependencies import (
            InMemoryFindingConclusionRepository,
        )

        repo = InMemoryFindingConclusionRepository()
        inv_id = uuid4()
        await repo.save_finding(
            "tenant-a",
            inv_id,
            "OBSERVATION",
            {"title": "legacy", "statement": "from dict path"},
        )
        items, total = await repo.list_findings("tenant-a", inv_id)
        assert total == 1
        assert items[0].title == "legacy"


# ===========================================================================
# SQL finding/conclusion repository: tenant scoping (mocked sessions)
# ===========================================================================


class TestSqlFindingRepoScope:
    @pytest.mark.asyncio
    async def test_save_finding_record_scoped(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.finding_repository import (
            SqlAlchemyFindingConclusionRepository,
        )

        session = _mock_session()
        async with _mock_rls(
            "investigation_agent_platform.infrastructure.persistence.finding_repository",
            session,
        ):
            repo = SqlAlchemyFindingConclusionRepository(db_session_factory=MagicMock())
            await repo.save_finding_record("tenant-a", _finding("tenant-a", uuid4()))
            assert session.execute.called

    @pytest.mark.asyncio
    async def test_save_conclusion_scoped(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.finding_repository import (
            SqlAlchemyFindingConclusionRepository,
        )

        session = _mock_session()
        async with _mock_rls(
            "investigation_agent_platform.infrastructure.persistence.finding_repository",
            session,
        ):
            repo = SqlAlchemyFindingConclusionRepository(db_session_factory=MagicMock())
            await repo.save_conclusion("tenant-a", _conclusion("tenant-a", uuid4()))
            assert session.execute.called

    @pytest.mark.asyncio
    async def test_get_conclusion_empty(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.finding_repository import (
            SqlAlchemyFindingConclusionRepository,
        )

        session = _mock_session(scalars_first=None)
        async with _mock_rls(
            "investigation_agent_platform.infrastructure.persistence.finding_repository",
            session,
        ):
            repo = SqlAlchemyFindingConclusionRepository(db_session_factory=MagicMock())
            assert await repo.get_conclusion("tenant-a", uuid4()) is None
            assert session.scalars.called

    @pytest.mark.asyncio
    async def test_list_findings_empty(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.finding_repository import (
            SqlAlchemyFindingConclusionRepository,
        )

        session = _mock_session(scalars_all=[], scalar_value=0)
        async with _mock_rls(
            "investigation_agent_platform.infrastructure.persistence.finding_repository",
            session,
        ):
            repo = SqlAlchemyFindingConclusionRepository(db_session_factory=MagicMock())
            items, total = await repo.list_findings("tenant-a", uuid4())
            assert items == []
            assert total == 0

    @pytest.mark.asyncio
    async def test_tenant_mismatch_rejected_before_io(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.finding_repository import (
            SqlAlchemyFindingConclusionRepository,
        )

        session = _mock_session()
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch(
            "investigation_agent_platform.infrastructure.persistence"
            ".finding_repository.rls_session",
            return_value=cm,
        ) as mock_rls:
            repo = SqlAlchemyFindingConclusionRepository(db_session_factory=MagicMock())
            with pytest.raises(ConcurrencyError):
                await repo.save_finding_record("tenant-b", _finding("tenant-a", uuid4()))
            assert not mock_rls.called
            assert not session.execute.called


# ===========================================================================
# SQL profile repository: tenant scoping + immutable revisions
# ===========================================================================


class TestSqlProfileRepoScope:
    @pytest.mark.asyncio
    async def test_get_latest_scoped(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.profile_repository import (
            SqlAlchemyApplicationProfileRepository,
        )

        session = _mock_session(scalars_first=None)
        async with _mock_rls(
            "investigation_agent_platform.infrastructure.persistence.profile_repository",
            session,
        ):
            repo = SqlAlchemyApplicationProfileRepository(db_session_factory=MagicMock())
            assert await repo.get_by_application_id("tenant-a", "example-app") is None
            assert session.scalars.called

    @pytest.mark.asyncio
    async def test_save_executes_upsert(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.profile_repository import (
            SqlAlchemyApplicationProfileRepository,
        )

        session = _mock_session()
        async with _mock_rls(
            "investigation_agent_platform.infrastructure.persistence.profile_repository",
            session,
        ):
            repo = SqlAlchemyApplicationProfileRepository(db_session_factory=MagicMock())
            await repo.save("tenant-a", _profile(version=3))
            assert session.execute.called

    @pytest.mark.asyncio
    async def test_list_scoped(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.profile_repository import (
            SqlAlchemyApplicationProfileRepository,
        )

        session = _mock_session(scalars_all=[])
        async with _mock_rls(
            "investigation_agent_platform.infrastructure.persistence.profile_repository",
            session,
        ):
            repo = SqlAlchemyApplicationProfileRepository(db_session_factory=MagicMock())
            assert await repo.list("tenant-a") == []
            assert session.scalars.called


# ===========================================================================
# In-memory profile revisions
# ===========================================================================


class TestInMemoryProfileRevisions:
    @pytest.mark.asyncio
    async def test_latest_resolution(self) -> None:
        from investigation_agent_platform.api.dependencies import (
            InMemoryApplicationProfileRepository,
        )

        repo = InMemoryApplicationProfileRepository()
        await repo.save("tenant-a", _profile(version=1))
        await repo.save("tenant-a", _profile(version=2))
        got = await repo.get_by_application_id("tenant-a", "example-app")
        assert got is not None
        assert got.version == 2

    @pytest.mark.asyncio
    async def test_exact_version_and_mismatch(self) -> None:
        from investigation_agent_platform.api.dependencies import (
            InMemoryApplicationProfileRepository,
        )

        repo = InMemoryApplicationProfileRepository()
        await repo.save("tenant-a", _profile(version=1))
        await repo.save("tenant-a", _profile(version=2))
        old = await repo.get_by_application_id("tenant-a", "example-app", version="1")
        assert old is not None
        assert old.version == 1
        assert await repo.get_by_application_id("tenant-a", "example-app", version="9") is None
        assert await repo.get_by_application_id("tenant-a", "example-app", version="x") is None

    @pytest.mark.asyncio
    async def test_two_tenants_share_app_id(self) -> None:
        from investigation_agent_platform.api.dependencies import (
            InMemoryApplicationProfileRepository,
        )

        repo = InMemoryApplicationProfileRepository()
        await repo.save("tenant-a", _profile(version=1))
        await repo.save("tenant-b", _profile(version=5))
        got_a = await repo.get_by_application_id("tenant-a", "example-app")
        got_b = await repo.get_by_application_id("tenant-b", "example-app")
        assert got_a is not None and got_a.version == 1
        assert got_b is not None and got_b.version == 5

    @pytest.mark.asyncio
    async def test_list_revisions_newest_first(self) -> None:
        from investigation_agent_platform.api.dependencies import (
            InMemoryApplicationProfileRepository,
        )

        repo = InMemoryApplicationProfileRepository()
        await repo.save("tenant-a", _profile(version=1))
        await repo.save("tenant-a", _profile(version=3))
        await repo.save("tenant-a", _profile(version=2))
        revisions = await repo.list_revisions("tenant-a", "example-app")
        assert [p.version for p in revisions] == [3, 2, 1]
        assert await repo.list_revisions("tenant-b", "example-app") == []

    @pytest.mark.asyncio
    async def test_resubmission_is_noop(self) -> None:
        """Same (tenant, id, version) resubmission never overwrites history."""
        from investigation_agent_platform.api.dependencies import (
            InMemoryApplicationProfileRepository,
        )

        repo = InMemoryApplicationProfileRepository()
        first = _profile(version=1)
        await repo.save("tenant-a", first)
        altered = first.model_copy(update={"description": "mutated"})
        await repo.save("tenant-a", altered)
        got = await repo.get_by_application_id("tenant-a", "example-app", version="1")
        assert got is not None
        assert got.description == first.description
        latest = await repo.get_by_application_id("tenant-a", "example-app")
        assert latest is not None
        assert latest.description == first.description


# ===========================================================================
# Investigation-to-profile binding + metadata round-trip
# ===========================================================================


class TestInvestigationProfileBinding:
    @pytest.mark.asyncio
    async def test_metadata_pins_profile_version(self) -> None:
        ctx = AppContext()
        await ctx.profile_repo.save("tenant-a", _profile(version=4))
        req = InvestigationRequest(
            application_id="example-app",
            problem_description="binding check",
            session_id="sess-bind",
            requested_by="tester",
        )
        inv = await ctx.create_investigation_service().execute(req, tenant_id="tenant-a")
        assert inv.metadata.get("profile_version") == 4

    @pytest.mark.asyncio
    async def test_binding_survives_newer_revision(self) -> None:
        ctx = AppContext()
        await ctx.profile_repo.save("tenant-a", _profile(version=1))
        req = InvestigationRequest(
            application_id="example-app",
            problem_description="pinned check",
            session_id="sess-pin",
            requested_by="tester",
        )
        inv = await ctx.create_investigation_service().execute(req, tenant_id="tenant-a")
        await ctx.profile_repo.save("tenant-a", _profile(version=2))
        reloaded = await ctx.get_investigation_service().execute("tenant-a", inv.id)
        assert reloaded is not None
        assert reloaded.metadata.get("profile_version") == 1

    def test_metadata_json_round_trip(self) -> None:
        import datetime

        from investigation_agent_platform.domain.investigation.models import (
            Investigation,
            InvestigationContext,
        )
        from investigation_agent_platform.infrastructure.persistence.investigation_repository import (
            SqlAlchemyInvestigationRepository,
        )

        now = datetime.datetime.now(datetime.UTC)
        inv = Investigation(
            session_id="sess-rt",
            application_id="example-app",
            tenant_id="tenant-a",
            request=InvestigationRequest(
                application_id="example-app",
                problem_description="round trip",
                session_id="sess-rt",
                requested_by="tester",
            ),
            context=InvestigationContext(environment="dev", time_window=(now, now)),
            metadata={"profile_version": 7, "note": "keep me"},
        )

        orm = SqlAlchemyInvestigationRepository._to_orm(inv)
        assert orm.metadata_json == {"profile_version": 7, "note": "keep me"}
        back = SqlAlchemyInvestigationRepository._from_orm(orm)
        assert back.metadata == {"profile_version": 7, "note": "keep me"}


# ===========================================================================
# Conclude service persistence
# ===========================================================================


class TestConcludeService:
    @pytest.mark.asyncio
    async def test_execute_persists_retrievable_finding(self) -> None:
        from investigation_agent_platform.application.investigation.services import (
            ConcludeInvestigationService,
        )

        ctx = AppContext()
        svc = ConcludeInvestigationService(
            investigation_repo=ctx.investigation_repo,
            finding_repo=ctx.finding_repo,
        )
        inv_id = uuid4()
        finding = _finding("tenant-a", inv_id)
        await svc.execute("tenant-a", inv_id, finding)
        items, total = await ctx.finding_repo.list_findings("tenant-a", inv_id)
        assert total == 1
        assert items[0].id == finding.id


# ===========================================================================
# Conclusion + findings endpoints
# ===========================================================================


class TestConclusionEndpoints:
    def _client_with_investigation(self, tenant_id: str = "tenant-a"):
        ctx = AppContext()
        set_app_context(ctx)
        return ctx, TestClient(create_app())

    @pytest.mark.asyncio
    async def test_conclusion_null_while_unconcluded(self, _restore_context: None) -> None:
        ctx, client = self._client_with_investigation()
        req = InvestigationRequest(
            application_id="example-app",
            problem_description="no conclusion yet",
            session_id="sess-nc",
            requested_by="tester",
        )
        inv = await ctx.create_investigation_service().execute(req, tenant_id="tenant-a")
        resp = client.get(
            f"/api/v1/investigations/{inv.id}/conclusion",
            headers={"X-Tenant-ID": "tenant-a"},
        )
        assert resp.status_code == 200
        assert resp.json()["conclusion"] is None

    @pytest.mark.asyncio
    async def test_conclusion_returns_persisted(self, _restore_context: None) -> None:
        ctx, client = self._client_with_investigation()
        req = InvestigationRequest(
            application_id="example-app",
            problem_description="concluded",
            session_id="sess-c",
            requested_by="tester",
        )
        inv = await ctx.create_investigation_service().execute(req, tenant_id="tenant-a")
        await ctx.finding_repo.save_conclusion("tenant-a", _conclusion("tenant-a", inv.id))
        resp = client.get(
            f"/api/v1/investigations/{inv.id}/conclusion",
            headers={"X-Tenant-ID": "tenant-a"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["conclusion"] is not None
        assert body["conclusion"]["root_cause"] == "Connection pool exhausted by retry storm."

    @pytest.mark.asyncio
    async def test_conclusion_cross_tenant_404(self, _restore_context: None) -> None:
        ctx, client = self._client_with_investigation()
        req = InvestigationRequest(
            application_id="example-app",
            problem_description="tenant wall",
            session_id="sess-wall",
            requested_by="tester",
        )
        inv = await ctx.create_investigation_service().execute(req, tenant_id="tenant-a")
        resp = client.get(
            f"/api/v1/investigations/{inv.id}/conclusion",
            headers={"X-Tenant-ID": "tenant-b"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_findings_endpoint_paginated(self, _restore_context: None) -> None:
        ctx, client = self._client_with_investigation()
        req = InvestigationRequest(
            application_id="example-app",
            problem_description="lister",
            session_id="sess-l",
            requested_by="tester",
        )
        inv = await ctx.create_investigation_service().execute(req, tenant_id="tenant-a")
        for i in range(3):
            await ctx.finding_repo.save_finding_record(
                "tenant-a", _finding("tenant-a", inv.id, title=f"f-{i}")
            )
        resp = client.get(
            f"/api/v1/investigations/{inv.id}/findings?limit=2&offset=1",
            headers={"X-Tenant-ID": "tenant-a"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert len(body["items"]) == 2
        assert body["offset"] == 1
        assert body["limit"] == 2

    @pytest.mark.asyncio
    async def test_profiles_sanitize_query_templates(self, _restore_context: None) -> None:
        _, client = self._client_with_investigation()
        resp = client.get("/api/v1/profiles", headers={"X-Tenant-ID": "tenant-a"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert items, "expected seeded example-app profile"
        for item in items:
            assert "state" in item, "aliased state block must be present"
            assert "state_configuration" not in item
            state = item["state"]
            assert "queryTemplates" not in state
            assert "connectionUrl" not in state


# ===========================================================================
# Migration 009 chain
# ===========================================================================


class TestMigration009:
    def test_009_chain(self) -> None:
        import importlib.util
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[2]
            / "migrations"
            / "versions"
            / "009_profile_revisions_and_investigation_metadata.py"
        )
        assert path.is_file()
        spec = importlib.util.spec_from_file_location("migration_009", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.revision == "009_profile_revisions_and_investigation_metadata"
        assert mod.down_revision == "008_evidence_observed_at_nullable"

    def test_009_remains_ancestor(self) -> None:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config("alembic.ini")
        script = ScriptDirectory.from_config(cfg)
        rev = script.get_revision("009_profile_revisions_and_investigation_metadata")
        assert rev is not None
        assert rev.down_revision == "008_evidence_observed_at_nullable"
        # Walk down from the current head: 009 must still be in the chain.
        chain: list[str] = []
        current = script.get_revision(script.get_heads()[0])
        while current is not None:
            chain.append(current.revision)
            down = current.down_revision
            current = script.get_revision(down) if down else None
        assert "009_profile_revisions_and_investigation_metadata" in chain
