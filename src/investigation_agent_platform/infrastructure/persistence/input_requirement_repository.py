# src/investigation_agent_platform/infrastructure/persistence/input_requirement_repository.py
"""SQLAlchemy input requirement/fulfillment repository (Part 11.5)."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from investigation_agent_platform.domain.common.exceptions import ConcurrencyError
from investigation_agent_platform.domain.investigation.input_requirements import (
    InputFulfillment,
    InputRequirement,
    RequirementState,
)
from investigation_agent_platform.domain.investigation.models import InvestigationStatus
from investigation_agent_platform.infrastructure.persistence.models import (
    InputFulfillmentORM,
    InputRequirementORM,
)
from investigation_agent_platform.infrastructure.persistence.rls import rls_session


class SqlAlchemyInputRequirementRepository:
    """PostgreSQL-backed InputRequirementRepository with strict tenant scoping."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    @staticmethod
    def _requirement_to_orm(req: InputRequirement, tenant_id: str) -> InputRequirementORM:
        return InputRequirementORM(
            id=req.requirement_id,
            tenant_id=tenant_id,
            investigation_id=req.investigation_id,
            requirement_version=req.requirement_version,
            reason=req.reason,
            json_schema=dict(req.json_schema),
            classification=req.classification,
            resume_status=req.resume_status.value,
            promote_to_evidence=req.promote_to_evidence,
            state=req.state.value,
            requested_at=req.requested_at,
            expires_at=req.expires_at,
            provenance_json=dict(req.provenance),
        )

    @staticmethod
    def _requirement_from_orm(row: InputRequirementORM) -> InputRequirement:
        return InputRequirement(
            requirement_id=row.id,
            requirement_version=row.requirement_version,
            tenant_id=row.tenant_id,
            investigation_id=row.investigation_id,
            reason=row.reason,
            json_schema=dict(row.json_schema or {}),
            classification=row.classification,
            resume_status=InvestigationStatus(row.resume_status),
            promote_to_evidence=bool(row.promote_to_evidence),
            state=RequirementState(row.state),
            requested_at=row.requested_at,
            expires_at=row.expires_at,
            provenance=dict(row.provenance_json or {}),
        )

    @staticmethod
    def _fulfillment_from_orm(row: InputFulfillmentORM) -> InputFulfillment:
        return InputFulfillment(
            requirement_id=row.requirement_id,
            requirement_version=row.requirement_version,
            tenant_id=row.tenant_id,
            investigation_id=row.investigation_id,
            fulfilled_by=row.fulfilled_by,
            content_digest=row.content_digest,
            content_bytes=row.content_bytes,
            fulfilled_at=row.fulfilled_at,
        )

    async def create_requirement(self, tenant_id: str, requirement: InputRequirement) -> None:
        if requirement.tenant_id != tenant_id:
            raise ConcurrencyError("Input requirement tenant mismatch")
        async with rls_session(self._session_factory, tenant_id) as session:
            session.add(self._requirement_to_orm(requirement, tenant_id))

    async def get_pending(self, tenant_id: str, investigation_id: UUID) -> list[InputRequirement]:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(InputRequirementORM)
                .where(
                    InputRequirementORM.tenant_id == tenant_id,
                    InputRequirementORM.investigation_id == investigation_id,
                    InputRequirementORM.state == RequirementState.PENDING.value,
                )
                .order_by(InputRequirementORM.requested_at)
            )
            return [self._requirement_from_orm(row) for row in result.all()]

    async def get_by_id(self, tenant_id: str, requirement_id: UUID) -> InputRequirement | None:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(InputRequirementORM).where(
                    InputRequirementORM.id == requirement_id,
                    InputRequirementORM.tenant_id == tenant_id,
                )
            )
            row = result.first()
            return self._requirement_from_orm(row) if row is not None else None

    async def set_state(
        self,
        tenant_id: str,
        requirement_id: UUID,
        expected_version: int,
        state: RequirementState,
    ) -> None:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(InputRequirementORM).where(
                    InputRequirementORM.id == requirement_id,
                    InputRequirementORM.tenant_id == tenant_id,
                )
            )
            row = result.first()
            if row is None:
                raise ConcurrencyError(
                    f"Input requirement {requirement_id} not found",
                    details={"requirement_id": str(requirement_id)},
                )
            if row.requirement_version != expected_version:
                await session.rollback()
                raise ConcurrencyError(
                    f"Input requirement {requirement_id} version mismatch",
                    details={
                        "requirement_id": str(requirement_id),
                        "expected_version": expected_version,
                    },
                )
            row.state = state.value

    async def record_fulfillment(self, tenant_id: str, fulfillment: InputFulfillment) -> None:
        if fulfillment.tenant_id != tenant_id:
            raise ConcurrencyError("Input fulfillment tenant mismatch")
        async with rls_session(self._session_factory, tenant_id) as session:
            stmt = (
                pg_insert(InputFulfillmentORM)
                .values(
                    tenant_id=tenant_id,
                    investigation_id=fulfillment.investigation_id,
                    requirement_id=fulfillment.requirement_id,
                    requirement_version=fulfillment.requirement_version,
                    fulfilled_by=fulfillment.fulfilled_by,
                    content_digest=fulfillment.content_digest,
                    content_bytes=fulfillment.content_bytes,
                    fulfilled_at=fulfillment.fulfilled_at,
                )
                .on_conflict_do_nothing(constraint="uq_fulfillment_req_version")
            )
            await session.execute(stmt)

    async def list_fulfillments(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[InputFulfillment]:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(InputFulfillmentORM)
                .where(
                    InputFulfillmentORM.tenant_id == tenant_id,
                    InputFulfillmentORM.investigation_id == investigation_id,
                )
                .order_by(InputFulfillmentORM.fulfilled_at)
            )
            return [self._fulfillment_from_orm(row) for row in result.all()]
