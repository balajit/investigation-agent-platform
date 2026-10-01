# src/investigation_agent_platform/infrastructure/persistence/batch_intake_repository.py
"""SQLAlchemy batch intake record repository (Part 11.9)."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from investigation_agent_platform.domain.common.exceptions import ConcurrencyError
from investigation_agent_platform.domain.intake.batch import (
    BatchRecordResult,
    BatchRecordStatus,
)
from investigation_agent_platform.infrastructure.persistence.models import (
    BatchIntakeRecordORM,
)
from investigation_agent_platform.infrastructure.persistence.rls import rls_session


def _row_from_result(
    tenant_id: str, job_id: UUID, record: BatchRecordResult
) -> BatchIntakeRecordORM:
    now = datetime.now(UTC)
    return BatchIntakeRecordORM(
        tenant_id=tenant_id,
        job_id=job_id,
        record_index=record.record_index,
        external_key=record.external_key,
        application_id=record.application_id,
        status=record.status.value,
        investigation_id=record.investigation_id,
        child_workflow_id=record.child_workflow_id,
        attempt=record.attempt,
        error=record.error,
        created_at=record.created_at,
        updated_at=now,
    )


def _result_from_row(row: BatchIntakeRecordORM) -> BatchRecordResult:
    return BatchRecordResult(
        record_index=row.record_index,
        external_key=row.external_key,
        application_id=row.application_id,
        status=BatchRecordStatus(row.status),
        investigation_id=row.investigation_id,
        child_workflow_id=row.child_workflow_id,
        attempt=row.attempt,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyBatchIntakeRepository:
    """PostgreSQL-backed batch records with strict tenant scoping."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    async def create_records(
        self, tenant_id: str, job_id: UUID, records: list[BatchRecordResult]
    ) -> None:
        async with rls_session(self._session_factory, tenant_id) as session:
            for record in records:
                session.add(_row_from_result(tenant_id, job_id, record))

    async def list_records(
        self, tenant_id: str, job_id: UUID, limit: int = 100, offset: int = 0
    ) -> tuple[list[BatchRecordResult], int]:
        async with rls_session(self._session_factory, tenant_id) as session:
            total = (
                await session.scalar(
                    select(func.count())
                    .select_from(BatchIntakeRecordORM)
                    .where(
                        BatchIntakeRecordORM.tenant_id == tenant_id,
                        BatchIntakeRecordORM.job_id == job_id,
                    )
                )
            ) or 0
            rows = (
                await session.scalars(
                    select(BatchIntakeRecordORM)
                    .where(
                        BatchIntakeRecordORM.tenant_id == tenant_id,
                        BatchIntakeRecordORM.job_id == job_id,
                    )
                    .order_by(BatchIntakeRecordORM.record_index.asc())
                    .limit(max(1, min(limit, 500)))
                    .offset(max(0, offset))
                )
            ).all()
            return [_result_from_row(row) for row in rows], int(total)

    async def get_record(
        self, tenant_id: str, job_id: UUID, record_index: int
    ) -> BatchRecordResult | None:
        async with rls_session(self._session_factory, tenant_id) as session:
            row = (
                await session.scalars(
                    select(BatchIntakeRecordORM).where(
                        BatchIntakeRecordORM.tenant_id == tenant_id,
                        BatchIntakeRecordORM.job_id == job_id,
                        BatchIntakeRecordORM.record_index == record_index,
                    )
                )
            ).first()
            return _result_from_row(row) if row is not None else None

    async def save_record(self, tenant_id: str, job_id: UUID, record: BatchRecordResult) -> None:
        async with rls_session(self._session_factory, tenant_id) as session:
            row = (
                await session.scalars(
                    select(BatchIntakeRecordORM).where(
                        BatchIntakeRecordORM.tenant_id == tenant_id,
                        BatchIntakeRecordORM.job_id == job_id,
                        BatchIntakeRecordORM.record_index == record.record_index,
                    )
                )
            ).first()
            if row is None:
                raise ConcurrencyError("Batch record not found")
            row.status = record.status.value
            row.investigation_id = record.investigation_id
            row.child_workflow_id = record.child_workflow_id
            row.attempt = record.attempt
            row.error = record.error
            row.updated_at = datetime.now(UTC)

    async def reset_failed(self, tenant_id: str, job_id: UUID, attempt: int) -> int:
        async with rls_session(self._session_factory, tenant_id) as session:
            rows = (
                await session.scalars(
                    select(BatchIntakeRecordORM).where(
                        BatchIntakeRecordORM.tenant_id == tenant_id,
                        BatchIntakeRecordORM.job_id == job_id,
                        BatchIntakeRecordORM.status == BatchRecordStatus.FAILED.value,
                    )
                )
            ).all()
            for row in rows:
                row.status = BatchRecordStatus.PENDING.value
                row.attempt = attempt
                row.error = None
                row.child_workflow_id = None
                row.updated_at = datetime.now(UTC)
            return len(rows)

    async def delete_job_records(self, tenant_id: str, job_id: UUID) -> int:
        async with rls_session(self._session_factory, tenant_id) as session:
            rows = (
                await session.scalars(
                    select(BatchIntakeRecordORM).where(
                        BatchIntakeRecordORM.tenant_id == tenant_id,
                        BatchIntakeRecordORM.job_id == job_id,
                    )
                )
            ).all()
            for row in rows:
                await session.delete(row)
            return len(rows)
