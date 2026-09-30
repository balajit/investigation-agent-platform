# src/investigation_agent_platform/infrastructure/persistence/quota_enforcer.py
"""Quota enforcement: Postgres-atomic and in-memory adapters (Part 11.3D).

Pre-dispatch checks must be atomic across API replicas, so the Postgres
adapter serializes increments with ``SELECT ... FOR UPDATE`` inside
``rls_session``. The in-memory adapter is single-process only (dev/test).
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from investigation_agent_platform.domain.common.extension import CapabilityScope
from investigation_agent_platform.domain.common.quotas import QuotaCheckResult, QuotaPolicy
from investigation_agent_platform.infrastructure.persistence.models import QuotaCounterORM
from investigation_agent_platform.infrastructure.persistence.rls import rls_session

_OPERATION_LIMITS: dict[str, str] = {
    "investigation.dispatch": "max_concurrent_investigations",
    "job.dispatch": "max_concurrent_jobs",
    "job.enqueue": "max_queued_jobs",
    "batch.record": "max_batch_records",
    "chat.session": "max_active_chat_sessions",
    "chat.stream": "max_active_streams",
}


def _limit_for(operation: str, policy: QuotaPolicy) -> tuple[str, int]:
    field = _OPERATION_LIMITS.get(operation, "max_concurrent_jobs")
    return field, int(getattr(policy, field))


class PostgresQuotaEnforcer:
    """Atomic quota checks backed by the quota_counters table."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    async def check(
        self, scope: CapabilityScope, operation: str, policy: QuotaPolicy
    ) -> QuotaCheckResult:
        field, limit = _limit_for(operation, policy)
        app_id = scope.application_id or ""
        now = datetime.now(UTC)
        window_start = now.replace(minute=0, second=0, microsecond=0)
        async with rls_session(self._session_factory, scope.tenant_id) as session:
            result = await session.scalars(
                select(QuotaCounterORM)
                .where(
                    QuotaCounterORM.tenant_id == scope.tenant_id,
                    QuotaCounterORM.application_id == app_id,
                    QuotaCounterORM.quota_class == "default",
                    QuotaCounterORM.operation == operation,
                    QuotaCounterORM.window_start == window_start,
                )
                .with_for_update()
            )
            row = result.first()
            current = row.count if row is not None else 0
            if current >= limit:
                return QuotaCheckResult(
                    allowed=False,
                    violated_limit=field,
                    current_usage=current,
                    limit=limit,
                    retry_after_seconds=60,
                )
            if row is None:
                session.add(
                    QuotaCounterORM(
                        tenant_id=scope.tenant_id,
                        application_id=app_id,
                        quota_class="default",
                        operation=operation,
                        window_start=window_start,
                        count=1,
                    )
                )
            else:
                row.count = current + 1
            return QuotaCheckResult(allowed=True, current_usage=current + 1, limit=limit)

    async def release(self, scope: CapabilityScope, operation: str) -> None:
        app_id = scope.application_id or ""
        now = datetime.now(UTC)
        window_start = now.replace(minute=0, second=0, microsecond=0)
        async with rls_session(self._session_factory, scope.tenant_id) as session:
            result = await session.scalars(
                select(QuotaCounterORM)
                .where(
                    QuotaCounterORM.tenant_id == scope.tenant_id,
                    QuotaCounterORM.application_id == app_id,
                    QuotaCounterORM.operation == operation,
                    QuotaCounterORM.window_start == window_start,
                )
                .with_for_update()
            )
            row = result.first()
            if row is not None and row.count > 0:
                row.count -= 1


class InMemoryQuotaEnforcer:
    """Single-process quota checks for dev/test (NOT replica-safe)."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._window = time.monotonic()

    async def check(
        self, scope: CapabilityScope, operation: str, policy: QuotaPolicy
    ) -> QuotaCheckResult:
        field, limit = _limit_for(operation, policy)
        key = f"{scope.tenant_id}:{scope.application_id or '-'}:{operation}"
        async with self._lock:
            current = self._counts.get(key, 0)
            if current >= limit:
                return QuotaCheckResult(
                    allowed=False,
                    violated_limit=field,
                    current_usage=current,
                    limit=limit,
                    retry_after_seconds=60,
                )
            self._counts[key] = current + 1
            return QuotaCheckResult(allowed=True, current_usage=current + 1, limit=limit)

    async def release(self, scope: CapabilityScope, operation: str) -> None:
        key = f"{scope.tenant_id}:{scope.application_id or '-'}:{operation}"
        async with self._lock:
            if self._counts.get(key, 0) > 0:
                self._counts[key] -= 1
