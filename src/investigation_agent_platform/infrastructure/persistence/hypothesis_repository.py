# src/investigation_agent_platform/infrastructure/persistence/hypothesis_repository.py
"""SQLAlchemy hypothesis repository."""

from typing import Any
from uuid import UUID

from sqlalchemy import select

from investigation_agent_platform.domain.hypothesis.models import (
    Hypothesis,
    HypothesisEvidenceAssessment,
    HypothesisStatus,
)
from investigation_agent_platform.infrastructure.persistence.models import HypothesisORM
from investigation_agent_platform.infrastructure.persistence.rls import rls_session


class SqlAlchemyHypothesisRepository:
    """PostgreSQL-backed HypothesisRepository over the hypotheses table with strict tenant scoping."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    @staticmethod
    def _to_orm(
        hypothesis: Hypothesis, tenant_id: str, investigation_id: UUID | None
    ) -> HypothesisORM:
        return HypothesisORM(
            id=hypothesis.id,
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            title=hypothesis.title,
            description=hypothesis.description,
            statement=hypothesis.statement,
            status=hypothesis.status.value,
            confidence_score=hypothesis.confidence_score,
            support_score=hypothesis.support_score,
            parent_hypothesis_id=hypothesis.parent_hypothesis_id,
            supporting_evidence_ids=[
                str(evidence_id) for evidence_id in hypothesis.supporting_evidence_ids
            ],
            refuting_evidence_ids=[
                str(evidence_id) for evidence_id in hypothesis.refuting_evidence_ids
            ],
            required_verification=hypothesis.required_verification,
            assessments_json=[
                assessment.model_dump(mode="json") for assessment in hypothesis.assessments
            ],
            created_at=hypothesis.created_at,
            updated_at=hypothesis.updated_at,
        )

    @staticmethod
    def _from_orm(row: HypothesisORM) -> Hypothesis:
        return Hypothesis(
            id=row.id,
            tenant_id=row.tenant_id,
            investigation_id=row.investigation_id or row.id,
            statement=row.statement,
            status=HypothesisStatus(row.status),
            confidence_score=row.confidence_score,
            support_score=row.support_score,
            assessments=[
                HypothesisEvidenceAssessment.model_validate(a) for a in row.assessments_json
            ],
            parent_hypothesis_id=row.parent_hypothesis_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
            title=row.title,
            description=row.description,
            supporting_evidence_ids=[
                __import__("uuid").UUID(s) for s in (row.supporting_evidence_ids or []) if s
            ],
            refuting_evidence_ids=[
                __import__("uuid").UUID(s) for s in (row.refuting_evidence_ids or []) if s
            ],
            required_verification=row.required_verification or [],
        )

    async def save(
        self, tenant_id: str, hypothesis: Hypothesis, investigation_id: UUID | None = None
    ) -> None:
        orm = self._to_orm(hypothesis, tenant_id, investigation_id)
        async with rls_session(self._session_factory, tenant_id) as session:
            if orm.id is not None:
                existing = await session.scalars(
                    select(HypothesisORM).where(
                        HypothesisORM.id == hypothesis.id,
                        HypothesisORM.investigation_id == investigation_id,
                    )
                )
                row = existing.first()
                if row is not None:
                    row.title = orm.title
                    row.description = orm.description
                    row.statement = orm.statement
                    row.status = orm.status
                    row.confidence_score = orm.confidence_score
                    row.support_score = orm.support_score
                    row.supporting_evidence_ids = orm.supporting_evidence_ids
                    row.refuting_evidence_ids = orm.refuting_evidence_ids
                    row.assessments_json = orm.assessments_json
                    row.updated_at = orm.updated_at
                    return
            session.add(orm)

    async def get_by_id(self, tenant_id: str, hypothesis_id: UUID) -> Hypothesis | None:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(HypothesisORM).where(
                    HypothesisORM.id == hypothesis_id,
                )
            )
            row = result.first()
            return self._from_orm(row) if row else None

    async def find_by_investigation_id(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[Hypothesis]:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(HypothesisORM).where(
                    HypothesisORM.investigation_id == investigation_id,
                )
            )
            return [self._from_orm(row) for row in result.all()]

    async def find_by_investigation_and_tenant(
        self, investigation_id: UUID, tenant_id: str, offset: int = 0, limit: int = 50
    ) -> tuple[list[Hypothesis], int]:
        """Paginated tenant-scoped hypotheses (mirrors the in-memory repo
        semantics the hypotheses router relies on)."""
        from sqlalchemy import func

        async with rls_session(self._session_factory, tenant_id) as session:
            total = await session.scalar(
                select(func.count())
                .select_from(HypothesisORM)
                .where(
                    HypothesisORM.tenant_id == tenant_id,
                    HypothesisORM.investigation_id == investigation_id,
                )
            )
            result = await session.scalars(
                select(HypothesisORM)
                .where(
                    HypothesisORM.tenant_id == tenant_id,
                    HypothesisORM.investigation_id == investigation_id,
                )
                .order_by(HypothesisORM.created_at)
                .offset(offset)
                .limit(limit)
            )
            return [self._from_orm(row) for row in result.all()], int(total or 0)
