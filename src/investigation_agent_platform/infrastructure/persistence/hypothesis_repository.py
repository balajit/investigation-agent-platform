# src/investigation_agent_platform/infrastructure/persistence/hypothesis_repository.py
"""SQLAlchemy hypothesis repository."""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from investigation_agent_platform.api.errors import sanitize_extra
from investigation_agent_platform.domain.hypothesis.models import (
    Hypothesis,
    HypothesisCausalStep,
    HypothesisEvidenceAssessment,
    HypothesisStatus,
)
from investigation_agent_platform.infrastructure.persistence.models import HypothesisORM
from investigation_agent_platform.infrastructure.persistence.rls import rls_session

logger = logging.getLogger(__name__)


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
            causal_steps_json=[
                step.model_dump(mode="json") for step in hypothesis.causal_steps
            ],
            created_at=hypothesis.created_at,
            updated_at=hypothesis.updated_at,
        )

    @staticmethod
    def _from_orm(row: HypothesisORM) -> Hypothesis:
        # P0-3: causal steps round-trip (absent on pre-migration rows).
        try:
            causal_steps = [
                HypothesisCausalStep.model_validate(s)
                for s in (getattr(row, "causal_steps_json", None) or [])
            ]
        except Exception:
            causal_steps = []
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
                UUID(s) for s in (row.supporting_evidence_ids or []) if s
            ],
            refuting_evidence_ids=[
                UUID(s) for s in (row.refuting_evidence_ids or []) if s
            ],
            required_verification=row.required_verification or [],
            causal_steps=causal_steps,
        )

    async def save(
        self, tenant_id: str, hypothesis: Hypothesis, investigation_id: UUID | None = None
    ) -> None:
        orm = self._to_orm(hypothesis, tenant_id, investigation_id)
        try:
            async with rls_session(self._session_factory, tenant_id) as session:
                if orm.id is not None:
                    existing = await session.scalars(
                        select(HypothesisORM).where(
                            HypothesisORM.tenant_id == tenant_id,
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
                        row.causal_steps_json = orm.causal_steps_json
                        row.updated_at = orm.updated_at
                        outcome = "updated"
                    else:
                        session.add(orm)
                        outcome = "inserted"
                else:
                    session.add(orm)
                    outcome = "inserted"
        except Exception as exc:
            logger.exception(
                "hypothesis save failed",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "investigation_id": str(investigation_id),
                        "event": "hypothesis.save",
                        "outcome": "failed",
                        "hypothesis_id": str(hypothesis.id),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                ),
            )
            raise
        logger.info(
            "hypothesis saved",
            extra=sanitize_extra(
                {
                    "tenant_id": tenant_id,
                    "investigation_id": str(investigation_id),
                    "event": "hypothesis.save",
                    "outcome": outcome,
                    "hypothesis_id": str(hypothesis.id),
                }
            ),
        )

    async def get_by_id(self, tenant_id: str, hypothesis_id: UUID) -> Hypothesis | None:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(HypothesisORM).where(
                    HypothesisORM.tenant_id == tenant_id,
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
                select(HypothesisORM)
                .where(
                    HypothesisORM.tenant_id == tenant_id,
                    HypothesisORM.investigation_id == investigation_id,
                )
                # P0-3: deterministic order — callers must not depend on
                # unspecified row order when selecting a candidate.
                .order_by(HypothesisORM.created_at, HypothesisORM.id)
            )
            return [self._from_orm(row) for row in result.all()]

    async def find_by_investigation_and_tenant(
        self, investigation_id: UUID, tenant_id: str, offset: int = 0, limit: int = 50
    ) -> tuple[list[Hypothesis], int]:
        """Paginated tenant-scoped hypotheses (mirrors the in-memory repo
        semantics the hypotheses router relies on)."""
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
