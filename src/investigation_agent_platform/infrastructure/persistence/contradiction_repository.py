# src/investigation_agent_platform/infrastructure/persistence/contradiction_repository.py
"""SQLAlchemy contradiction repository (P0-3)."""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select

from investigation_agent_platform.api.errors import sanitize_extra
from investigation_agent_platform.application.investigation.verification import (
    Contradiction,
    ContradictionSeverity,
)
from investigation_agent_platform.infrastructure.persistence.models import ContradictionORM
from investigation_agent_platform.infrastructure.persistence.rls import rls_session

logger = logging.getLogger(__name__)


class SqlAlchemyContradictionRepository:
    """PostgreSQL-backed contradiction store with strict tenant scoping."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    @staticmethod
    def _to_orm(c: Contradiction, tenant_id: str, investigation_id: UUID) -> ContradictionORM:
        return ContradictionORM(
            id=c.contradiction_id,
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            statement=c.statement,
            severity=c.severity.value,
            evidence_ids=[str(e) for e in c.evidence_ids],
            hypothesis_ids=[str(h) for h in c.hypothesis_ids],
            resolution_status=c.resolution_status,
            resolved_by=c.resolved_by,
            resolution_rationale=c.resolution_rationale,
            resolved_at=c.resolved_at,
            superseded_by=c.superseded_by,
        )

    @staticmethod
    def _from_orm(row: ContradictionORM) -> Contradiction:
        return Contradiction(
            contradiction_id=row.id,
            statement=row.statement,
            evidence_ids=row.evidence_ids,
            hypothesis_ids=row.hypothesis_ids,
            severity=ContradictionSeverity(row.severity),
            created_at=row.created_at,
            resolution_status=row.resolution_status,
            resolved_by=row.resolved_by,
            resolution_rationale=row.resolution_rationale,
            resolved_at=row.resolved_at,
            superseded_by=row.superseded_by,
        )

    async def save(
        self, tenant_id: str, contradiction: Contradiction, investigation_id: UUID
    ) -> None:
        try:
            async with rls_session(self._session_factory, tenant_id) as session:
                existing = await session.scalars(
                    select(ContradictionORM).where(
                        ContradictionORM.tenant_id == tenant_id,
                        ContradictionORM.id == contradiction.contradiction_id,
                    )
                )
                row = existing.first()
                if row is not None:
                    orm = self._to_orm(contradiction, tenant_id, investigation_id)
                    row.statement = orm.statement
                    row.severity = orm.severity
                    row.evidence_ids = orm.evidence_ids
                    row.hypothesis_ids = orm.hypothesis_ids
                    row.resolution_status = orm.resolution_status
                    row.resolved_by = orm.resolved_by
                    row.resolution_rationale = orm.resolution_rationale
                    row.resolved_at = orm.resolved_at
                    row.superseded_by = orm.superseded_by
                    outcome = "updated"
                else:
                    session.add(self._to_orm(contradiction, tenant_id, investigation_id))
                    outcome = "inserted"
        except Exception as exc:
            logger.exception(
                "contradiction save failed",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "investigation_id": str(investigation_id),
                        "event": "contradiction.save",
                        "outcome": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                ),
            )
            raise
        logger.info(
            "contradiction saved",
            extra=sanitize_extra(
                {
                    "tenant_id": tenant_id,
                    "investigation_id": str(investigation_id),
                    "event": "contradiction.save",
                    "outcome": outcome,
                }
            ),
        )

    async def find_by_investigation_id(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[Contradiction]:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(ContradictionORM)
                .where(
                    ContradictionORM.tenant_id == tenant_id,
                    ContradictionORM.investigation_id == investigation_id,
                )
                .order_by(ContradictionORM.created_at, ContradictionORM.id)
            )
            return [self._from_orm(row) for row in result.all()]
