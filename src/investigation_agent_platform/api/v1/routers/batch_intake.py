# src/investigation_agent_platform/api/v1/routers/batch_intake.py
"""Bulk investigation intake (Part 11.9).

POST ingests up to 500 validated records (JSON or CSV-with-mapping),
pre-creates their investigations, and starts the bounded
`BulkIntakeWorkflow` parent. Status reads paginate child records; cancel
propagates to live children (TERMINATE-equivalent via explicit fan-out);
retry re-dispatches FAILED rows with attempt-suffixed child ids; purge
deletes record rows for terminal batches past retention.
"""

import hashlib
import json
import logging
from datetime import timedelta
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.tenant import require_principal, require_tenant
from investigation_agent_platform.application.intake.batch_service import (
    BatchQuotaExceededError,
    BatchValidationError,
    BulkIntakeService,
)
from investigation_agent_platform.application.intake.csv_adapter import parse_csv_batch
from investigation_agent_platform.application.intake.job_kinds import (
    BATCH_INTAKE_WORKFLOW,
)
from investigation_agent_platform.domain.common.background_job import (
    BackgroundJobStatus,
)
from investigation_agent_platform.domain.common.exceptions import (
    ConcurrencyError,
    IdempotencyConflictError,
    IdempotencyInProgressError,
)
from investigation_agent_platform.domain.intake.batch import (
    MAX_BATCH_RECORDS,
    BatchIntakeRecord,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["batch-intake"])


class CsvPayload(BaseModel):
    text: str = Field(..., min_length=1, max_length=1_000_000)
    field_mapping: dict[str, Any] = Field(...)


class BatchIntakeBody(BaseModel):
    records: list[BatchIntakeRecord] | None = Field(default=None, max_length=MAX_BATCH_RECORDS)
    csv: CsvPayload | None = Field(default=None)


def _service(ctx: Any) -> BulkIntakeService:
    async def _create_investigation(tenant_id: str, record: BatchIntakeRecord) -> Any:
        from investigation_agent_platform.domain.investigation.models import (
            InvestigationRequest,
        )

        service = ctx.create_investigation_service()
        return await service.execute(
            InvestigationRequest(
                application_id=record.application_id,
                problem_description=record.problem_description,
                session_id=record.session_id or f"sess_{record.application_id}",
                requested_by="batch-intake",
                priority=record.priority,
                parameters=dict(record.parameters),
            ),
            tenant_id=tenant_id,
        )

    quota_policy = None
    try:
        from investigation_agent_platform.domain.common.quotas import QuotaPolicy
        from investigation_agent_platform.infrastructure.configuration.config import (
            load_application_config_from_env as _load_cfg,
        )

        quota_policy = QuotaPolicy.model_validate(_load_cfg().quotas.model_dump(mode="json"))
    except Exception as exc:
        logger.warning("Batch intake without quota guard", extra={"error": str(exc)})
    return BulkIntakeService(
        batch_repo=getattr(ctx, "batch_repo", None),
        job_repo=getattr(ctx, "background_job_repo", None),
        profile_repo=getattr(ctx, "profile_repo", None),
        investigation_creator=_create_investigation,
        quota_enforcer=getattr(ctx, "quota_enforcer", None),
        quota_policy=quota_policy,
    )


def _records_from_body(body: BatchIntakeBody) -> list[BatchIntakeRecord]:
    if body.records is not None:
        return list(body.records)
    if body.csv is not None:
        try:
            return parse_csv_batch(body.csv.text, dict(body.csv.field_mapping))
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Provide either records or csv with an explicit field_mapping",
    )


