# src/investigation_agent_platform/infrastructure/persistence/log_exemplar_repository.py
"""SQLAlchemy log-exemplar repository (log-group persistence)."""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select

from investigation_agent_platform.api.errors import sanitize_extra
from investigation_agent_platform.domain.evidence.models import LogExemplar
from investigation_agent_platform.infrastructure.persistence.models import LogExemplarORM
from investigation_agent_platform.infrastructure.persistence.rls import rls_session

logger = logging.getLogger(__name__)


class SqlAlchemyLogExemplarRepository:
    """PostgreSQL-backed exemplar store with strict tenant scoping."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    @staticmethod
    def _to_orm(e: LogExemplar) -> LogExemplarORM:
        return LogExemplarORM(
            id=e.id,
            tenant_id=e.tenant_id,
            investigation_id=e.investigation_id,
            signature=e.signature,
            member_evidence_ids=[str(m) for m in e.member_evidence_ids],
            count=e.count,
            span_first=e.span_first,
            span_last=e.span_last,
            representative_snippet=e.representative_snippet,
        )

    @staticmethod
    def _from_orm(row: LogExemplarORM) -> LogExemplar:
        return LogExemplar(
            id=row.id,
            tenant_id=row.tenant_id,
            investigation_id=row.investigation_id,
            signature=row.signature,
            member_evidence_ids=[UUID(m) for m in (row.member_evidence_ids or [])],
            count=row.count,
            span_first=row.span_first,
            span_last=row.span_last,
            representative_snippet=row.representative_snippet,
            created_at=row.created_at,
        )

    async def save_batch(
        self, tenant_id: str, exemplars: list[LogExemplar]
    ) -> int:
        """Insert exemplars idempotently (by id); returns inserted count."""
        if not exemplars:
            return 0
        inserted = 0
        try:
            async with rls_session(self._session_factory, tenant_id) as session:
                for exemplar in exemplars:
                    if exemplar.tenant_id != tenant_id:
                        continue
                    existing = await session.scalars(
                        select(LogExemplarORM.id).where(
                            LogExemplarORM.tenant_id == tenant_id,
                            LogExemplarORM.id == exemplar.id,
                        )
                    )
                    if existing.first() is not None:
                        continue
                    session.add(self._to_orm(exemplar))
                    inserted += 1
                await session.commit()
        except Exception as exc:
            logger.exception(
                "log exemplar save failed",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "event": "log_exemplar.save_batch",
                        "outcome": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                ),
            )
            raise
        return inserted

    async def list_by_investigation(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[LogExemplar]:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(LogExemplarORM)
                .where(
                    LogExemplarORM.tenant_id == tenant_id,
                    LogExemplarORM.investigation_id == investigation_id,
                )
                .order_by(LogExemplarORM.created_at, LogExemplarORM.id)
            )
            return [self._from_orm(row) for row in result.all()]
