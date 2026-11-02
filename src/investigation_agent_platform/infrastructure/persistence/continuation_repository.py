"""SQLAlchemy persistence for S7 continuations and conclusion history."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult

from investigation_agent_platform.api.errors import sanitize_extra
from investigation_agent_platform.domain.common.exceptions import ConcurrencyError
from investigation_agent_platform.domain.continuation.models import S7Continuation
from investigation_agent_platform.domain.finding.models import InvestigationConclusion
from investigation_agent_platform.infrastructure.persistence.models import (
    ConclusionHistoryORM,
    ContinuationReasoningLeaseORM,
    InvestigationConclusionORM,
    S7ContinuationORM,
)
from investigation_agent_platform.infrastructure.persistence.rls import rls_session
from investigation_agent_platform.infrastructure.telemetry.story import TRACE_LEVEL

logger = logging.getLogger(__name__)


class SqlAlchemyContinuationRepository:
    """Tenant-scoped continuation, lease, and conclusion-history repository."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    @staticmethod
    def _continuation_values(continuation: S7Continuation) -> dict[str, Any]:
        # Python-mode dump: datetimes/UUIDs stay native for the typed
        # columns. (mode="json" stringifies them and asyncpg rejects the
        # strings for TIMESTAMPTZ/UUID targets — caught by live-DB round trip.)
        values = continuation.model_dump()
        values.pop("claim_lease_expires_at", None)
        values.pop("last_run_id", None)
        return values

    @staticmethod
    def _continuation_from_orm(row: S7ContinuationORM) -> S7Continuation:
        return S7Continuation.model_validate(
            {
                "continuation_id": row.continuation_id,
                "version": row.version,
                "tenant_id": row.tenant_id,
                "investigation_id": row.investigation_id,
                "handoff_version": row.handoff_version,
                "source_handoff_state": row.source_handoff_state,
                "action": row.action,
                "parameters": row.parameters,
                "target": row.target,
                "price": row.price,
                "idempotency_key": row.idempotency_key,
                "status": row.status,
                "attempt_count": row.attempt_count,
                "created_at": row.created_at,
                "expires_at": row.expires_at,
                "claimed_at": row.claimed_at,
                "completed_at": row.completed_at,
                "last_error_code": row.last_error_code,
            }
        )

    @staticmethod
    def _conclusion_values(conclusion: InvestigationConclusion) -> dict[str, Any]:
        return {
            "tenant_id": conclusion.tenant_id,
            "investigation_id": conclusion.investigation_id,
            "status": conclusion.status.value,
            "root_cause": conclusion.root_cause,
            "confidence": conclusion.confidence,
            "supporting_evidence_ids": [str(value) for value in conclusion.supporting_evidence_ids],
            "supporting_hypothesis_ids": [str(value) for value in conclusion.supporting_hypothesis_ids],
            "contradicting_evidence_ids": [str(value) for value in conclusion.contradicting_evidence_ids],
            "limitations": list(conclusion.limitations),
            "recommended_next_steps": list(conclusion.recommended_next_steps),
            "verifier_version": getattr(conclusion, "verifier_version", None) or "v2",
            "best_reason": getattr(conclusion, "best_reason", "") or None,
            "against_evidence": list(getattr(conclusion, "against_evidence", []) or []),
            "provisional": bool(getattr(conclusion, "provisional", False)),
            "ranked_alternatives": list(getattr(conclusion, "ranked_alternatives", []) or []),
            "next_check": getattr(conclusion, "next_check", None),
            "proven_callers": list(getattr(conclusion, "proven_callers", []) or []),
            "unresolved_callers": list(getattr(conclusion, "unresolved_callers", []) or []),
            "caller_coverage": getattr(conclusion, "caller_coverage", None),
            "eliminated_rivals": [
                value.model_dump(mode="json")
                for value in (getattr(conclusion, "eliminated_rivals", None) or [])
            ],
            "handoff": conclusion.handoff.model_dump(mode="json") if conclusion.handoff else None,
        }

    async def insert(self, tenant_id: str, continuation: S7Continuation) -> S7Continuation:
        if continuation.tenant_id != tenant_id:
            raise ConcurrencyError("Continuation tenant mismatch")
        if logger.isEnabledFor(TRACE_LEVEL):
            logger.log(
                TRACE_LEVEL,
                "continuation insert started",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "investigation_id": str(continuation.investigation_id),
                        "continuation_id": str(continuation.continuation_id),
                        "event": "continuation.insert",
                        "outcome": "started",
                        "action": continuation.action,
                        "idempotency_key": continuation.idempotency_key,
                    }
                ),
            )
        async with rls_session(self._session_factory, tenant_id) as session:
            values = self._continuation_values(continuation)
            stmt = insert(S7ContinuationORM).values(**values).on_conflict_do_nothing(
                constraint="uq_s7_continuation_tenant_investigation_key"
            )
            await session.execute(stmt)
            row = await session.scalar(
                select(S7ContinuationORM).where(
                    S7ContinuationORM.tenant_id == tenant_id,
                    S7ContinuationORM.continuation_id == continuation.continuation_id,
                )
            )
            if row is None:
                row = await session.scalar(
                    select(S7ContinuationORM).where(
                        S7ContinuationORM.tenant_id == tenant_id,
                        S7ContinuationORM.investigation_id == continuation.investigation_id,
                        S7ContinuationORM.idempotency_key == continuation.idempotency_key,
                    )
                )
            if row is None:
                raise ConcurrencyError("Continuation insert was not observable")
            if logger.isEnabledFor(TRACE_LEVEL):
                logger.log(
                    TRACE_LEVEL,
                    "continuation insert completed",
                    extra=sanitize_extra(
                        {
                            "tenant_id": tenant_id,
                            "investigation_id": str(continuation.investigation_id),
                            "continuation_id": str(row.continuation_id),
                            "event": "continuation.insert",
                            "outcome": "completed",
                            "status": row.status,
                            "attempt_count": row.attempt_count,
                        }
                    ),
                )
            return self._continuation_from_orm(row)

    async def supersede_pending(
        self, tenant_id: str, investigation_id: UUID, idempotency_key: str
    ) -> int:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.execute(
                update(S7ContinuationORM)
                .where(
                    S7ContinuationORM.tenant_id == tenant_id,
                    S7ContinuationORM.investigation_id == investigation_id,
                    S7ContinuationORM.status == "PENDING",
                    S7ContinuationORM.idempotency_key != idempotency_key,
                )
                .values(status="SUPERSEDED", last_error_code="CONTINUATION_SUPERSEDED")
            )
            superseded = int(getattr(result, "rowcount", 0) or 0)
            if logger.isEnabledFor(TRACE_LEVEL):
                logger.log(
                    TRACE_LEVEL,
                    "continuation supersede pending completed",
                    extra=sanitize_extra(
                        {
                            "tenant_id": tenant_id,
                            "investigation_id": str(investigation_id),
                            "event": "continuation.supersede_pending",
                            "outcome": "completed",
                            "superseded_count": superseded,
                        }
                    ),
                )
            return superseded

    async def get(self, tenant_id: str, continuation_id: UUID) -> S7Continuation | None:
        async with rls_session(self._session_factory, tenant_id) as session:
            row = await session.scalar(
                select(S7ContinuationORM).where(
                    S7ContinuationORM.tenant_id == tenant_id,
                    S7ContinuationORM.continuation_id == continuation_id,
                )
            )
            if logger.isEnabledFor(TRACE_LEVEL):
                logger.log(
                    TRACE_LEVEL,
                    "continuation get completed",
                    extra=sanitize_extra(
                        {
                            "tenant_id": tenant_id,
                            "continuation_id": str(continuation_id),
                            "event": "continuation.get",
                            "outcome": "completed",
                            "found": row is not None,
                            "status": row.status if row is not None else None,
                            "action": row.action if row is not None else None,
                            "attempt_count": row.attempt_count if row is not None else None,
                            "investigation_id": (
                                str(row.investigation_id) if row is not None else None
                            ),
                        }
                    ),
                )
            return self._continuation_from_orm(row) if row is not None else None

    async def claim(
        self,
        tenant_id: str,
        continuation_id: UUID,
        investigation_id: UUID,
        handoff_version: str,
        idempotency_key: str,
        now: datetime,
        lease_seconds: int,
        max_attempts: int = 20,
    ) -> bool:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(S7ContinuationORM)
                    .where(
                        S7ContinuationORM.continuation_id == continuation_id,
                        S7ContinuationORM.tenant_id == tenant_id,
                        S7ContinuationORM.investigation_id == investigation_id,
                        S7ContinuationORM.handoff_version == handoff_version,
                        S7ContinuationORM.idempotency_key == idempotency_key,
                        S7ContinuationORM.expires_at > now,
                        S7ContinuationORM.attempt_count < max_attempts,
                        (
                            S7ContinuationORM.status.in_(
                                ["PENDING", "FAILED", "DENIED"]
                            )
                            | (
                                (S7ContinuationORM.status == "CLAIMED")
                                & (S7ContinuationORM.claim_lease_expires_at < now)
                            )
                        ),
                    )
                    .values(
                        status="CLAIMED",
                        claimed_at=now,
                        claim_lease_expires_at=now + timedelta(seconds=lease_seconds),
                        attempt_count=S7ContinuationORM.attempt_count + 1,
                    ),
                ),
            )
            granted = result.rowcount == 1
            if logger.isEnabledFor(TRACE_LEVEL):
                logger.log(
                    TRACE_LEVEL,
                    "continuation claim completed",
                    extra=sanitize_extra(
                        {
                            "tenant_id": tenant_id,
                            "investigation_id": str(investigation_id),
                            "continuation_id": str(continuation_id),
                            "event": "continuation.claim",
                            "outcome": "completed",
                            "granted": granted,
                            "max_attempts": max_attempts,
                        }
                    ),
                )
            return granted

    async def set_status(
        self,
        tenant_id: str,
        continuation_id: UUID,
        status: str,
        *,
        completed_at: datetime | None = None,
        error_code: str | None = None,
        run_id: str | None = None,
    ) -> bool:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(S7ContinuationORM)
                    .where(
                        S7ContinuationORM.tenant_id == tenant_id,
                        S7ContinuationORM.continuation_id == continuation_id,
                    )
                    .values(
                        status=status,
                        completed_at=completed_at,
                        last_error_code=error_code,
                        last_run_id=run_id,
                    ),
                ),
            )
            applied = result.rowcount == 1
            if logger.isEnabledFor(TRACE_LEVEL):
                logger.log(
                    TRACE_LEVEL,
                    "continuation set status completed",
                    extra=sanitize_extra(
                        {
                            "tenant_id": tenant_id,
                            "continuation_id": str(continuation_id),
                            "event": "continuation.set_status",
                            "outcome": "completed",
                            "status": status,
                            "applied": applied,
                            "error_code": error_code,
                        }
                    ),
                )
            return applied

    async def acquire_reasoning_lease(
        self,
        tenant_id: str,
        investigation_id: UUID,
        continuation_id: UUID,
        attempt: int,
        now: datetime,
        lease_seconds: int,
        run_id: str | None = None,
    ) -> bool:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(ContinuationReasoningLeaseORM)
                    .where(
                        ContinuationReasoningLeaseORM.tenant_id == tenant_id,
                        ContinuationReasoningLeaseORM.investigation_id == investigation_id,
                        ContinuationReasoningLeaseORM.lease_expires_at < now,
                    )
                    .values(
                        continuation_id=continuation_id,
                        attempt=attempt,
                        claimed_at=now,
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                        run_id=run_id,
                    ),
                ),
            )
            if result.rowcount == 1:
                if logger.isEnabledFor(TRACE_LEVEL):
                    logger.log(
                        TRACE_LEVEL,
                        "reasoning lease acquire completed",
                        extra=sanitize_extra(
                            {
                                "tenant_id": tenant_id,
                                "investigation_id": str(investigation_id),
                                "continuation_id": str(continuation_id),
                                "event": "continuation.acquire_reasoning_lease",
                                "outcome": "completed",
                                "path": "renew",
                                "attempt": attempt,
                            }
                        ),
                    )
                return True
            result = cast(
                CursorResult[Any],
                await session.execute(
                    insert(ContinuationReasoningLeaseORM)
                    .values(
                        tenant_id=tenant_id,
                        investigation_id=investigation_id,
                        continuation_id=continuation_id,
                        attempt=attempt,
                        claimed_at=now,
                        lease_expires_at=now + timedelta(seconds=lease_seconds),
                        run_id=run_id,
                    )
                    .on_conflict_do_nothing(index_elements=["investigation_id"]),
                    ),
            )
            acquired = result.rowcount == 1
            if logger.isEnabledFor(TRACE_LEVEL):
                logger.log(
                    TRACE_LEVEL,
                    "reasoning lease acquire completed",
                    extra=sanitize_extra(
                        {
                            "tenant_id": tenant_id,
                            "investigation_id": str(investigation_id),
                            "continuation_id": str(continuation_id),
                            "event": "continuation.acquire_reasoning_lease",
                            "outcome": "completed",
                            "path": "insert",
                            "acquired": acquired,
                            "attempt": attempt,
                        }
                    ),
                )
            return acquired

    async def release_reasoning_lease(self, tenant_id: str, investigation_id: UUID) -> bool:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    delete(ContinuationReasoningLeaseORM).where(
                        ContinuationReasoningLeaseORM.tenant_id == tenant_id,
                        ContinuationReasoningLeaseORM.investigation_id == investigation_id,
                    )
                ),
            )
            released = result.rowcount == 1
            if logger.isEnabledFor(TRACE_LEVEL):
                logger.log(
                    TRACE_LEVEL,
                    "reasoning lease release completed",
                    extra=sanitize_extra(
                        {
                            "tenant_id": tenant_id,
                            "investigation_id": str(investigation_id),
                            "event": "continuation.release_reasoning_lease",
                            "outcome": "completed",
                            "released": released,
                        }
                    ),
                )
            return released

    async def replace_conclusion_with_history(
        self,
        tenant_id: str,
        conclusion: InvestigationConclusion,
        continuation_id: UUID,
        archived_at: datetime | None = None,
    ) -> UUID:
        if conclusion.tenant_id != tenant_id:
            raise ConcurrencyError("Conclusion tenant mismatch")
        async with rls_session(self._session_factory, tenant_id) as session:
            row = await session.scalar(
                select(InvestigationConclusionORM)
                .where(
                    InvestigationConclusionORM.tenant_id == tenant_id,
                    InvestigationConclusionORM.investigation_id == conclusion.investigation_id,
                )
                .with_for_update()
            )
            if row is None:
                raise ConcurrencyError("Conclusion row required for replacement")
            snapshot = {
                "tenant_id": row.tenant_id,
                "investigation_id": str(row.investigation_id),
                "status": row.status,
                "root_cause": row.root_cause,
                "confidence": row.confidence,
                "supporting_evidence_ids": list(row.supporting_evidence_ids or []),
                "supporting_hypothesis_ids": list(row.supporting_hypothesis_ids or []),
                "contradicting_evidence_ids": list(row.contradicting_evidence_ids or []),
                "limitations": list(row.limitations or []),
                "recommended_next_steps": list(row.recommended_next_steps or []),
                "generated_at": row.generated_at.isoformat(),
                "best_reason": row.best_reason or "",
                "against_evidence": list(row.against_evidence or []),
                "provisional": bool(row.provisional),
                "ranked_alternatives": list(row.ranked_alternatives or []),
                "next_check": row.next_check,
                "proven_callers": list(row.proven_callers or []),
                "unresolved_callers": list(row.unresolved_callers or []),
                "caller_coverage": row.caller_coverage,
                "eliminated_rivals": row.eliminated_rivals,
                "handoff": row.handoff,
                "verifier_version": row.verifier_version,
            }
            history = ConclusionHistoryORM(
                tenant_id=tenant_id,
                investigation_id=conclusion.investigation_id,
                archived_at=archived_at or datetime.now(UTC),
                superseded_by_continuation_id=continuation_id,
                status=row.status,
                handoff_version=(row.handoff or {}).get("version") if row.handoff else None,
                snapshot=snapshot,
            )
            session.add(history)
            await session.flush()
            values = self._conclusion_values(conclusion)
            result = cast(
                CursorResult[Any],
                await session.execute(
                    update(InvestigationConclusionORM)
                    .where(
                        InvestigationConclusionORM.tenant_id == tenant_id,
                        InvestigationConclusionORM.investigation_id == conclusion.investigation_id,
                    )
                    .values(**values),
                ),
            )
            if result.rowcount != 1:
                raise ConcurrencyError("Conclusion row lost during replacement")
            if logger.isEnabledFor(TRACE_LEVEL):
                logger.log(
                    TRACE_LEVEL,
                    "conclusion replace with history completed",
                    extra=sanitize_extra(
                        {
                            "tenant_id": tenant_id,
                            "investigation_id": str(conclusion.investigation_id),
                            "continuation_id": str(continuation_id),
                            "event": "continuation.replace_conclusion_with_history",
                            "outcome": "completed",
                            "history_id": str(history.id),
                            "status": row.status,
                        }
                    ),
                )
            return history.id

    async def list_conclusion_history(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[dict[str, Any]]:
        async with rls_session(self._session_factory, tenant_id) as session:
            rows = await session.scalars(
                select(ConclusionHistoryORM)
                .where(
                    ConclusionHistoryORM.tenant_id == tenant_id,
                    ConclusionHistoryORM.investigation_id == investigation_id,
                )
                .order_by(ConclusionHistoryORM.archived_at)
            )
            rows = rows.all()
            if logger.isEnabledFor(TRACE_LEVEL):
                for row in rows:
                    logger.log(
                        TRACE_LEVEL,
                        "conclusion history row",
                        extra=sanitize_extra(
                            {
                                "tenant_id": tenant_id,
                                "investigation_id": str(investigation_id),
                                "event": "continuation.list_conclusion_history",
                                "outcome": "row",
                                "history_id": str(row.id),
                                "status": row.status,
                                "archived_at": (
                                    row.archived_at.isoformat()
                                    if row.archived_at is not None
                                    else None
                                ),
                                "superseded_by_continuation_id": (
                                    str(row.superseded_by_continuation_id)
                                    if row.superseded_by_continuation_id is not None
                                    else None
                                ),
                                "handoff_version": row.handoff_version,
                            }
                        ),
                    )
                logger.log(
                    TRACE_LEVEL,
                    "conclusion history list completed",
                    extra=sanitize_extra(
                        {
                            "tenant_id": tenant_id,
                            "investigation_id": str(investigation_id),
                            "event": "continuation.list_conclusion_history",
                            "outcome": "completed",
                            "row_count": len(rows),
                        }
                    ),
                )
            return [
                {
                    "id": row.id,
                    "archived_at": row.archived_at,
                    "superseded_by_continuation_id": row.superseded_by_continuation_id,
                    "status": row.status,
                    "handoff_version": row.handoff_version,
                    "snapshot": row.snapshot,
                }
                for row in rows
            ]
