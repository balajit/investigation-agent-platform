# src/investigation_agent_platform/infrastructure/persistence/investigation_repository.py
"""SQLAlchemy investigation repository implementing optimistic concurrency control and child persistence."""

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, update

from investigation_agent_platform.api.errors import sanitize_extra
from investigation_agent_platform.domain.common.exceptions import ConcurrencyError
from investigation_agent_platform.domain.common.utils import SystemClock
from investigation_agent_platform.domain.investigation.models import (
    ActorType,
    Investigation,
    InvestigationContext,
    InvestigationRequest,
    InvestigationStatus,
)
from investigation_agent_platform.infrastructure.persistence.models import (
    InvestigationFactORM,
    InvestigationORM,
)
from investigation_agent_platform.infrastructure.persistence.rls import rls_session

logger = logging.getLogger(__name__)


class SqlAlchemyInvestigationRepository:
    """PostgreSQL-backed InvestigationRepository with version-guarded updates and child entity persistence."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    @staticmethod
    def _to_orm(investigation: Investigation) -> InvestigationORM:
        return InvestigationORM(
            id=investigation.id,
            tenant_id=investigation.tenant_id,
            application_id=investigation.application_id,
            environment=investigation.context.environment,
            title=(investigation.request.problem_description or "")[:256],
            problem_description=investigation.request.problem_description,
            status=investigation.status.value,
            phase=investigation.phase,
            created_at=investigation.created_at,
            updated_at=investigation.updated_at,
            started_at=investigation.started_at,
            completed_at=investigation.completed_at,
            created_by=investigation.request.requested_by,
            session_id=investigation.session_id,
            request_json=investigation.request.model_dump(mode="json"),
            context_json=investigation.context.model_dump(mode="json"),
            metadata_json=investigation.metadata or None,
            version=investigation.version,
            code_issue_fingerprint=investigation.code_issue_fingerprint,
        )

    @staticmethod
    def _from_orm(row: InvestigationORM) -> Investigation:
        request = (
            InvestigationRequest.model_validate(row.request_json)
            if row.request_json
            else InvestigationRequest(
                problem_description=row.problem_description,
                application_id=row.application_id,
                session_id=row.session_id,
                requested_by=row.created_by,
            )
        )
        context = (
            InvestigationContext.model_validate(row.context_json)
            if row.context_json
            else InvestigationContext(
                environment=row.environment,
                time_window=(row.created_at, row.created_at),
            )
        )
        # Gracefully handle unknown/legacy status values: no ARCHIVED state
        # exists on InvestigationStatus, so everything unrecognized (legacy
        # rows included) maps to CANCELLED rather than crashing the read.
        try:
            status = InvestigationStatus(row.status)
        except ValueError:
            logger.warning("Unknown investigation status '%s' mapped to CANCELLED", row.status)
            status = InvestigationStatus.CANCELLED
        return Investigation(
            id=row.id,
            session_id=row.session_id,
            application_id=row.application_id,
            tenant_id=row.tenant_id,
            request=request,
            context=context,
            status=status,
            phase=row.phase or status.value,
            created_at=row.created_at,
            updated_at=row.updated_at,
            started_at=row.started_at,
            completed_at=row.completed_at,
            version=row.version,
            code_issue_fingerprint=row.code_issue_fingerprint,
            metadata=row.metadata_json or {},
        )

    async def create(self, tenant_id: str, investigation: Investigation) -> None:
        try:
            async with rls_session(self._session_factory, tenant_id) as session:
                session.add(self._to_orm(investigation))
        except Exception as exc:
            logger.exception(
                "investigation create failed",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "investigation_id": str(investigation.id),
                        "event": "investigation.create",
                        "outcome": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                ),
            )
            raise
        logger.info(
            "investigation created",
            extra=sanitize_extra(
                {
                    "tenant_id": tenant_id,
                    "investigation_id": str(investigation.id),
                    "event": "investigation.create",
                    "outcome": "created",
                    "status": investigation.status.value,
                }
            ),
        )

    async def get_by_id(self, tenant_id: str, investigation_id: UUID) -> Investigation | None:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(InvestigationORM).where(
                    InvestigationORM.id == investigation_id,
                    InvestigationORM.tenant_id == tenant_id,
                )
            )
            row = result.first()
            return self._from_orm(row) if row else None

    async def save(
        self, tenant_id: str, investigation: Investigation, expected_version: int
    ) -> None:
        try:
            async with rls_session(self._session_factory, tenant_id) as session:
                result = await session.execute(
                    update(InvestigationORM)
                    .where(
                        InvestigationORM.id == investigation.id,
                        InvestigationORM.tenant_id == tenant_id,
                        InvestigationORM.version == expected_version,
                    )
                    .values(
                        status=investigation.status.value,
                        phase=investigation.phase,
                        updated_at=investigation.updated_at,
                        started_at=investigation.started_at,
                        completed_at=investigation.completed_at,
                        request_json=investigation.request.model_dump(mode="json"),
                        context_json=investigation.context.model_dump(mode="json"),
                        metadata_json=investigation.metadata or None,
                        version=expected_version + 1,
                    )
                )
                if result.rowcount == 0:  # type: ignore[attr-defined]
                    await session.rollback()
                    raise ConcurrencyError(
                        f"Investigation {investigation.id} was modified concurrently or tenant mismatch; "
                        f"expected version {expected_version}.",
                        details={
                            "investigation_id": str(investigation.id),
                            "expected_version": expected_version,
                            "tenant_id": tenant_id,
                        },
                    )

                # Facts are append-only (TO_DO-024): every fact on the
                # aggregate INSERTs a new child row; facts are never updated
                # or deleted here, so concurrent appends cannot lost-update.
                if hasattr(investigation, "facts") and investigation.facts:
                    for fact in investigation.facts:
                        session.add(
                            InvestigationFactORM(
                                investigation_id=investigation.id,
                                fact_type=getattr(fact, "fact_type", "DISCOVERED"),
                                statement=fact.statement,
                                confidence=getattr(fact, "confidence", 1.0),
                                source_evidence_ids=[
                                    str(e) for e in getattr(fact, "source_evidence_ids", [])
                                ],
                                attributes=getattr(fact, "attributes", {}),
                                observed_at=getattr(fact, "observed_at", investigation.updated_at),
                            )
                        )
                fact_count = len(investigation.facts) if hasattr(investigation, "facts") else 0
        except ConcurrencyError:
            logger.exception(
                "investigation save version conflict",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "investigation_id": str(investigation.id),
                        "event": "investigation.save",
                        "outcome": "version_conflict",
                        "expected_version": expected_version,
                    }
                ),
            )
            raise
        except Exception as exc:
            logger.exception(
                "investigation save failed",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "investigation_id": str(investigation.id),
                        "event": "investigation.save",
                        "outcome": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                ),
            )
            raise
        logger.info(
            "investigation saved",
            extra=sanitize_extra(
                {
                    "tenant_id": tenant_id,
                    "investigation_id": str(investigation.id),
                    "event": "investigation.save",
                    "outcome": "saved",
                    "status": investigation.status.value,
                    "expected_version": expected_version,
                    "fact_count": fact_count,
                }
            ),
        )

    async def delete(self, tenant_id: str, investigation_id: UUID) -> None:
        outcome = "deleted"
        try:
            async with rls_session(self._session_factory, tenant_id) as session:
                result = await session.scalars(
                    select(InvestigationORM).where(
                        InvestigationORM.id == investigation_id,
                        InvestigationORM.tenant_id == tenant_id,
                    )
                )
                row = result.first()
                if row is None:
                    outcome = "not_found"
                    logger.info(
                        "investigation deleted",
                        extra=sanitize_extra(
                            {
                                "tenant_id": tenant_id,
                                "investigation_id": str(investigation_id),
                                "event": "investigation.delete",
                                "outcome": outcome,
                            }
                        ),
                    )
                    return
                # Try domain transition to CANCELLED for audit / invariant enforcement.
                transitioned = False
                try:
                    inv = self._from_orm(row)
                    if inv.status not in {
                        InvestigationStatus.COMPLETED,
                        InvestigationStatus.FAILED,
                        InvestigationStatus.CANCELLED,
                    }:
                        updated, _ = inv.transition_to(
                            InvestigationStatus.CANCELLED,
                            ActorType.ADMIN,
                            "Deleted via repository",
                            clock=SystemClock(),
                        )
                        await session.execute(
                            update(InvestigationORM)
                            .where(
                                InvestigationORM.id == investigation_id,
                                InvestigationORM.tenant_id == tenant_id,
                                InvestigationORM.version == row.version,
                            )
                            .values(
                                status=updated.status.value,
                                phase=updated.phase,
                                updated_at=updated.updated_at,
                                started_at=updated.started_at,
                                completed_at=updated.completed_at,
                                version=row.version + 1,
                            )
                        )
                        transitioned = True
                except Exception as exc:
                    # Deliberate warning (not exception): the fallback below
                    # still soft-deletes the row; the stack trace is preserved
                    # via exc_info for triage.
                    logger.warning(
                        "delete transition_to CANCELLED failed, falling back to direct update",
                        exc_info=True,
                        extra=sanitize_extra(
                            {
                                "tenant_id": tenant_id,
                                "investigation_id": str(investigation_id),
                                "event": "investigation.delete",
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                            }
                        ),
                    )
                if not transitioned:
                    # Fallback: direct soft-delete to CANCELLED (terminal enum value).
                    await session.execute(
                        update(InvestigationORM)
                        .where(
                            InvestigationORM.id == investigation_id,
                            InvestigationORM.tenant_id == tenant_id,
                        )
                        .values(
                            status=InvestigationStatus.CANCELLED.value,
                            phase=InvestigationStatus.CANCELLED.value,
                        )
                    )
        except Exception as exc:
            logger.exception(
                "investigation delete failed",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "investigation_id": str(investigation_id),
                        "event": "investigation.delete",
                        "outcome": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                ),
            )
            raise
        logger.info(
            "investigation deleted",
            extra=sanitize_extra(
                {
                    "tenant_id": tenant_id,
                    "investigation_id": str(investigation_id),
                    "event": "investigation.delete",
                    "outcome": outcome,
                }
            ),
        )

    async def exists(self, tenant_id: str, investigation_id: UUID) -> bool:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(InvestigationORM.id).where(
                    InvestigationORM.id == investigation_id,
                    InvestigationORM.tenant_id == tenant_id,
                )
            )
            return result.first() is not None

    async def list_open_ids(self, tenant_id: str) -> list[UUID]:
        """IDs of investigations not yet in a terminal status (ISSUE-4)."""
        terminal = {
            InvestigationStatus.COMPLETED.value,
            InvestigationStatus.FAILED.value,
            InvestigationStatus.CANCELLED.value,
        }
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(InvestigationORM.id).where(
                    InvestigationORM.tenant_id == tenant_id,
                    InvestigationORM.status.notin_(terminal),
                )
            )
            return list(result.all())

    async def list_filtered(
        self,
        tenant_id: str,
        statuses: list[str] | None,
        application_id: str | None,
        limit: int,
    ) -> list[UUID]:
        async with rls_session(self._session_factory, tenant_id) as session:
            stmt = select(InvestigationORM.id).where(
                InvestigationORM.tenant_id == tenant_id
            )
            if statuses:
                stmt = stmt.where(InvestigationORM.status.in_(statuses))
            if application_id is not None:
                stmt = stmt.where(InvestigationORM.application_id == application_id)
            stmt = stmt.order_by(InvestigationORM.created_at.desc()).limit(max(0, limit))
            result = await session.scalars(stmt)
            return list(result.all())

    async def purge_expired(self, tenant_id: str, cutoff: datetime, limit: int = 500) -> int:
        """Hard-delete terminal investigations completed at/before cutoff (TO_DO-023).

        Only COMPLETED/FAILED/CANCELLED rows are eligible — open, paused, or
        awaiting-input investigations are pinned regardless of age. Child
        rows (evidence, timeline, transitions, facts, findings, conclusions)
        cascade via ``ondelete="CASCADE"`` FKs.
        """
        terminal = (
            InvestigationStatus.COMPLETED.value,
            InvestigationStatus.FAILED.value,
            InvestigationStatus.CANCELLED.value,
        )
        try:
            async with rls_session(self._session_factory, tenant_id) as session:
                ids = (
                    await session.scalars(
                        select(InvestigationORM.id)
                        .where(
                            InvestigationORM.tenant_id == tenant_id,
                            InvestigationORM.status.in_(terminal),
                            InvestigationORM.completed_at.is_not(None),
                            InvestigationORM.completed_at <= cutoff,
                        )
                        .order_by(InvestigationORM.completed_at)
                        .limit(max(1, min(limit, 5000)))
                    )
                ).all()
                if not ids:
                    purged = 0
                else:
                    result = await session.execute(
                        delete(InvestigationORM).where(
                            InvestigationORM.tenant_id == tenant_id,
                            InvestigationORM.id.in_(ids),
                        )
                    )
                    purged = int(result.rowcount or 0)
        except Exception as exc:
            logger.exception(
                "investigation purge failed",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "event": "investigation.purge_expired",
                        "outcome": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                ),
            )
            raise
        logger.info(
            "investigation purge completed",
            extra=sanitize_extra(
                {
                    "tenant_id": tenant_id,
                    "event": "investigation.purge_expired",
                    "outcome": "purged",
                    "purged_count": purged,
                }
            ),
        )
        return purged
