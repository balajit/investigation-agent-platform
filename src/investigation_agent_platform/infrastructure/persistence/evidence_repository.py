# src/investigation_agent_platform/infrastructure/persistence/evidence_repository.py
"""SQLAlchemy evidence repository."""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

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
            evidence_key=evidence.evidence_id,
            payload_json=evidence.model_dump(mode="json"),
        )

    @staticmethod
    def _from_orm(row: EvidenceORM) -> Evidence:
        return Evidence.model_validate(row.payload_json)

    async def save(self, tenant_id: str, evidence: Evidence, investigation_id: UUID | None = None) -> None:
        async with rls_session(self._session_factory, tenant_id) as session:
            stmt = insert(EvidenceORM).values(
                investigation_id=investigation_id,
                tenant_id=tenant_id,
                type=evidence.evidence_type.value,
                source_type=evidence.provider,
                source_id=evidence.source,
                observed_at=evidence.observed_at,
                retrieved_at=evidence.retrieved_at,
                summary=evidence.summary,
                content_reference=evidence.content_uri or None,
                classification=evidence.classification.value,
                is_sanitized=evidence.is_redacted,
                evidence_key=evidence.evidence_id,
                payload_json=evidence.model_dump(mode="json"),
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_evidence_tenant_key",
                set_={
                    "summary": stmt.excluded.summary,
                    "content_reference": stmt.excluded.content_reference,
                    "is_sanitized": stmt.excluded.is_sanitized,
                    "payload_json": stmt.excluded.payload_json,
                    "retrieved_at": stmt.excluded.retrieved_at,
                },
            )
            await session.execute(stmt)

    async def save_batch(
        self, tenant_id: str, evidence_list: list[Evidence], investigation_id: UUID | None = None
    ) -> None:
        async with rls_session(self._session_factory, tenant_id) as session:
            for evidence in evidence_list:
                stmt = insert(EvidenceORM).values(
                    investigation_id=investigation_id,
                    tenant_id=tenant_id,
                    type=evidence.evidence_type.value,
                    source_type=evidence.provider,
                    source_id=evidence.source,
                    observed_at=evidence.observed_at,
                    retrieved_at=evidence.retrieved_at,
                    summary=evidence.summary,
                    content_reference=evidence.content_uri or None,
                    classification=evidence.classification.value,
                    is_sanitized=evidence.is_redacted,
                    evidence_key=evidence.evidence_id,
                    payload_json=evidence.model_dump(mode="json"),
                )
                stmt = stmt.on_conflict_do_update(
                    constraint="uq_evidence_tenant_key",
                    set_={
                        "summary": stmt.excluded.summary,
                        "content_reference": stmt.excluded.content_reference,
                        "is_sanitized": stmt.excluded.is_sanitized,
                        "payload_json": stmt.excluded.payload_json,
                        "retrieved_at": stmt.excluded.retrieved_at,
                    },
                )
                await session.execute(stmt)

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

    async def find_by_investigation_id(self, tenant_id: str, investigation_id: UUID) -> list[Evidence]:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(EvidenceORM).where(
                    EvidenceORM.tenant_id == tenant_id,
                    EvidenceORM.investigation_id == investigation_id,
                )
            )
            return [self._from_orm(row) for row in result.all()]
