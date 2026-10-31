"""R12A: knowledge `_transition` is an insert-only save.

T02 pins the foreign-row exclusion against a counting fake (unit).
T01/T03 run against a disposable Postgres (db-suite): the transition
persists and the janitor sweep persists it too. Written first, red
before the fix.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest


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


class _CountingRepo:
    def __init__(self, rows=None):  # type: ignore[no-untyped-def]
        self._rows = {a.id: a for a in (rows or [])}
        self.saves = 0
        self.updates: list = []

    async def save(self, tenant_id, artifact):  # type: ignore[no-untyped-def]
        self.saves += 1
        self._rows[artifact.id] = artifact

    async def update_status(self, tenant_id, artifact_id, status):  # type: ignore[no-untyped-def]
        self.updates.append((tenant_id, artifact_id, status))
        row = self._rows.get(artifact_id)
        if row is None or row.tenant_id != tenant_id:
            return False
        self._rows[artifact_id] = row.model_copy(update={"status": status})
        return True

    async def get_by_id(self, tenant_id, artifact_id):  # type: ignore[no-untyped-def]
        return self._rows.get(artifact_id)

    async def list_expired_ttl(self, tenant_id, cutoff, limit=500):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.domain.knowledge.models import ArtifactStatus

        return [
            a
            for a in self._rows.values()
            if a.tenant_id == tenant_id
            and a.status == ArtifactStatus.ACTIVE
            and a.valid_to is not None
            and a.valid_to <= cutoff
        ][:limit]


def _service(repo, now):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.knowledge.retrieve import (
        KnowledgeRetrievalService,
    )

    return KnowledgeRetrievalService(artifact_repo=repo, clock_now=lambda: now)


@pytest.mark.asyncio
async def test_r12a_t02_foreign_shared_row_is_never_written() -> None:
    from investigation_agent_platform.domain.knowledge.models import (
        ArtifactStatus,
        ArtifactVisibility,
    )

    now = datetime.now(UTC)
    foreign = _ttl_artifact(
        tenant_id="tenant-a",
        visibility=ArtifactVisibility.SHARED_CODE_ISSUE,
        code_issue_fingerprint="fp-shared-1",
        valid_from=now - timedelta(days=2),
        valid_to=now - timedelta(days=1),
    )
    repo = _CountingRepo([foreign])
    service = _service(repo, now)
    await service._transition("tenant-b", foreign, ArtifactStatus.EXPIRED)
    assert repo.saves == 0
    assert repo.updates == []
    own = _ttl_artifact(tenant_id="tenant-b")
    repo2 = _CountingRepo([own])
    await _service(repo2, now)._transition("tenant-b", own, ArtifactStatus.EXPIRED)
    assert repo2.saves == 0
    assert len(repo2.updates) == 1


def _orm_tables():  # type: ignore[no-untyped-def]
    from investigation_agent_platform.infrastructure.persistence.models import (
        InvestigationORM,
        KnowledgeArtifactORM,
    )

    return InvestigationORM, KnowledgeArtifactORM


@pytest.mark.asyncio
async def test_r12a_t01_transition_persists(disposable_db) -> None:  # type: ignore[no-untyped-def]
    from r12_disposable_db import dispose_engine, make_session_factory

    from investigation_agent_platform.application.knowledge.retrieve import (
        KnowledgeRetrievalService,
    )
    from investigation_agent_platform.domain.knowledge.models import ArtifactStatus
    from investigation_agent_platform.infrastructure.persistence.knowledge_repository import (
        SqlAlchemyArtifactRepository,
    )

    InvestigationORM, KnowledgeArtifactORM = _orm_tables()
    factory, engine = make_session_factory(disposable_db)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                InvestigationORM.__table__.create, checkfirst=True
            )
            await conn.run_sync(
                KnowledgeArtifactORM.__table__.create, checkfirst=True
            )
        inv_id = uuid.uuid4()
        async with factory() as session:
            session.add(
                InvestigationORM(
                    id=inv_id,
                    tenant_id="tenant-a",
                    application_id="app-1",
                    environment="test",
                    title="t",
                    problem_description="d",
                    status="RUNNING",
                    phase="GATHER",
                    created_by="test",
                    session_id="s-1",
                )
            )
            await session.commit()
        now = datetime.now(UTC)
        artifact = _ttl_artifact(
            investigation_id=inv_id,
            valid_from=now - timedelta(days=2),
            valid_to=now - timedelta(days=1),
        )
        repo = SqlAlchemyArtifactRepository(factory)
        await repo.save("tenant-a", artifact)
        service = KnowledgeRetrievalService(artifact_repo=repo, clock_now=lambda: now)
        await service.retrieve_for_reasoning("tenant-a", "app-1", uuid.uuid4())
        stored = await repo.get_by_id("tenant-a", artifact.id)
        assert stored is not None and stored.status == ArtifactStatus.EXPIRED
        # Negative control: save stays insert-only — a second save of the
        # same id raises instead of overwriting.
        import sqlalchemy.exc

        with pytest.raises(sqlalchemy.exc.IntegrityError):
            await repo.save("tenant-a", artifact)
    finally:
        await dispose_engine(engine)


@pytest.mark.asyncio
async def test_r12a_t03_janitor_sweep_persists(disposable_db) -> None:  # type: ignore[no-untyped-def]
    from r12_disposable_db import dispose_engine, make_session_factory

    from investigation_agent_platform.application.knowledge.janitor import (
        KnowledgeArtifactJanitor,
    )
    from investigation_agent_platform.application.knowledge.retrieve import (
        KnowledgeRetrievalService,
    )
    from investigation_agent_platform.domain.knowledge.models import ArtifactStatus
    from investigation_agent_platform.infrastructure.persistence.knowledge_repository import (
        SqlAlchemyArtifactRepository,
    )

    InvestigationORM, _ = _orm_tables()
    from investigation_agent_platform.infrastructure.persistence.models import (
        KnowledgeArtifactORM,
    )

    factory, engine = make_session_factory(disposable_db)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                InvestigationORM.__table__.create, checkfirst=True
            )
            await conn.run_sync(
                KnowledgeArtifactORM.__table__.create, checkfirst=True
            )
        inv_id = uuid.uuid4()
        async with factory() as session:
            session.add(
                InvestigationORM(
                    id=inv_id,
                    tenant_id="tenant-a",
                    application_id="app-1",
                    environment="test",
                    title="t",
                    problem_description="d",
                    status="RUNNING",
                    phase="GATHER",
                    created_by="test",
                    session_id="s-1",
                )
            )
            await session.commit()
        now = datetime.now(UTC)
        past = _ttl_artifact(
            investigation_id=inv_id,
            valid_from=now - timedelta(days=2),
            valid_to=now - timedelta(days=1),
        )
        repo = SqlAlchemyArtifactRepository(factory)
        await repo.save("tenant-a", past)
        service = KnowledgeRetrievalService(artifact_repo=repo, clock_now=lambda: now)
        janitor = KnowledgeArtifactJanitor(repo, service)
        result = await janitor.sweep_tenant("tenant-a", cutoff=now)
        assert result.expired_count == 1
        remaining = await repo.list_active_for_reuse("tenant-a", "app-1")
        assert [a.id for a in remaining] == []
        stored = await repo.get_by_id("tenant-a", past.id)
        assert stored is not None and stored.status == ArtifactStatus.EXPIRED
    finally:
        await dispose_engine(engine)
