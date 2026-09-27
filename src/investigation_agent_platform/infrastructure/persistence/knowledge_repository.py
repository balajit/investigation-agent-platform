# src/investigation_agent_platform/infrastructure/persistence/knowledge_repository.py
"""PostgreSQL knowledge repositories: artifacts, sessions, code-issue index (Part 6 Slice 0)."""

import logging
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select

from investigation_agent_platform.domain.knowledge.models import (
    ArtifactStatus,
    ArtifactVisibility,
    InvestigationSession,
    KnowledgeArtifact,
    RefreshPolicy,
    ReverifySpec,
)
from investigation_agent_platform.infrastructure.persistence.models import (
    CodeIssueIndexORM,
    InvestigationSessionORM,
    KnowledgeArtifactORM,
)
from investigation_agent_platform.infrastructure.persistence.rls import rls_session

logger = logging.getLogger(__name__)


def _artifact_to_orm(artifact: KnowledgeArtifact) -> KnowledgeArtifactORM:
    return KnowledgeArtifactORM(
        id=artifact.id,
        tenant_id=artifact.tenant_id,
        application_id=artifact.application_id,
        investigation_id=artifact.investigation_id,
        kind=artifact.kind,
        statement=artifact.statement,
        confidence=artifact.confidence,
        refresh_policy=artifact.refresh_policy.value,
        reverify_json=artifact.reverify.model_dump(mode="json") if artifact.reverify else None,
        valid_from=artifact.valid_from,
        valid_to=artifact.valid_to,
        source_evidence_ids=[str(e) for e in artifact.source_evidence_ids],
        source_log_refs=list(artifact.source_log_refs),
        code_refs=list(artifact.code_refs),
        supersedes_id=artifact.supersedes_id,
        status=artifact.status.value,
        store_refs=dict(artifact.store_refs),
        visibility=artifact.visibility.value,
        code_issue_fingerprint=artifact.code_issue_fingerprint,
    )


def _artifact_from_orm(row: KnowledgeArtifactORM) -> KnowledgeArtifact:
    return KnowledgeArtifact(
        id=row.id,
        tenant_id=row.tenant_id,
        application_id=row.application_id,
        investigation_id=row.investigation_id,
        kind=row.kind,
        statement=row.statement,
        confidence=row.confidence,
        refresh_policy=RefreshPolicy(row.refresh_policy),
        reverify=ReverifySpec.model_validate(row.reverify_json) if row.reverify_json else None,
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        source_evidence_ids=[UUID(e) for e in (row.source_evidence_ids or [])],
        source_log_refs=list(row.source_log_refs or []),
        code_refs=list(row.code_refs or []),
        supersedes_id=row.supersedes_id,
        status=ArtifactStatus(row.status),
        store_refs=dict(row.store_refs or {}),
        visibility=ArtifactVisibility(row.visibility),
        code_issue_fingerprint=row.code_issue_fingerprint,
    )


def _session_to_orm(session: InvestigationSession) -> InvestigationSessionORM:
    return InvestigationSessionORM(
        id=session.id,
        investigation_id=session.investigation_id,
        session_number=session.session_number,
        tenant_id=session.tenant_id,
        occurred_at=session.occurred_at,
        log_refs=list(session.log_refs),
        trace_refs=list(session.trace_refs),
        status=session.status,
    )


def _session_from_orm(row: InvestigationSessionORM) -> InvestigationSession:
    return InvestigationSession(
        id=row.id,
        investigation_id=row.investigation_id,
        session_number=row.session_number,
        tenant_id=row.tenant_id,
        occurred_at=row.occurred_at,
        log_refs=list(row.log_refs or []),
        trace_refs=list(row.trace_refs or []),
        status=row.status,
    )


