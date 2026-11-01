# src/investigation_agent_platform/infrastructure/persistence/idempotency_repository.py
"""Durable, multi-replica-safe idempotency store (F-013/F-014).

Atomicity is provided by the database's unique constraint on
``(tenant_id, idempotency_key)`` rather than a process-local ``asyncio.Lock``
— safe across any number of API replicas. A reservation is created via
``INSERT ... ON CONFLICT DO NOTHING``; if the insert is won, the caller
executes the operation and calls ``complete`` to store the response. If the
insert loses (row already exists), the request payload hash is compared: a
mismatch raises ``IdempotencyConflictError``; a match with no response yet
raises ``IdempotencyInProgressError``; a match with a stored response returns
it (replay).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from investigation_agent_platform.api.errors import sanitize_extra
from investigation_agent_platform.domain.common.exceptions import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
)
from investigation_agent_platform.infrastructure.persistence.models import IdempotencyKeyORM
from investigation_agent_platform.infrastructure.persistence.rls import rls_session
from investigation_agent_platform.infrastructure.telemetry.story import TRACE_LEVEL

logger = logging.getLogger(__name__)

_PENDING_STATUS = -1


class SqlAlchemyIdempotencyStore:
    def __init__(self, db_session_factory: Any, ttl_seconds: float = 86400) -> None:
        self._session_factory = db_session_factory
        self._ttl_seconds = ttl_seconds

    def _reserve_stmt(self, tenant_id: str, key: str, request_hash: str, now: datetime):  # type: ignore[no-untyped-def]
        return (
            pg_insert(IdempotencyKeyORM)
            .values(
                id=uuid4(),
                tenant_id=tenant_id,
                idempotency_key=key,
                request_hash=request_hash,
                response_status=_PENDING_STATUS,
                response_json={},
                created_at=now,
                expires_at=now + timedelta(seconds=self._ttl_seconds),
            )
            .on_conflict_do_nothing(constraint="uq_idempotency_tenant_key")
            .returning(IdempotencyKeyORM.id)
        )

    async def _reclaim_and_reserve(self, session: Any, row: Any, tenant_id: str, key: str, request_hash: str, now: datetime) -> tuple[dict[str, Any] | None, bool]:
        # R12C: one transaction — delete, flush, re-insert. The unique
        # constraint arbitrates concurrent reclaimers; the loser sees an
        # empty insert and reports in-progress below.
        await session.delete(row)
        await session.flush()
        inserted = await session.execute(self._reserve_stmt(tenant_id, key, request_hash, now))
        if inserted.first() is not None:
            return None, True
        raise IdempotencyInProgressError(
            f"A request with idempotency key '{key}' is already in progress"
        )

    # P0-1b: a PENDING row older than this cannot be a live owner — the
    # longest activity timeout in the system is 120s, so 180s clears any
    # genuine owner with margin. Owners die without completing on worker
    # restarts and activity timeouts; reclaiming risks double execution,
    # but downstream dedup already tolerates that by design (see
    # _complete_reservation's contract).
    _ORPHAN_AFTER_SECONDS = 180.0

    async def reserve_or_get(
        self,
        tenant_id: str,
        key: str,
        request_hash: str,
        operation: str = "default",
        application_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        from investigation_agent_platform.domain.common.idempotency import (
            scoped_idempotency_key,
        )

        key = scoped_idempotency_key(key, operation, application_id)
        now = datetime.now(UTC)
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.execute(
                self._reserve_stmt(tenant_id, key, request_hash, now)
            )
            if result.first() is not None:
                if logger.isEnabledFor(TRACE_LEVEL):
                    logger.log(
                        TRACE_LEVEL,
                        "idempotency reserve completed",
                        extra=sanitize_extra(
                            {
                                "tenant_id": tenant_id,
                                "event": "idempotency.reserve_or_get",
                                "outcome": "reserved",
                                "operation": operation,
                            }
                        ),
                    )
                return None, True

            existing = await session.scalars(
                select(IdempotencyKeyORM).where(
                    IdempotencyKeyORM.tenant_id == tenant_id,
                    IdempotencyKeyORM.idempotency_key == key,
                )
            )
            row = existing.first()
            if row is None:
                # Extremely unlikely race between the failed insert and this
                # select; treat as a fresh reservation attempt failure.
                raise IdempotencyInProgressError(
                    f"Idempotency key '{key}' reservation could not be resolved"
                )
            if row.expires_at is not None and row.expires_at.replace(tzinfo=UTC) < now:
                # Expired: reclaim in the same transaction (R12C). An
                # expired key is reusable regardless of the old request
                # hash — no conflict error.
                return await self._reclaim_and_reserve(
                    session, row, tenant_id, key, request_hash, now
                )
            if row.request_hash != request_hash:
                raise IdempotencyConflictError(
                    f"Idempotency key '{key}' already used with a different request payload"
                )
            if row.response_status == _PENDING_STATUS:
                try:
                    created = row.created_at
                    if created is not None:
                        if created.tzinfo is None:
                            created = created.replace(tzinfo=UTC)
                        age = (now - created).total_seconds()
                    else:
                        age = 0.0
                except Exception:
                    age = 0.0
                if age > self._ORPHAN_AFTER_SECONDS:
                    # Orphaned owner (restart/timeout without complete):
                    # reclaim exactly like an expired row (R12C).
                    return await self._reclaim_and_reserve(
                        session, row, tenant_id, key, request_hash, now
                    )
                raise IdempotencyInProgressError(
                    f"A request with idempotency key '{key}' is already in progress"
                )
            if logger.isEnabledFor(TRACE_LEVEL):
                logger.log(
                    TRACE_LEVEL,
                    "idempotency existing key completed",
                    extra=sanitize_extra(
                        {
                            "tenant_id": tenant_id,
                            "event": "idempotency.reserve_or_get",
                            "outcome": "completed_existing",
                            "operation": operation,
                            "response_status": row.response_status,
                        }
                    ),
                )
            return row.response_json, False

    async def complete(
        self,
        tenant_id: str,
        key: str,
        response: dict[str, Any],
        operation: str = "default",
        application_id: str | None = None,
    ) -> None:
        from investigation_agent_platform.domain.common.idempotency import (
            scoped_idempotency_key,
        )

        key = scoped_idempotency_key(key, operation, application_id)
        status_code = int(response.get("_status_code", 200)) if isinstance(response, dict) else 200
        async with rls_session(self._session_factory, tenant_id) as session:
            existing = await session.scalars(
                select(IdempotencyKeyORM).where(
                    IdempotencyKeyORM.tenant_id == tenant_id,
                    IdempotencyKeyORM.idempotency_key == key,
                )
            )
            row = existing.first()
            if row is not None:
                row.response_status = status_code
                row.response_json = response
            if logger.isEnabledFor(TRACE_LEVEL):
                logger.log(
                    TRACE_LEVEL,
                    "idempotency complete",
                    extra=sanitize_extra(
                        {
                            "tenant_id": tenant_id,
                            "event": "idempotency.complete",
                            "outcome": "completed",
                            "operation": operation,
                            "status_code": status_code,
                            "found": row is not None,
                        }
                    ),
                )

    async def abandon(
        self,
        tenant_id: str,
        key: str,
        operation: str = "default",
        application_id: str | None = None,
    ) -> None:
        """Release a pending reservation so a retry can reserve fresh.

        Called when the reserved operation raised before ``complete``: the
        owner is dead, so the lease must not block retries until TTL expiry.
        Deletes only pending rows (a completed response is never removed —
        that path is a replay, not a retry). Never raises.
        """
        from investigation_agent_platform.domain.common.idempotency import (
            scoped_idempotency_key,
        )

        try:
            key = scoped_idempotency_key(key, operation, application_id)
            async with rls_session(self._session_factory, tenant_id) as session:
                existing = await session.scalars(
                    select(IdempotencyKeyORM).where(
                        IdempotencyKeyORM.tenant_id == tenant_id,
                        IdempotencyKeyORM.idempotency_key == key,
                    )
                )
                row = existing.first()
                if row is not None and row.response_status == _PENDING_STATUS:
                    await session.delete(row)
        except Exception as exc:
            logger.warning("Idempotency abandon failed", extra={"error": str(exc)})
