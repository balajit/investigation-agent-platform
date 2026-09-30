# src/investigation_agent_platform/infrastructure/persistence/finding_repository.py
"""SQLAlchemy finding/conclusion repository (Part 11.2)."""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from investigation_agent_platform.domain.finding.models import (
    ConclusionStatus,
    Finding,
    FindingType,
    InvestigationConclusion,
    SeverityLevel,
)
from investigation_agent_platform.infrastructure.persistence.models import (
    FindingORM,
    InvestigationConclusionORM,
)
from investigation_agent_platform.infrastructure.persistence.rls import rls_session

logger = logging.getLogger(__name__)


class SqlAlchemyFindingConclusionRepository:
    """PostgreSQL-backed FindingConclusionRepository with strict tenant scoping."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    # -- mapping ---------------------------------------------------------

    @staticmethod
    def _finding_to_orm(finding: Finding, tenant_id: str) -> FindingORM:
        return FindingORM(
            id=finding.id,
            tenant_id=tenant_id,
            investigation_id=finding.investigation_id,
            finding_type=finding.finding_type.value,
            title=finding.title,
            statement=finding.statement,
            severity=finding.severity.value,
            confidence=finding.confidence,
            evidence_ids=[str(e) for e in finding.evidence_ids],
            related_hypothesis_ids=[str(h) for h in finding.related_hypothesis_ids],
            causal_chain=[str(c) for c in finding.causal_chain],
            remediation_steps=list(finding.remediation_steps),
        )

    @staticmethod
    def _finding_from_orm(row: FindingORM) -> Finding:
        return Finding(
            id=row.id,
            tenant_id=row.tenant_id,
            investigation_id=row.investigation_id,
            finding_type=FindingType(row.finding_type),
            title=row.title,
            statement=row.statement,
            evidence_ids=[UUID(e) for e in (row.evidence_ids or [])],
            related_hypothesis_ids=[UUID(h) for h in (row.related_hypothesis_ids or [])],
            causal_chain=[UUID(c) for c in (row.causal_chain or [])],
            severity=SeverityLevel(row.severity),
            remediation_steps=list(row.remediation_steps or []),
            confidence=row.confidence,
            created_at=row.created_at,
        )

    @staticmethod
    def _conclusion_from_orm(row: InvestigationConclusionORM) -> InvestigationConclusion:
        return InvestigationConclusion(
            tenant_id=row.tenant_id,
            investigation_id=row.investigation_id,
            status=ConclusionStatus(row.status),
            root_cause=row.root_cause,
            supporting_evidence_ids=[UUID(e) for e in (row.supporting_evidence_ids or [])],
            supporting_hypothesis_ids=[UUID(h) for h in (row.supporting_hypothesis_ids or [])],
            contradicting_evidence_ids=[UUID(e) for e in (row.contradicting_evidence_ids or [])],
            confidence=row.confidence,
            limitations=list(row.limitations or []),
            recommended_next_steps=list(row.recommended_next_steps or []),
            generated_at=row.generated_at,
        )

    # -- writes ----------------------------------------------------------

    async def save_finding(
        self, tenant_id: str, investigation_id: UUID, finding_type: str, details: dict[str, Any]
    ) -> None:
        """Legacy dict-based entry point: rehydrate the typed Finding when the
        details payload carries one, otherwise record a minimal finding row so
        the call is never silently dropped."""
        try:
            finding = Finding.model_validate({**details, "tenant_id": tenant_id})
            await self.save_finding_record(tenant_id, finding)
            return
        except Exception:
            logger.debug("save_finding details are not a Finding; storing minimal row")
        async with rls_session(self._session_factory, tenant_id) as session:
            existing = await session.scalars(
                select(FindingORM).where(
                    FindingORM.tenant_id == tenant_id,
                    FindingORM.investigation_id == investigation_id,
                    FindingORM.finding_type == finding_type,
                    FindingORM.title == str(details.get("title", finding_type)),
                )
            )
            if existing.first() is not None:
                return
            session.add(
                FindingORM(
                    tenant_id=tenant_id,
                    investigation_id=investigation_id,
                    finding_type=finding_type,
                    title=str(details.get("title", finding_type))[:256],
                    statement=str(details.get("statement", ""))[:2048] or finding_type,
                )
            )

    async def save_finding_record(self, tenant_id: str, finding: Finding) -> None:
        if finding.tenant_id != tenant_id:
            from investigation_agent_platform.domain.common.exceptions import ConcurrencyError

            raise ConcurrencyError("Finding tenant mismatch")
        async with rls_session(self._session_factory, tenant_id) as session:
            stmt = (
                insert(FindingORM)
                .values(
                    id=finding.id,
                    tenant_id=tenant_id,
                    investigation_id=finding.investigation_id,
                    finding_type=finding.finding_type.value,
                    title=finding.title,
                    statement=finding.statement,
                    severity=finding.severity.value,
                    confidence=finding.confidence,
                    evidence_ids=[str(e) for e in finding.evidence_ids],
                    related_hypothesis_ids=[str(h) for h in finding.related_hypothesis_ids],
                    causal_chain=[str(c) for c in finding.causal_chain],
                    remediation_steps=list(finding.remediation_steps),
                )
                .on_conflict_do_nothing(constraint="findings_pkey")
            )
            await session.execute(stmt)

    async def save_conclusion(self, tenant_id: str, conclusion: InvestigationConclusion) -> None:
        if conclusion.tenant_id != tenant_id:
            from investigation_agent_platform.domain.common.exceptions import ConcurrencyError

            raise ConcurrencyError("Conclusion tenant mismatch")
        async with rls_session(self._session_factory, tenant_id) as session:
            stmt = (
                insert(InvestigationConclusionORM)
                .values(
                    tenant_id=tenant_id,
                    investigation_id=conclusion.investigation_id,
                    status=conclusion.status.value,
                    root_cause=conclusion.root_cause,
                    confidence=conclusion.confidence,
                    supporting_evidence_ids=[str(e) for e in conclusion.supporting_evidence_ids],
                    supporting_hypothesis_ids=[
                        str(h) for h in conclusion.supporting_hypothesis_ids
                    ],
                    contradicting_evidence_ids=[
                        str(e) for e in conclusion.contradicting_evidence_ids
                    ],
                    limitations=list(conclusion.limitations),
                    recommended_next_steps=list(conclusion.recommended_next_steps),
                )
                .on_conflict_do_update(
                    constraint="uq_conclusion_investigation",
                    set_={
                        "status": conclusion.status.value,
                        "root_cause": conclusion.root_cause,
                        "confidence": conclusion.confidence,
                        "supporting_evidence_ids": [
                            str(e) for e in conclusion.supporting_evidence_ids
                        ],
                        "supporting_hypothesis_ids": [
                            str(h) for h in conclusion.supporting_hypothesis_ids
                        ],
                        "contradicting_evidence_ids": [
                            str(e) for e in conclusion.contradicting_evidence_ids
                        ],
                        "limitations": list(conclusion.limitations),
                        "recommended_next_steps": list(conclusion.recommended_next_steps),
                    },
                )
            )
            await session.execute(stmt)

    # -- reads -----------------------------------------------------------

    async def get_conclusion(
        self, tenant_id: str, investigation_id: UUID
    ) -> InvestigationConclusion | None:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(InvestigationConclusionORM).where(
                    InvestigationConclusionORM.tenant_id == tenant_id,
                    InvestigationConclusionORM.investigation_id == investigation_id,
                )
            )
            row = result.first()
            return self._conclusion_from_orm(row) if row is not None else None

    async def list_findings(
        self,
        tenant_id: str,
        investigation_id: UUID,
        finding_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Finding], int]:
        async with rls_session(self._session_factory, tenant_id) as session:
            filters = [
                FindingORM.tenant_id == tenant_id,
                FindingORM.investigation_id == investigation_id,
            ]
            if finding_type is not None:
                filters.append(FindingORM.finding_type == finding_type)
            total = await session.scalar(
                select(func.count()).select_from(FindingORM).where(*filters)
            )
            result = await session.scalars(
                select(FindingORM)
                .where(*filters)
                .order_by(FindingORM.created_at)
                .offset(offset)
                .limit(limit)
            )
            return [self._finding_from_orm(row) for row in result.all()], int(total or 0)

    async def get_findings_by_ids(self, tenant_id: str, finding_ids: list[UUID]) -> list[Finding]:
        if not finding_ids:
            return []
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(FindingORM).where(
                    FindingORM.tenant_id == tenant_id,
                    FindingORM.id.in_(finding_ids),
                )
            )
            return [self._finding_from_orm(row) for row in result.all()]

    async def list_tenant_findings(
        self, tenant_id: str, limit: int = 100, offset: int = 0
    ) -> tuple[list[Finding], int]:
        """Tenant-wide findings, newest first (Part 11.7 clustering input)."""
        async with rls_session(self._session_factory, tenant_id) as session:
            filters = [FindingORM.tenant_id == tenant_id]
            total = await session.scalar(
                select(func.count()).select_from(FindingORM).where(*filters)
            )
            result = await session.scalars(
                select(FindingORM)
                .where(*filters)
                .order_by(FindingORM.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
            return [self._finding_from_orm(row) for row in result.all()], int(total or 0)
