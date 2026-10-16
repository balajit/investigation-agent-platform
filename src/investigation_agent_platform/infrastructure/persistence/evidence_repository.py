# src/investigation_agent_platform/infrastructure/persistence/evidence_repository.py
"""SQLAlchemy evidence repository."""

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from investigation_agent_platform.api.errors import sanitize_extra
from investigation_agent_platform.domain.evidence.models import Evidence
from investigation_agent_platform.infrastructure.persistence.models import EvidenceORM
from investigation_agent_platform.infrastructure.persistence.rls import rls_session

logger = logging.getLogger(__name__)


class SqlAlchemyEvidenceRepository:
    """PostgreSQL-backed EvidenceRepository over the evidence table with strict tenant scoping."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    @staticmethod
    def _to_orm(evidence: Evidence, investigation_id: UUID | None = None) -> EvidenceORM:
        return EvidenceORM(
            investigation_id=investigation_id,
            tenant_id=evidence.tenant_id,
            type=evidence.evidence_type.value,
            source_type=evidence.provider,
            source_id=evidence.source,
            observed_at=evidence.observed_at,
            retrieved_at=evidence.retrieved_at,
            summary=evidence.summary,
            content_reference=evidence.content_uri or None,
            classification=evidence.classification.value,
            is_sanitized=evidence.is_redacted,
            evidence_key=str(evidence.evidence_id),
            fingerprint=evidence.fingerprint,
            payload_json=evidence.model_dump(mode="json"),
        )

    @staticmethod
    def _from_orm(row: EvidenceORM) -> Evidence:
        return Evidence.model_validate(row.payload_json)

    @staticmethod
    def _values(
        evidence: Evidence, tenant_id: str, investigation_id: UUID | None
    ) -> dict[str, Any]:
        return {
            "investigation_id": investigation_id,
            "tenant_id": tenant_id,
            "type": evidence.evidence_type.value,
            "source_type": evidence.provider,
            "source_id": evidence.source,
            "observed_at": evidence.observed_at,
            "retrieved_at": evidence.retrieved_at,
            "summary": evidence.summary,
            "content_reference": evidence.content_uri or None,
            "classification": evidence.classification.value,
            "is_sanitized": evidence.is_redacted,
            "evidence_key": str(evidence.evidence_id),
            "fingerprint": evidence.fingerprint,
            "payload_json": evidence.model_dump(mode="json"),
        }

    async def save(
        self, tenant_id: str, evidence: Evidence, investigation_id: UUID | None = None
    ) -> None:
        try:
            async with rls_session(self._session_factory, tenant_id) as session:
                # Atomic upsert: a bare ON CONFLICT DO NOTHING covers every
                # unique constraint (tenant+key AND tenant+fingerprint) in one
                # statement. The pre-check-then-insert this replaces raced when
                # parallel fetches recorded the same deterministic evidence_id
                # (uuid5 over index+doc), killing runs with
                # uq_evidence_tenant_key violations (WORKFLOW_ERROR, 2026-10-16).
                stmt = insert(EvidenceORM).values(
                    **self._values(evidence, tenant_id, investigation_id)
                )
                stmt = stmt.on_conflict_do_nothing()
                result = await session.execute(stmt)
                outcome = "inserted" if result.rowcount else "deduplicated"
        except Exception as exc:
            logger.exception(
                "evidence save failed",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "investigation_id": str(investigation_id),
                        "event": "evidence.save",
                        "outcome": "failed",
                        "evidence_id": str(evidence.evidence_id),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                ),
            )
            raise
        logger.info(
            "evidence saved",
            extra=sanitize_extra(
                {
                    "tenant_id": tenant_id,
                    "investigation_id": str(investigation_id),
                    "event": "evidence.save",
                    "outcome": outcome,
                    "evidence_id": str(evidence.evidence_id),
                    "fingerprint": evidence.fingerprint,
                }
            ),
        )

    async def save_batch(
        self, tenant_id: str, evidence_list: list[Evidence], investigation_id: UUID | None = None
    ) -> None:
        # TO_DO-015: constant-query-count batch — one multi-row INSERT ...
        # ON CONFLICT DO NOTHING, regardless of batch size. The bare conflict
        # target covers tenant+key as well as tenant+fingerprint, so parallel
        # duplicate fetches converge instead of violating uq_evidence_tenant_key.
        unique: dict[str, Evidence] = {}
        for evidence in evidence_list:
            unique.setdefault(evidence.fingerprint, evidence)
        batch_size = len(evidence_list)
        if not unique:
            logger.info(
                "evidence batch saved",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "investigation_id": str(investigation_id),
                        "event": "evidence.save_batch",
                        "outcome": "inserted",
                        "batch_size": batch_size,
                        "unique_count": 0,
                        "inserted_count": 0,
                    }
                ),
            )
            return
        try:
            async with rls_session(self._session_factory, tenant_id) as session:
                stmt = insert(EvidenceORM).values(
                    [self._values(e, tenant_id, investigation_id) for e in unique.values()]
                )
                stmt = stmt.on_conflict_do_nothing()
                result = await session.execute(stmt)
                inserted_count = max(result.rowcount, 0)
        except Exception as exc:
            logger.exception(
                "evidence batch save failed",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "investigation_id": str(investigation_id),
                        "event": "evidence.save_batch",
                        "outcome": "failed",
                        "batch_size": batch_size,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                ),
            )
            raise
        logger.info(
            "evidence batch saved",
            extra=sanitize_extra(
                {
                    "tenant_id": tenant_id,
                    "investigation_id": str(investigation_id),
                    "event": "evidence.save_batch",
                    "outcome": "inserted",
                    "batch_size": batch_size,
                    "unique_count": len(unique),
                    "inserted_count": inserted_count,
                }
            ),
        )

    async def get_by_id(self, tenant_id: str, evidence_id: UUID) -> Evidence | None:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(EvidenceORM).where(
                    EvidenceORM.tenant_id == tenant_id,
                    EvidenceORM.evidence_key == str(evidence_id),
                )
            )
            row = result.first()
            return self._from_orm(row) if row else None

    async def get_by_ids(self, tenant_id: str, evidence_ids: list[UUID]) -> list[Evidence]:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(EvidenceORM).where(
                    EvidenceORM.tenant_id == tenant_id,
                    EvidenceORM.evidence_key.in_([str(e) for e in evidence_ids]),
                )
            )
            return [self._from_orm(row) for row in result.all()]

    async def find_by_investigation_id(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[Evidence]:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(EvidenceORM).where(
                    EvidenceORM.tenant_id == tenant_id,
                    EvidenceORM.investigation_id == investigation_id,
                )
            )
            return [self._from_orm(row) for row in result.all()]

    async def purge_expired(self, tenant_id: str, cutoff: datetime, limit: int = 500) -> int:
        """Hard-delete orphan evidence rows (NULL investigation) at/before cutoff.

        Rows attached to an investigation are never purged standalone — they
        cascade with their investigation's retention purge.
        """
        try:
            async with rls_session(self._session_factory, tenant_id) as session:
                ids = (
                    await session.scalars(
                        select(EvidenceORM.id)
                        .where(
                            EvidenceORM.tenant_id == tenant_id,
                            EvidenceORM.investigation_id.is_(None),
                            EvidenceORM.created_at <= cutoff,
                        )
                        .order_by(EvidenceORM.created_at)
                        .limit(max(1, min(limit, 5000)))
                    )
                ).all()
                if not ids:
                    purged = 0
                else:
                    result = await session.execute(
                        delete(EvidenceORM).where(
                            EvidenceORM.tenant_id == tenant_id,
                            EvidenceORM.id.in_(ids),
                        )
                    )
                    purged = int(result.rowcount or 0)
        except Exception as exc:
            logger.exception(
                "evidence purge failed",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "event": "evidence.purge_expired",
                        "outcome": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                ),
            )
            raise
        logger.info(
            "evidence purge completed",
            extra=sanitize_extra(
                {
                    "tenant_id": tenant_id,
                    "event": "evidence.purge_expired",
                    "outcome": "purged",
                    "purged_count": purged,
                }
            ),
        )
        return purged