class SqlAlchemyArtifactRepository:
    """Envelope persistence. Reads are tenant-scoped; the DB's visibility
    carve-out additionally surfaces SHARED rows (policy-enforced, not
    application-filtered). Callers MUST still verify session membership
    before using a SHARED artifact for another tenant."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    async def save(self, tenant_id: str, artifact: KnowledgeArtifact) -> None:
        async with rls_session(self._session_factory, tenant_id) as session:
            session.add(_artifact_to_orm(artifact))

    async def get_by_id(self, tenant_id: str, artifact_id: UUID) -> KnowledgeArtifact | None:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(KnowledgeArtifactORM).where(KnowledgeArtifactORM.id == artifact_id)
            )
            row = result.first()
            return _artifact_from_orm(row) if row else None

    async def list_for_investigation(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[KnowledgeArtifact]:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(KnowledgeArtifactORM).where(
                    KnowledgeArtifactORM.investigation_id == investigation_id
                )
            )
            return [_artifact_from_orm(row) for row in result.all()]

    async def list_active_for_reuse(
        self, tenant_id: str, application_id: str, kinds: list[str] | None = None
    ) -> list[KnowledgeArtifact]:
        async with rls_session(self._session_factory, tenant_id) as session:
            stmt = select(KnowledgeArtifactORM).where(
                KnowledgeArtifactORM.tenant_id == tenant_id,
                KnowledgeArtifactORM.application_id == application_id,
                KnowledgeArtifactORM.status == ArtifactStatus.ACTIVE.value,
            )
            if kinds:
                stmt = stmt.where(KnowledgeArtifactORM.kind.in_(kinds))
            result = await session.scalars(stmt)
            return [_artifact_from_orm(row) for row in result.all()]

    async def list_shared_for_fingerprint(
        self, tenant_id: str, code_issue_fingerprint: str
    ) -> list[KnowledgeArtifact]:
        # NOTE: the tenant_id parameter binds the RLS session. SHARED rows
        # from other tenants are visible through the DB visibility policy;
        # the caller MUST verify the requesting tenant holds a session on
        # this fingerprint before use.
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(KnowledgeArtifactORM).where(
                    KnowledgeArtifactORM.code_issue_fingerprint == code_issue_fingerprint,
                    KnowledgeArtifactORM.visibility == ArtifactVisibility.SHARED_CODE_ISSUE.value,
                    KnowledgeArtifactORM.status == ArtifactStatus.ACTIVE.value,
                )
            )
            return [_artifact_from_orm(row) for row in result.all()]


class SqlAlchemySessionRepository:
    """Strictly tenant-scoped session detail persistence (RLS, no carve-out)."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    async def append(self, tenant_id: str, session: InvestigationSession) -> None:
        if session.tenant_id != tenant_id:
            from investigation_agent_platform.domain.common.exceptions import ConcurrencyError

            raise ConcurrencyError("Session tenant mismatch")
        async with rls_session(self._session_factory, tenant_id) as session_ctx:
            session_ctx.add(_session_to_orm(session))

    async def list_own_sessions(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[InvestigationSession]:
        async with rls_session(self._session_factory, tenant_id) as session_ctx:
            result = await session_ctx.scalars(
                select(InvestigationSessionORM)
                .where(InvestigationSessionORM.investigation_id == investigation_id)
                .order_by(InvestigationSessionORM.session_number)
            )
            return [_session_from_orm(row) for row in result.all()]

    async def count_own_sessions(self, tenant_id: str, investigation_id: UUID) -> int:
        async with rls_session(self._session_factory, tenant_id) as session_ctx:
            result = await session_ctx.scalars(
                select(func.count(InvestigationSessionORM.id)).where(
                    InvestigationSessionORM.investigation_id == investigation_id
                )
            )
            return int(result.first() or 0)


class SqlAlchemyCodeIssueIndex:
    """Tenant-free coordination index. NO tenant parameters by design —
    rows identify no tenant (fingerprint + opaque UUID + number + time).
    Service-layer only: intake merge lookup and redacted-placeholder
    assembly. Never exposed directly to callers."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    async def record_session(
        self,
        code_issue_fingerprint: str,
        session_number: int,
        investigation_id: UUID,
        occurred_at: datetime,
    ) -> None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        # Deliberately NOT rls_session: this table has no RLS and no tenant
        # column. Unique constraint makes concurrent same-number writes safe.
        async with self._session_factory() as session:
            await session.execute(
                pg_insert(CodeIssueIndexORM)
                .values(
                    id=uuid4(),
                    code_issue_fingerprint=code_issue_fingerprint,
                    session_number=session_number,
                    investigation_id=investigation_id,
                    occurred_at=occurred_at,
                )
                .on_conflict_do_nothing(
                    constraint="uq_code_issue_session",
                )
            )
            await session.commit()

    async def sessions_for_fingerprint(
        self, code_issue_fingerprint: str
    ) -> list[tuple[int, UUID, datetime]]:
        from sqlalchemy import select as sa_select

        async with self._session_factory() as session:
            result = await session.scalars(
                sa_select(CodeIssueIndexORM)
                .where(CodeIssueIndexORM.code_issue_fingerprint == code_issue_fingerprint)
                .order_by(CodeIssueIndexORM.session_number)
            )
            return [(r.session_number, r.investigation_id, r.occurred_at) for r in result.all()]

    async def latest_investigation(
        self, code_issue_fingerprint: str, open_only: bool = False
    ) -> UUID | None:
        _ = open_only  # Status filtering is the caller's job (it can read statuses).
        rows = await self.sessions_for_fingerprint(code_issue_fingerprint)
        return rows[-1][1] if rows else None

    async def next_session_number(self, code_issue_fingerprint: str) -> int:
        """Next session number, race-safe via transaction-scoped advisory lock."""
        from sqlalchemy import func as sa_func
        from sqlalchemy import select as sa_select
        from sqlalchemy import text as sa_text

        async with self._session_factory() as session:
            await session.execute(
                sa_text("SELECT pg_advisory_xact_lock(hashtext(:fp))"),
                {"fp": code_issue_fingerprint},
            )
            result = await session.scalars(
                sa_select(sa_func.coalesce(sa_func.max(CodeIssueIndexORM.session_number), 0)).where(
                    CodeIssueIndexORM.code_issue_fingerprint == code_issue_fingerprint
                )
            )
            number = int(result.first() or 0) + 1
            await session.commit()
            return number
