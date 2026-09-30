# src/investigation_agent_platform/infrastructure/persistence/background_job_repository.py
"""SQLAlchemy background job repository with OCC (Part 11.3B)."""

from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update

from investigation_agent_platform.domain.common.background_job import (
    BackgroundJob,
    BackgroundJobStage,
    BackgroundJobStatus,
)
from investigation_agent_platform.domain.common.exceptions import ConcurrencyError
from investigation_agent_platform.infrastructure.persistence.models import BackgroundJobORM
from investigation_agent_platform.infrastructure.persistence.rls import rls_session


class SqlAlchemyBackgroundJobRepository:
    """PostgreSQL-backed BackgroundJobRepository with strict tenant scoping."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    @staticmethod
    def _to_orm(job: BackgroundJob) -> BackgroundJobORM:
        return BackgroundJobORM(
            id=job.id,
            tenant_id=job.tenant_id,
            application_id=job.application_id,
            kind=job.kind,
            contract_version=job.contract_version,
            status=job.status.value,
            progress=job.progress,
            total=job.total,
            created_by=job.created_by,
            created_at=job.created_at,
            started_at=job.started_at,
            completed_at=job.completed_at,
            attempt=job.attempt,
            parent_job_id=job.parent_job_id,
            workflow_id=job.workflow_id,
            run_id=job.run_id,
            input_ref=job.input_ref,
            result_ref=job.result_ref,
            error=job.error,
            quota_class=job.quota_class,
            retention_class=job.retention_class,
            stages_json={"stages": [stage.model_dump(mode="json") for stage in job.stages]},
            version=job.version,
        )

    @staticmethod
    def _from_orm(row: BackgroundJobORM) -> BackgroundJob:
        raw_stages: list[dict[str, Any]] = []
        if isinstance(row.stages_json, dict):
            maybe = row.stages_json.get("stages")
            if isinstance(maybe, list):
                raw_stages = [s for s in maybe if isinstance(s, dict)]
        return BackgroundJob(
            id=row.id,
            tenant_id=row.tenant_id,
            application_id=row.application_id,
            kind=row.kind,
            contract_version=row.contract_version,
            status=BackgroundJobStatus(row.status),
            progress=row.progress,
            total=row.total,
            created_by=row.created_by,
            created_at=row.created_at,
            started_at=row.started_at,
            completed_at=row.completed_at,
            attempt=row.attempt,
            parent_job_id=row.parent_job_id,
            workflow_id=row.workflow_id,
            run_id=row.run_id,
            input_ref=row.input_ref,
            result_ref=row.result_ref,
            error=row.error,
            quota_class=row.quota_class,
            retention_class=row.retention_class,
            stages=[BackgroundJobStage.model_validate(s) for s in raw_stages],
            version=row.version,
        )

    async def create(self, job: BackgroundJob) -> None:
        async with rls_session(self._session_factory, job.tenant_id) as session:
            session.add(self._to_orm(job))

    async def get_by_id(self, tenant_id: str, job_id: UUID) -> BackgroundJob | None:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(BackgroundJobORM).where(
                    BackgroundJobORM.id == job_id,
                    BackgroundJobORM.tenant_id == tenant_id,
                )
            )
            row = result.first()
            return self._from_orm(row) if row is not None else None

    async def save(self, tenant_id: str, job: BackgroundJob, expected_version: int) -> None:
        if job.tenant_id != tenant_id:
            raise ConcurrencyError("Background job tenant mismatch")
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.execute(
                update(BackgroundJobORM)
                .where(
                    BackgroundJobORM.id == job.id,
                    BackgroundJobORM.tenant_id == tenant_id,
                    BackgroundJobORM.version == expected_version,
                )
                .values(
                    status=job.status.value,
                    progress=job.progress,
                    total=job.total,
                    started_at=job.started_at,
                    completed_at=job.completed_at,
                    attempt=job.attempt,
                    workflow_id=job.workflow_id,
                    run_id=job.run_id,
                    input_ref=job.input_ref,
                    result_ref=job.result_ref,
                    error=job.error,
                    stages_json={"stages": [stage.model_dump(mode="json") for stage in job.stages]},
                    version=expected_version + 1,
                )
            )
            if result.rowcount == 0:  # type: ignore[attr-defined]
                await session.rollback()
                raise ConcurrencyError(
                    f"Background job {job.id} was modified concurrently; "
                    f"expected version {expected_version}.",
                    details={
                        "job_id": str(job.id),
                        "expected_version": expected_version,
                        "tenant_id": tenant_id,
                    },
                )

    async def list_jobs(
        self,
        tenant_id: str,
        kind: str | None = None,
        status: BackgroundJobStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[BackgroundJob], int]:
        async with rls_session(self._session_factory, tenant_id) as session:
            filters = [BackgroundJobORM.tenant_id == tenant_id]
            if kind is not None:
                filters.append(BackgroundJobORM.kind == kind)
            if status is not None:
                filters.append(BackgroundJobORM.status == status.value)
            total = await session.scalar(
                select(func.count()).select_from(BackgroundJobORM).where(*filters)
            )
            result = await session.scalars(
                select(BackgroundJobORM)
                .where(*filters)
                .order_by(BackgroundJobORM.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
            return [self._from_orm(row) for row in result.all()], int(total or 0)
