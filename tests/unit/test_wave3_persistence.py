"""Wave 3 persistence batch: mutation logs, tenant predicates, save_batch,
retention purge, Fact semantics, transition actor (TO_DO-008/012/014/015/023/024).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from pydantic import ValidationError

APP = "investigation_agent_platform.infrastructure.persistence"


def _make_investigation(tenant_id: str = "tenant-a"):
    from datetime import timedelta

    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationContext,
        InvestigationRequest,
        InvestigationStatus,
    )

    now = datetime.now(UTC)
    return Investigation(
        session_id="sess-1",
        application_id="example-app",
        tenant_id=tenant_id,
        request=InvestigationRequest(
            application_id="example-app",
            problem_description="incident desc",
            session_id="sess-1",
            requested_by="tester",
        ),
        context=InvestigationContext(
            environment="production",
            time_window=(now - timedelta(hours=1), now),
        ),
        status=InvestigationStatus.CREATED,
        created_at=now,
        updated_at=now,
        version=1,
    )


def _make_profile():
    from investigation_agent_platform.domain.profile.models import (
        ApplicationProfile,
        CorrelationConfig,
        Datasource,
        DatasourceKind,
        DatasourceRole,
        InvestigationConfig,
        QueryTemplate,
        Service,
        ServiceCode,
    )

    return ApplicationProfile(
        id="app1",
        tenant_id="tenant-a",
        name="n",
        description="d",
        environment="prod",
        services=[
            Service(
                name="svc",
                code=ServiceCode(
                    org="org",
                    repos=["repo"],
                    default_branch="main",
                    language="python",
                    source_roots=["src"],
                    build_system="uv",
                    module_structure="src/mod",
                ),
            )
        ],
        datasources=[
            Datasource(
                id="primary",
                name="db (pg)",
                kind=DatasourceKind.POSTGRES,
                role=DatasourceRole.STATE,
                allowed_tables=["t"],
                query_templates={
                    "q1": QueryTemplate(
                        description=None,
                        sql="SELECT * FROM t WHERE id=:id",
                        read_only=True,
                        allowed_tables=["t"],
                    )
                },
            )
        ],
        correlation=CorrelationConfig(fields=["trace.id"]),
        investigation=InvestigationConfig(),
    )


def _mock_rls(module: str, session: MagicMock) -> MagicMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)
    return patch(f"{module}.rls_session", return_value=cm)


def _session_with(scalars_result: MagicMock | None = None, execute_rowcount: int = 0) -> MagicMock:
    session = AsyncMock()
    if scalars_result is not None:
        session.scalars = AsyncMock(return_value=scalars_result)
    execute_result = MagicMock()
    execute_result.rowcount = execute_rowcount
    session.execute = AsyncMock(return_value=execute_result)
    return session


def _scalars(first: object = None, all_: list[object] | None = None) -> MagicMock:
    m = MagicMock()
    m.first.return_value = first
    m.all.return_value = all_ if all_ is not None else []
    return m


def _collect_adds(session: MagicMock) -> list[object]:
    """Capture session.add rows; helper avoids B023 loop-variable binding."""
    added: list[object] = []

    def _add(row: object) -> None:
        added.append(row)

    session.add = MagicMock(side_effect=_add)  # type: ignore[method-assign]
    return added


def _make_evidence(tenant_id: str = "tenant-a", fingerprint: str | None = None):
    from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
    from investigation_agent_platform.domain.provenance.models import (
        EvidenceFreshness,
        EvidenceProvenance,
        QueryFingerprint,
        SourceLocation,
    )

    now = datetime.now(UTC)
    inv_id = uuid.uuid4()
    return Evidence(
        tenant_id=tenant_id,
        investigation_id=inv_id,
        evidence_type=EvidenceType.LOG,
        provider="prov",
        source="urn:src",
        title="t",
        summary="s",
        fingerprint=fingerprint or f"fp-{uuid.uuid4()}",
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


def _make_timeline_event(tenant_id: str = "tenant-a", iid: UUID | None = None):
    from investigation_agent_platform.domain.timeline.models import (
        TimelineEvent,
        TimelineEventType,
    )

    return TimelineEvent(
        tenant_id=tenant_id,
        investigation_id=iid or uuid.uuid4(),
        timestamp=datetime.now(UTC),
        event_type=TimelineEventType.LOG_EVENT,
        description="d",
    )


# -- TO_DO-024: Fact semantics ---------------------------------------------


class TestFactSemantics:
    def test_blank_statement_rejected(self) -> None:
        from investigation_agent_platform.domain.investigation.models import (
            Fact,
            FactType,
        )

        with pytest.raises(ValidationError):
            Fact(
                tenant_id="t",
                investigation_id=uuid.uuid4(),
                statement="   ",
                fact_type=FactType.OBSERVATION,
            )

    def test_duplicate_evidence_ids_rejected(self) -> None:
        from investigation_agent_platform.domain.investigation.models import (
            Fact,
            FactType,
        )

        eid = uuid.uuid4()
        with pytest.raises(ValidationError):
            Fact(
                tenant_id="t",
                investigation_id=uuid.uuid4(),
                statement="observed",
                fact_type=FactType.OBSERVATION,
                source_evidence_ids=[eid, eid],
            )

    def test_fact_is_immutable(self) -> None:
        from investigation_agent_platform.domain.investigation.models import (
            Fact,
            FactType,
        )

        fact = Fact(
            tenant_id="t",
            investigation_id=uuid.uuid4(),
            statement="observed",
            fact_type=FactType.OBSERVATION,
        )
        assert fact.model_config.get("frozen") is True

    @pytest.mark.asyncio
    async def test_concurrent_appends_all_survive(self) -> None:
        """Append-only: two concurrent saves INSERT all fact rows, no UPDATE."""
        from investigation_agent_platform.domain.investigation.models import (
            Fact,
            FactType,
        )
        from investigation_agent_platform.infrastructure.persistence.investigation_repository import (
            SqlAlchemyInvestigationRepository,
        )

        def make_repo(session: MagicMock) -> SqlAlchemyInvestigationRepository:
            return SqlAlchemyInvestigationRepository(db_session_factory=MagicMock())

        for run in range(2):
            session = AsyncMock()
            session.scalars = AsyncMock()
            execute_result = MagicMock()
            execute_result.rowcount = 1
            session.execute = AsyncMock(return_value=execute_result)
            added = _collect_adds(session)
            cm = AsyncMock()
            cm.__aenter__ = AsyncMock(return_value=session)
            cm.__aexit__ = AsyncMock(return_value=None)
            inv = _make_investigation()
            fact = Fact(
                tenant_id="tenant-a",
                investigation_id=inv.id,
                statement=f"observation {run}",
                fact_type=FactType.OBSERVATION,
            )
            inv_with_fact = inv.model_copy(update={"facts": [fact]})
            with patch(f"{APP}.investigation_repository.rls_session", return_value=cm):
                repo = make_repo(session)
                await repo.save("tenant-a", inv_with_fact, expected_version=1)
            assert len(added) == 1
            # version-guarded UPDATE issued exactly once (the aggregate row);
            # facts travel via session.add (INSERT), never UPDATE.
            assert session.execute.await_count == 1


# -- TO_DO-015: save_batch constant-query ----------------------------------


class TestSaveBatch:
    @pytest.mark.asyncio
    async def test_batch_single_select_single_insert(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.evidence_repository import (
            SqlAlchemyEvidenceRepository,
        )

        items = [_make_evidence(fingerprint=f"fp-{i}") for i in range(5)]
        session = _session_with(scalars_result=_scalars(all_=[]))
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch(f"{APP}.evidence_repository.rls_session", return_value=cm):
            repo = SqlAlchemyEvidenceRepository(db_session_factory=MagicMock())
            await repo.save_batch("tenant-a", items, investigation_id=uuid.uuid4())
        # One bulk INSERT + one holder-map SELECT (constant queries);
        # shared-reference interest insert only fires on actual sharing.
        assert session.scalars.await_count == 0
        assert session.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_batch_dedupes_within_batch(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.evidence_repository import (
            SqlAlchemyEvidenceRepository,
        )

        items = [_make_evidence(fingerprint="fp-same") for _ in range(4)]
        session = _session_with(scalars_result=_scalars(all_=[]))
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch(f"{APP}.evidence_repository.rls_session", return_value=cm):
            repo = SqlAlchemyEvidenceRepository(db_session_factory=MagicMock())
            await repo.save_batch("tenant-a", items, investigation_id=uuid.uuid4())
        assert session.scalars.await_count == 0
        assert session.execute.await_count == 2
        stmt = session.execute.await_args_list[0].args[0]
        param_values = list(stmt.compile().params.values())
        assert param_values.count("fp-same") == 1

    @pytest.mark.asyncio
    async def test_batch_skips_already_stored(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.evidence_repository import (
            SqlAlchemyEvidenceRepository,
        )

        # Already-stored rows are skipped by the database itself (ON CONFLICT
        # DO NOTHING): the batch still issues its INSERT plus the holder-map
        # SELECT, and the conflicting row is ignored atomically instead of
        # raising.
        items = [_make_evidence(fingerprint="fp-known")]
        session = _session_with(scalars_result=_scalars(all_=["fp-known"]))
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch(f"{APP}.evidence_repository.rls_session", return_value=cm):
            repo = SqlAlchemyEvidenceRepository(db_session_factory=MagicMock())
            await repo.save_batch("tenant-a", items, investigation_id=uuid.uuid4())
        assert session.scalars.await_count == 0
        assert session.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_batch_logs_size_and_outcome(self, caplog: pytest.LogCaptureFixture) -> None:
        from investigation_agent_platform.infrastructure.persistence.evidence_repository import (
            SqlAlchemyEvidenceRepository,
        )

        items = [_make_evidence(fingerprint=f"fp-{i}") for i in range(3)]
        session = _session_with(scalars_result=_scalars(all_=[]))
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=None)
        with (
            caplog.at_level(logging.INFO),
            patch(f"{APP}.evidence_repository.rls_session", return_value=cm),
        ):
            repo = SqlAlchemyEvidenceRepository(db_session_factory=MagicMock())
            await repo.save_batch("tenant-a", items, investigation_id=uuid.uuid4())
        rows = [r for r in caplog.records if r.message == "evidence batch saved"]
        assert len(rows) == 1
        assert rows[0].tenant_id == "tenant-a"
        assert rows[0].event == "evidence.save_batch"
        assert rows[0].batch_size == 3
        assert rows[0].outcome == "inserted"


# -- TO_DO-008: mutation log rows ------------------------------------------


class TestMutationLogs:
    @pytest.mark.asyncio
    async def test_timeline_append_logs_ids(self, caplog: pytest.LogCaptureFixture) -> None:
        from investigation_agent_platform.infrastructure.persistence.timeline_repository import (
            SqlAlchemyTimelineRepository,
        )

        event = _make_timeline_event()
        iid = uuid.uuid4()
        session = AsyncMock()
        session.add = MagicMock()
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=None)
        with (
            caplog.at_level(logging.INFO),
            patch(f"{APP}.timeline_repository.rls_session", return_value=cm),
        ):
            repo = SqlAlchemyTimelineRepository(db_session_factory=MagicMock())
            await repo.append("tenant-a", event, investigation_id=iid)
        rows = [r for r in caplog.records if r.message == "timeline event appended"]
        assert len(rows) == 1
        assert rows[0].tenant_id == "tenant-a"
        assert rows[0].investigation_id == str(iid)
        assert rows[0].outcome == "appended"

    @pytest.mark.asyncio
    async def test_transition_record_logs_actor(self, caplog: pytest.LogCaptureFixture) -> None:
        from investigation_agent_platform.infrastructure.persistence.transition_repository import (
            SqlAlchemyTransitionRepository,
        )

        session = AsyncMock()
        added = _collect_adds(session)
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=None)
        iid = uuid.uuid4()
        with (
            caplog.at_level(logging.INFO),
            patch(f"{APP}.transition_repository.rls_session", return_value=cm),
        ):
            repo = SqlAlchemyTransitionRepository(db_session_factory=MagicMock())
            await repo.record_transition(
                "tenant-a", iid, "CREATED", "INVESTIGATING", "go", actor="USER"
            )
        assert added[0].actor == "USER"
        rows = [r for r in caplog.records if r.message == "transition recorded"]
        assert len(rows) == 1
        assert rows[0].tenant_id == "tenant-a"
        assert rows[0].actor == "USER"
        assert rows[0].outcome == "recorded"

    @pytest.mark.asyncio
    async def test_transition_actor_defaults_system(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.transition_repository import (
            SqlAlchemyTransitionRepository,
        )

        session = AsyncMock()
        added = _collect_adds(session)
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch(f"{APP}.transition_repository.rls_session", return_value=cm):
            repo = SqlAlchemyTransitionRepository(db_session_factory=MagicMock())
            await repo.record_transition("tenant-a", uuid.uuid4(), "CREATED", "INVESTIGATING", "go")
        assert added[0].actor == "SYSTEM"

    @pytest.mark.asyncio
    async def test_transition_actor_validated(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.transition_repository import (
            SqlAlchemyTransitionRepository,
        )

        repo = SqlAlchemyTransitionRepository(db_session_factory=MagicMock())
        with pytest.raises(ValueError):
            await repo.record_transition(
                "tenant-a", uuid.uuid4(), "CREATED", "INVESTIGATING", "go", actor=""
            )

    @pytest.mark.asyncio
    async def test_investigation_create_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        from investigation_agent_platform.infrastructure.persistence.investigation_repository import (
            SqlAlchemyInvestigationRepository,
        )

        inv = _make_investigation()
        session = AsyncMock()
        session.add = MagicMock()
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=None)
        with (
            caplog.at_level(logging.INFO),
            patch(f"{APP}.investigation_repository.rls_session", return_value=cm),
        ):
            repo = SqlAlchemyInvestigationRepository(db_session_factory=MagicMock())
            await repo.create("tenant-a", inv)
        rows = [r for r in caplog.records if r.message == "investigation created"]
        assert len(rows) == 1
        assert rows[0].tenant_id == "tenant-a"
        assert rows[0].investigation_id == str(inv.id)

    @pytest.mark.asyncio
    async def test_profile_save_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        from investigation_agent_platform.infrastructure.persistence.profile_repository import (
            SqlAlchemyApplicationProfileRepository,
        )

        profile = _make_profile()
        session = _session_with()
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=None)
        with (
            caplog.at_level(logging.INFO),
            patch(f"{APP}.profile_repository.rls_session", return_value=cm),
        ):
            repo = SqlAlchemyApplicationProfileRepository(db_session_factory=MagicMock())
            await repo.save("tenant-a", profile)
        rows = [r for r in caplog.records if r.message == "profile saved"]
        assert len(rows) == 1
        assert rows[0].tenant_id == "tenant-a"
        assert rows[0].outcome == "inserted"

    @pytest.mark.asyncio
    async def test_failure_path_uses_exception_log(self, caplog: pytest.LogCaptureFixture) -> None:
        from investigation_agent_platform.infrastructure.persistence.evidence_repository import (
            SqlAlchemyEvidenceRepository,
        )

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=RuntimeError("db down"))
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=None)
        with (
            caplog.at_level(logging.INFO),
            patch(f"{APP}.evidence_repository.rls_session", return_value=cm),
        ):
            repo = SqlAlchemyEvidenceRepository(db_session_factory=MagicMock())
            with pytest.raises(RuntimeError):
                await repo.save("tenant-a", _make_evidence())
        rows = [r for r in caplog.records if r.message == "evidence save failed"]
        assert len(rows) == 1
        assert rows[0].exc_info is not None
        assert rows[0].outcome == "failed"


# -- TO_DO-014: tenant predicates ------------------------------------------


class TestTenantPredicates:
    @pytest.mark.asyncio
    async def test_timeline_reads_scope_tenant(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.timeline_repository import (
            SqlAlchemyTimelineRepository,
        )

        for method, kwargs in [
            ("find_by_investigation_id", {}),
            (
                "find_by_time_range",
                {"start": datetime.now(UTC), "end": datetime.now(UTC)},
            ),
        ]:
            session = AsyncMock()
            session.scalars = AsyncMock(return_value=_scalars(all_=[]))
            cm = AsyncMock()
            cm.__aenter__ = AsyncMock(return_value=session)
            cm.__aexit__ = AsyncMock(return_value=None)
            with patch(f"{APP}.timeline_repository.rls_session", return_value=cm):
                repo = SqlAlchemyTimelineRepository(db_session_factory=MagicMock())
                await getattr(repo, method)("tenant-a", uuid.uuid4(), **kwargs)
            stmt = session.scalars.await_args.args[0]
            assert "tenant_id" in str(stmt)

    @pytest.mark.asyncio
    async def test_hypothesis_reads_scope_tenant(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.hypothesis_repository import (
            SqlAlchemyHypothesisRepository,
        )

        repo = SqlAlchemyHypothesisRepository(db_session_factory=MagicMock())
        session = AsyncMock()
        session.scalars = AsyncMock(return_value=_scalars(all_=[]))
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch(f"{APP}.hypothesis_repository.rls_session", return_value=cm):
            await repo.get_by_id("tenant-a", uuid.uuid4())
            await repo.find_by_investigation_id("tenant-a", uuid.uuid4())
        for call in session.scalars.await_args_list:
            assert "tenant_id" in str(call.args[0])

    @pytest.mark.asyncio
    async def test_knowledge_and_session_reads_scope_tenant(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.knowledge_repository import (
            SqlAlchemyArtifactRepository,
            SqlAlchemySessionRepository,
        )

        session = AsyncMock()
        session.scalars = AsyncMock(return_value=_scalars(all_=[]))
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch(f"{APP}.knowledge_repository.rls_session", return_value=cm):
            await SqlAlchemyArtifactRepository(
                db_session_factory=MagicMock()
            ).list_for_investigation("tenant-a", uuid.uuid4())
            await SqlAlchemySessionRepository(db_session_factory=MagicMock()).list_own_sessions(
                "tenant-a", uuid.uuid4()
            )
            await SqlAlchemySessionRepository(db_session_factory=MagicMock()).count_own_sessions(
                "tenant-a", uuid.uuid4()
            )
        for call in session.scalars.await_args_list:
            assert "tenant_id" in str(call.args[0])


# -- TO_DO-014: RLS migration -------------------------------------------------


class TestRlsMigration:
    REV = "migrations/versions/3354e515779a_core_retention_classes_and_rls_coverage.py"

    def test_wave3_migration_exists_with_force_rls(self) -> None:
        import pathlib

        path = pathlib.Path(self.REV)
        assert path.exists()
        text = path.read_text()
        assert "down_revision" in text and "016_profile_v2_columns" in text
        for table in (
            "investigation_facts",
            "investigation_entities",
            "investigation_conclusions",
            "application_profiles",
        ):
            assert table in text
        assert "FORCE ROW LEVEL SECURITY" in text
        assert "CREATE POLICY tenant_isolation" in text

    def test_wave3_migration_adds_retention_columns(self) -> None:
        import pathlib

        text = pathlib.Path(self.REV).read_text()
        for table, default in [
            ("investigations", "investigation"),
            ("evidence", "evidence"),
            ("timeline_events", "timeline"),
            ("investigation_transitions", "transition"),
        ]:
            assert table in text
            assert default in text
        assert "retention_class" in text


# -- TO_DO-023: retention --------------------------------------------------


class TestCoreRetention:
    def test_policy_registry_covers_core_tables(self) -> None:
        from investigation_agent_platform.domain.common.lifecycle import (
            CORE_RETENTION_POLICIES,
            CoreRetentionClass,
        )

        assert set(CORE_RETENTION_POLICIES) == {c.value for c in CoreRetentionClass}
        for policy in CORE_RETENTION_POLICIES.values():
            assert policy.purge_job_kind == "core-retention-purge"
            assert policy.retain_days > 0

    def test_retention_cutoff(self) -> None:
        from investigation_agent_platform.domain.common.lifecycle import (
            CORE_RETENTION_POLICIES,
            retention_cutoff,
        )

        now = datetime.now(UTC)
        cutoff = retention_cutoff(CORE_RETENTION_POLICIES["investigation"], now)
        assert cutoff == now - timedelta(days=365)

    @pytest.mark.asyncio
    async def test_purge_tenant_counts(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.retention import (
            CoreRetentionPurge,
        )

        now = datetime.now(UTC)
        inv_repo = AsyncMock()
        inv_repo.purge_expired = AsyncMock(return_value=2)
        ev_repo = AsyncMock()
        ev_repo.purge_expired = AsyncMock(return_value=5)
        tl_repo = AsyncMock()
        tl_repo.purge_expired = AsyncMock(return_value=7)
        driver = CoreRetentionPurge(inv_repo, ev_repo, tl_repo)
        result = await driver.purge_tenant("tenant-a", now=now, limit=100)
        assert result.investigations_purged == 2
        assert result.evidence_purged == 5
        assert result.timeline_purged == 7
        for repo in (inv_repo, ev_repo, tl_repo):
            args = repo.purge_expired.await_args.args
            assert args[0] == "tenant-a"
            assert args[1] <= now
            assert args[2] == 100

    @pytest.mark.asyncio
    async def test_purge_isolates_repository_failure(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.retention import (
            CoreRetentionPurge,
        )

        inv_repo = AsyncMock()
        inv_repo.purge_expired = AsyncMock(side_effect=RuntimeError("db down"))
        ev_repo = AsyncMock()
        ev_repo.purge_expired = AsyncMock(return_value=1)
        tl_repo = AsyncMock()
        tl_repo.purge_expired = AsyncMock(return_value=1)
        driver = CoreRetentionPurge(inv_repo, ev_repo, tl_repo)
        result = await driver.purge_tenant("tenant-a")
        assert result.investigations_purged == 0
        assert result.evidence_purged == 1
        assert result.timeline_purged == 1

    @pytest.mark.asyncio
    async def test_investigation_purge_only_terminal(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.investigation_repository import (
            SqlAlchemyInvestigationRepository,
        )

        cutoff = datetime.now(UTC)
        session = _session_with(scalars_result=_scalars(all_=[uuid.uuid4()]), execute_rowcount=1)
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch(f"{APP}.investigation_repository.rls_session", return_value=cm):
            repo = SqlAlchemyInvestigationRepository(db_session_factory=MagicMock())
            purged = await repo.purge_expired("tenant-a", cutoff)
        assert purged == 1
        # First scalars call is the purge select (later calls belong to the
        # reference-evidence rehome pass).
        select_stmt = session.scalars.await_args_list[0].args[0]
        text = str(select_stmt)
        assert "tenant_id" in text
        params = list(select_stmt.compile().params.values())
        assert ["COMPLETED", "FAILED", "CANCELLED"] in params
        delete_stmt = session.execute.await_args.args[0]
        assert "tenant_id" in str(delete_stmt)

    @pytest.mark.asyncio
    async def test_orphan_purge_scopes_tenant_and_cutoff(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.evidence_repository import (
            SqlAlchemyEvidenceRepository,
        )
        from investigation_agent_platform.infrastructure.persistence.timeline_repository import (
            SqlAlchemyTimelineRepository,
        )

        cutoff = datetime.now(UTC)
        for repo_cls, module in [
            (SqlAlchemyEvidenceRepository, "evidence_repository"),
            (SqlAlchemyTimelineRepository, "timeline_repository"),
        ]:
            session = _session_with(scalars_result=_scalars(all_=[]))
            cm = AsyncMock()
            cm.__aenter__ = AsyncMock(return_value=session)
            cm.__aexit__ = AsyncMock(return_value=None)
            with patch(f"{APP}.{module}.rls_session", return_value=cm):
                purged = await repo_cls(db_session_factory=MagicMock()).purge_expired(
                    "tenant-a", cutoff
                )
            assert purged == 0
            assert session.execute.await_count == 0
            text = str(session.scalars.await_args.args[0])
            assert "tenant_id" in text