@router.post("/batch-intake", status_code=status.HTTP_202_ACCEPTED)
async def create_batch_intake(
    body: BatchIntakeBody,
    x_tenant_id: str = Depends(require_tenant),
    principal_id: str = Depends(require_principal),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
) -> dict[str, Any]:
    """Ingest one batch and start its parent workflow (idempotent)."""
    if not x_idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Idempotency-Key header is required",
        )
    ctx = get_app_context()
    if getattr(ctx, "batch_repo", None) is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Batch store is unavailable",
        )
    records = _records_from_body(body)
    canonical = json.dumps([r.model_dump(mode="json") for r in records], sort_keys=True)
    request_hash = hashlib.sha256(canonical.encode()).hexdigest()
    try:
        cached, reserved = await ctx.idempotency_store.reserve_or_get(
            x_tenant_id, x_idempotency_key, request_hash, operation="batch-intake"
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IdempotencyInProgressError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not reserved:
        logger.info("Replaying cached batch-intake response for key=%s", x_idempotency_key)
        return cached or {}

    service = _service(ctx)
    try:
        job, _rows = await service.ingest_batch(x_tenant_id, records, principal_id)
    except BatchValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except BatchQuotaExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": "60"},
        ) from exc
    except ConcurrencyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    temporal_client = getattr(ctx, "temporal_client", None)
    if temporal_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Workflow execution engine is unavailable; batch was recorded but not started",
        )
    workflow_id = f"wf-batch-{job.id.hex}"
    try:
        from investigation_agent_platform.application.worker.workflows import (
            BulkIntakeInput,
        )

        await temporal_client.start_workflow(
            BATCH_INTAKE_WORKFLOW,
            BulkIntakeInput(tenant_id=x_tenant_id, job_id=str(job.id)),
            id=workflow_id,
            task_queue=getattr(
                getattr(ctx, "temporal_config", None), "task_queue", "investigation-tasks"
            ),
            execution_timeout=timedelta(seconds=7200),
            run_timeout=timedelta(seconds=7200),
        )
    except Exception as exc:
        logger.error(
            "Failed to start batch workflow",
            extra={"error": str(exc), "job_id": str(job.id)},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to start batch workflow; batch was recorded but not started",
        ) from exc

    try:
        current = await ctx.background_job_repo.get_by_id(x_tenant_id, job.id)
        if current is not None:
            queued = current.model_copy(
                update={
                    "status": BackgroundJobStatus.QUEUED,
                    "workflow_id": workflow_id,
                    "version": current.version + 1,
                }
            )
            await ctx.background_job_repo.save(x_tenant_id, queued, current.version)
    except ConcurrencyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    from investigation_agent_platform.infrastructure.messaging.job_fanout import (
        maybe_publish_job_progress,
    )

    await maybe_publish_job_progress(ctx, x_tenant_id, queued if current is not None else job)
    response_payload: dict[str, Any] = {
        "job_id": str(job.id),
        "workflow_id": workflow_id,
        "status": BackgroundJobStatus.QUEUED.value,
        "total": len(records),
    }
    await ctx.idempotency_store.complete(
        x_tenant_id, x_idempotency_key, response_payload, operation="batch-intake"
    )
    return response_payload


