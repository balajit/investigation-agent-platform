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

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from investigation_agent_platform.domain.common.exceptions import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
)
from investigation_agent_platform.infrastructure.persistence.models import IdempotencyKeyORM
from investigation_agent_platform.infrastructure.persistence.rls import rls_session

_PENDING_STATUS = -1


class SqlAlchemyIdempotencyStore:
    def __init__(self, db_session_factory: Any, ttl_seconds: float = 86400) -> None:
        self._session_factory = db_session_factory
        self._ttl_seconds = ttl_seconds

    async def reserve_or_get(
        self, tenant_id: str, key: str, request_hash: str
    ) -> tuple[dict[str, Any] | None, bool]:
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=self._ttl_seconds)
        async with rls_session(self._session_factory, tenant_id) as session:
            stmt = (
                pg_insert(IdempotencyKeyORM)
                .values(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    idempotency_key=key,
                    request_hash=request_hash,
                    response_status=_PENDING_STATUS,
                    response_json={},
                    created_at=now,
                    expires_at=expires_at,
                )
                .on_conflict_do_nothing(constraint="uq_idempotency_tenant_key")
                .returning(IdempotencyKeyORM.id)
            )
            result = await session.execute(stmt)
            if result.first() is not None:
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
                # Expired: safe to reuse — delete and let caller retry the
                # reservation on the next call rather than racing an UPDATE here.
                await session.delete(row)
                raise IdempotencyInProgressError(
                    f"Idempotency key '{key}' expired; retry the request"
                )
            if row.request_hash != request_hash:
                raise IdempotencyConflictError(
                    f"Idempotency key '{key}' already used with a different request payload"
                )
            if row.response_status == _PENDING_STATUS:
                raise IdempotencyInProgressError(
                    f"A request with idempotency key '{key}' is already in progress"
                )
            return row.response_json, False

    async def complete(self, tenant_id: str, key: str, response: dict[str, Any]) -> None:
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
