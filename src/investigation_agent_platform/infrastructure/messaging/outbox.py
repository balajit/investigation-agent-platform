# src/investigation_agent_platform/infrastructure/messaging/outbox.py
"""Transactional outbox repository and dispatcher (F-058).

Domain events are persisted to ``outbox_events`` — durable, tenant-scoped,
uniquely keyed by ``(tenant_id, idempotency_key)`` — before any attempt to
publish them to the broker. A separate dispatcher process/call publishes
undispatched rows and marks them sent; consumers must be idempotent since
at-least-once delivery is the guarantee, not exactly-once.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from investigation_agent_platform.infrastructure.persistence.models import OutboxEventORM
from investigation_agent_platform.infrastructure.persistence.rls import rls_session

logger = logging.getLogger(__name__)


class SqlAlchemyOutboxRepository:
    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    async def enqueue(
        self,
        tenant_id: str,
        investigation_id: UUID | None,
        event_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> None:
        """Insert the event; a duplicate ``idempotency_key`` for this tenant is
        a no-op (the event has already been queued for delivery)."""
        async with rls_session(self._session_factory, tenant_id) as session:
            stmt = (
                pg_insert(OutboxEventORM)
                .values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    event_type=event_type,
                    idempotency_key=idempotency_key,
                    payload_json=payload,
                    created_at=datetime.now(UTC),
                )
                .on_conflict_do_nothing(constraint="uq_outbox_tenant_idempotency_key")
            )
            await session.execute(stmt)

    async def dispatch_pending(
        self, tenant_id: str, publisher: Any, batch_size: int = 25, max_attempts: int = 5
    ) -> int:
        """Publish undispatched events for one tenant. Returns count dispatched.

        Each event is marked ``dispatched_at`` only after ``publisher.publish``
        returns successfully; a publish failure increments ``dispatch_attempts``
        and records ``last_error`` for later retry rather than losing the event.
        """
        dispatched = 0
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(OutboxEventORM)
                .where(
                    OutboxEventORM.tenant_id == tenant_id,
                    OutboxEventORM.dispatched_at.is_(None),
                    OutboxEventORM.dispatch_attempts < max_attempts,
                )
                .order_by(OutboxEventORM.created_at)
                .limit(batch_size)
            )
            rows = result.all()
            for row in rows:
                try:
                    from investigation_agent_platform.ports.messaging.publisher import EventEnvelope

                    envelope = EventEnvelope(
                        event_type=row.event_type,
                        tenant_id=row.tenant_id,
                        investigation_id=row.investigation_id,
                        idempotency_key=row.idempotency_key,
                        payload=row.payload_json,
                    )
                    await publisher.publish(envelope)
                    row.dispatched_at = datetime.now(UTC)
                    dispatched += 1
                except Exception as exc:
                    row.dispatch_attempts += 1
                    row.last_error = str(exc)[:2000]
                    logger.warning(
                        "Outbox dispatch failed",
                        extra={
                            "event_id": str(row.id),
                            "event_type": row.event_type,
                            "error": str(exc),
                        },
                    )
        return dispatched