@router.get("/batch-intake/{job_id}")
async def get_batch_intake(
    job_id: str,
    x_tenant_id: str = Depends(require_tenant),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Batch job + summary + paginated child records."""
    ctx = get_app_context()
    try:
        job_uuid = UUID(job_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid batch id format"
        ) from exc
    service = _service(ctx)
    job, records, total = await service.get_batch(x_tenant_id, job_uuid, limit, offset)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")
    summary = await service.summarize(x_tenant_id, job_uuid)
    return {
        "job": job.model_dump(mode="json"),
        "summary": summary.model_dump(mode="json") if summary is not None else None,
        "items": [record.model_dump(mode="json") for record in records],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.post("/batch-intake/{job_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_batch_intake(
    job_id: str,
    x_tenant_id: str = Depends(require_tenant),
) -> dict[str, str]:
    """Cancel a batch: job marked CANCEL_REQUESTED and the parent workflow
    execution canceled (children terminate via explicit fan-out)."""
    ctx = get_app_context()
    try:
        job_uuid = UUID(job_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid batch id format"
        ) from exc
    service = _service(ctx)
    job = await service.cancel_batch(x_tenant_id, job_uuid)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Batch not found")
    temporal_client = getattr(ctx, "temporal_client", None)
    workflow_id = getattr(job, "workflow_id", None)
    if temporal_client is not None and workflow_id:
        try:
            handle = temporal_client.get_workflow_handle(workflow_id)
            await handle.cancel()
        except Exception as exc:
            logger.warning(
                "Batch parent cancel failed; job row already CANCEL_REQUESTED",
                extra={"job_id": job_id, "error": str(exc)},
            )
    return {"job_id": job_id, "status": "CANCELLING"}


@router.post("/batch-intake/{job_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_batch_intake(
    job_id: str,
    x_tenant_id: str = Depends(require_tenant),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
) -> dict[str, Any]:
    """Partial retry: reset FAILED rows and start a new parent run with
    attempt-suffixed child ids (no collisions with prior attempts)."""
    if not x_idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Idempotency-Key header is required",
        )
    ctx = get_app_context()
    try:
        job_uuid = UUID(job_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid batch id format"
        ) from exc
    canonical = json.dumps({"job_id": job_id, "op": "retry"}, sort_keys=True)
    request_hash = hashlib.sha256(canonical.encode()).hexdigest()
    try:
        cached, reserved = await ctx.idempotency_store.reserve_or_get(
            x_tenant_id, x_idempotency_key, request_hash, operation="batch-intake-retry"
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IdempotencyInProgressError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not reserved:
        return cached or {}

    service = _service(ctx)
    reset = await service.retry_failed(x_tenant_id, job_uuid)
    if not reset:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No failed records to retry",
        )
    job = await ctx.background_job_repo.get_by_id(x_tenant_id, job_uuid)
    attempt = getattr(job, "attempt", 1) if job is not None else 1
    temporal_client = getattr(ctx, "temporal_client", None)
    if temporal_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Workflow execution engine is unavailable",
        )
    workflow_id = f"wf-batch-{job_uuid.hex}-r{attempt}"
    try:
        from investigation_agent_platform.application.worker.workflows import (
            BulkIntakeInput,
        )

        await temporal_client.start_workflow(
            BATCH_INTAKE_WORKFLOW,
            BulkIntakeInput(tenant_id=x_tenant_id, job_id=job_id, attempt=attempt),
            id=workflow_id,
            task_queue=getattr(
                getattr(ctx, "temporal_config", None), "task_queue", "investigation-tasks"
            ),
            execution_timeout=timedelta(seconds=7200),
            run_timeout=timedelta(seconds=7200),
        )
    except Exception as exc:
        logger.error("Failed to start batch retry", extra={"job_id": job_id})
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to start batch retry",
        ) from exc
    if job is not None:
        try:
            updated = job.model_copy(
                update={
                    "status": BackgroundJobStatus.QUEUED,
                    "workflow_id": workflow_id,
                    "version": job.version + 1,
                }
            )
            await ctx.background_job_repo.save(x_tenant_id, updated, job.version)
        except ConcurrencyError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    from investigation_agent_platform.infrastructure.messaging.job_fanout import (
        maybe_publish_job_progress,
    )

    await maybe_publish_job_progress(ctx, x_tenant_id, updated if job is not None else None)
    response_payload: dict[str, Any] = {
        "job_id": job_id,
        "workflow_id": workflow_id,
        "status": BackgroundJobStatus.QUEUED.value,
        "retried": reset,
    }
    await ctx.idempotency_store.complete(
        x_tenant_id, x_idempotency_key, response_payload, operation="batch-intake-retry"
    )
    return response_payload


@router.delete("/batch-intake/{job_id}")
async def purge_batch_intake(
    job_id: str,
    x_tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Purge record rows for terminal batches past retention (409 otherwise)."""
    ctx = get_app_context()
    try:
        job_uuid = UUID(job_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid batch id format"
        ) from exc
    service = _service(ctx)
    if not await service.purge_batch(x_tenant_id, job_uuid):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Batch is missing, active, or within retention",
        )
    return {"job_id": job_id, "status": "PURGED"}
