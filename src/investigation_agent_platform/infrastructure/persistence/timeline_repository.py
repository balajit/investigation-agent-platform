# src/investigation_agent_platform/infrastructure/persistence/timeline_repository.py
"""SQLAlchemy timeline event repository."""

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select

from investigation_agent_platform.api.errors import sanitize_extra
from investigation_agent_platform.domain.timeline.models import TimelineEvent, TimelineEventType
from investigation_agent_platform.infrastructure.persistence.models import TimelineEventORM
from investigation_agent_platform.infrastructure.persistence.rls import rls_session

logger = logging.getLogger(__name__)


class SqlAlchemyTimelineRepository:
    """PostgreSQL-backed TimelineRepository over the timeline_events table."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    @staticmethod
    def _to_orm(event: TimelineEvent, investigation_id: UUID | None) -> TimelineEventORM:
        return TimelineEventORM(
            tenant_id=event.tenant_id,
            investigation_id=investigation_id,
            event_type=event.event_type.value,
            description=event.description,
            entity_ids=[str(entity_id) for entity_id in event.entity_ids],
            evidence_ids=[str(evidence_id) for evidence_id in event.evidence_ids],
            attributes=event.attributes,
            timestamp=event.timestamp,
        )

    @staticmethod
    def _from_orm(row: TimelineEventORM) -> TimelineEvent:
        def _parse_uuids(vals: list[str] | None) -> list[UUID]:
            out: list[UUID] = []
            for v in vals or []:
                try:
                    out.append(UUID(v))
                except ValueError:
                    # Explicit fallback: malformed stored ids are skipped so
                    # one bad row cannot break timeline hydration.
                    continue
            return out

        return TimelineEvent(
            id=row.id,
            tenant_id=row.tenant_id,
            investigation_id=row.investigation_id or row.id,
            timestamp=row.timestamp,
            event_type=TimelineEventType(row.event_type),
            description=row.description,
            attributes=row.attributes,
            entity_ids=_parse_uuids(row.entity_ids),
            evidence_ids=_parse_uuids(row.evidence_ids),
        )

    async def append(
        self, tenant_id: str, event: TimelineEvent, investigation_id: UUID | None = None
    ) -> None:
        try:
            async with rls_session(self._session_factory, tenant_id) as session:
                session.add(self._to_orm(event, investigation_id))
        except Exception as exc:
            logger.exception(
                "timeline event append failed",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "investigation_id": str(investigation_id),
                        "event": "timeline.append",
                        "outcome": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                ),
            )
            raise
        logger.info(
            "timeline event appended",
            extra=sanitize_extra(
                {
                    "tenant_id": tenant_id,
                    "investigation_id": str(investigation_id),
                    "event": "timeline.append",
                    "outcome": "appended",
                    "event_id": str(event.id),
                    "event_type": event.event_type.value,
                }
            ),
        )

    async def append_batch(
        self, tenant_id: str, events: list[TimelineEvent], investigation_id: UUID | None = None
    ) -> None:
        try:
            async with rls_session(self._session_factory, tenant_id) as session:
                for event in events:
                    session.add(self._to_orm(event, investigation_id))
        except Exception as exc:
            logger.exception(
                "timeline batch append failed",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "investigation_id": str(investigation_id),
                        "event": "timeline.append_batch",
                        "outcome": "failed",
                        "batch_size": len(events),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                ),
            )
            raise
        logger.info(
            "timeline batch appended",
            extra=sanitize_extra(
                {
                    "tenant_id": tenant_id,
                    "investigation_id": str(investigation_id),
                    "event": "timeline.append_batch",
                    "outcome": "appended",
                    "batch_size": len(events),
                }
            ),
        )

    async def find_by_investigation_id(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[TimelineEvent]:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(TimelineEventORM)
                .where(
                    TimelineEventORM.tenant_id == tenant_id,
                    TimelineEventORM.investigation_id == investigation_id,
                )
                .order_by(TimelineEventORM.timestamp)
            )
            return [self._from_orm(row) for row in result.all()]

    async def find_by_time_range(
        self, tenant_id: str, investigation_id: UUID, start: datetime, end: datetime
    ) -> list[TimelineEvent]:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(TimelineEventORM)
                .where(
                    TimelineEventORM.tenant_id == tenant_id,
                    TimelineEventORM.investigation_id == investigation_id,
                    TimelineEventORM.timestamp >= start,
                    TimelineEventORM.timestamp <= end,
                )
                .order_by(TimelineEventORM.timestamp)
            )
            return [self._from_orm(row) for row in result.all()]

    async def purge_expired(self, tenant_id: str, cutoff: datetime, limit: int = 500) -> int:
        """Hard-delete orphan timeline rows (NULL investigation) at/before cutoff.

        Rows attached to an investigation are never purged standalone — they
        cascade with their investigation's retention purge.
        """
        try:
            async with rls_session(self._session_factory, tenant_id) as session:
                ids = (
                    await session.scalars(
                        select(TimelineEventORM.id)
                        .where(
                            TimelineEventORM.tenant_id == tenant_id,
                            TimelineEventORM.investigation_id.is_(None),
                            TimelineEventORM.timestamp <= cutoff,
                        )
                        .order_by(TimelineEventORM.timestamp)
                        .limit(max(1, min(limit, 5000)))
                    )
                ).all()
                if not ids:
                    purged = 0
                else:
                    result = await session.execute(
                        delete(TimelineEventORM).where(
                            TimelineEventORM.tenant_id == tenant_id,
                            TimelineEventORM.id.in_(ids),
                        )
                    )
                    purged = int(result.rowcount or 0)
        except Exception as exc:
            logger.exception(
                "timeline purge failed",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "event": "timeline.purge_expired",
                        "outcome": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                ),
            )
            raise
        logger.info(
            "timeline purge completed",
            extra=sanitize_extra(
                {
                    "tenant_id": tenant_id,
                    "event": "timeline.purge_expired",
                    "outcome": "purged",
                    "purged_count": purged,
                }
            ),
        )
        return purged
