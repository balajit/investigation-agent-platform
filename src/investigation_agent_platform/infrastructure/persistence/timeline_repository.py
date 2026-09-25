# src/investigation_agent_platform/infrastructure/persistence/timeline_repository.py
"""SQLAlchemy timeline event repository."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from investigation_agent_platform.domain.timeline.models import TimelineEvent, TimelineEventType
from investigation_agent_platform.infrastructure.persistence.models import TimelineEventORM
from investigation_agent_platform.infrastructure.persistence.rls import rls_session


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
        from uuid import UUID as _UUID

        def _parse_uuids(vals: list[str] | None) -> list[_UUID]:
            out: list[_UUID] = []
            for v in vals or []:
                try:
                    out.append(_UUID(v))
                except Exception:
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
        async with rls_session(self._session_factory, tenant_id) as session:
            session.add(self._to_orm(event, investigation_id))

    async def append_batch(
        self, tenant_id: str, events: list[TimelineEvent], investigation_id: UUID | None = None
    ) -> None:
        async with rls_session(self._session_factory, tenant_id) as session:
            for event in events:
                session.add(self._to_orm(event, investigation_id))

    async def find_by_investigation_id(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[TimelineEvent]:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(TimelineEventORM)
                .where(TimelineEventORM.investigation_id == investigation_id)
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
                    TimelineEventORM.investigation_id == investigation_id,
                    TimelineEventORM.timestamp >= start,
                    TimelineEventORM.timestamp <= end,
                )
                .order_by(TimelineEventORM.timestamp)
            )
            return [self._from_orm(row) for row in result.all()]
