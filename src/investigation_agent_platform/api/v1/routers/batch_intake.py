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

from fastapi import APIRouter, Depends, Header, Query, status

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.errors import ApiError, sanitize_extra
from investigation_agent_platform.api.tenant import require_principal, require_tenant
from investigation_agent_platform.api.v1.schemas.batch_intake import (
    BatchIntakeAck,
    BatchIntakeBody,
    BatchIntakeDetail,
    BatchRetryAck,
    BatchSubmitAck,
    CsvPayload,
)
from investigation_agent_platform.api.v1.schemas.errors import COMMON_ERROR_RESPONSES
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
    BatchIntakeRecord,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["batch-intake"])


# Router-local input models moved to api.v1.schemas.batch_intake (TO_DO-018,
# TO_DO-026); re-exported here so existing imports keep working.
__all__ = ["BatchIntakeBody", "CsvPayload", "router"]


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
    except Exception:
        logger.warning(
            "Batch intake without quota guard",
            extra=sanitize_extra({"event": "batch_intake.quota_policy_missing"}),
        )
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
            raise ApiError(
                code="BATCH_VALIDATION", http_status=status.HTTP_400_BAD_REQUEST, message=str(exc)
            ) from exc
    raise ApiError(
        code="BATCH_INPUT_REQUIRED", http_status=status.HTTP_400_BAD_REQUEST, message="Provide either records or csv with an explicit field_mapping"
    )


@router.post(
    "/batch-intake",
    response_model=BatchSubmitAck,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_batch_intake(
    body: BatchIntakeBody,
    x_tenant_id: str = Depends(require_tenant),
    principal_id: str = Depends(require_principal),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
) -> BatchSubmitAck:
    """Ingest one batch and start its parent workflow (idempotent)."""
    if not x_idempotency_key:
        raise ApiError(
            code="IDEMPOTENCY_KEY_REQUIRED", http_status=status.HTTP_400_BAD_REQUEST, message="X-Idempotency-Key header is required"
        )
    ctx = get_app_context()
    if getattr(ctx, "batch_repo", None) is None:
        raise ApiError(
            code="BATCH_STORE_UNAVAILABLE", http_status=status.HTTP_503_SERVICE_UNAVAILABLE, message="Batch store is unavailable"
        )
    records = _records_from_body(body)
    canonical = json.dumps([r.model_dump(mode="json") for r in records], sort_keys=True)
    request_hash = hashlib.sha256(canonical.encode()).hexdigest()
    try:
        cached, reserved = await ctx.idempotency_store.reserve_or_get(
            x_tenant_id, x_idempotency_key, request_hash, operation="batch-intake"
        )
    except IdempotencyConflictError as exc:
        raise ApiError(
            code="IDEMPOTENCY_CONFLICT", http_status=status.HTTP_409_CONFLICT, message=str(exc)
        ) from exc
    except IdempotencyInProgressError as exc:
        raise ApiError(
            code="IDEMPOTENCY_CONFLICT", http_status=status.HTTP_409_CONFLICT, message=str(exc)
        ) from exc
    if not reserved:
        logger.info("Replaying cached batch-intake response for key=%s", x_idempotency_key)
        return BatchSubmitAck.model_validate(cached or {})

    service = _service(ctx)
    try:
        job, _rows = await service.ingest_batch(x_tenant_id, records, principal_id)
    except BatchValidationError as exc:
        raise ApiError(
            code="BATCH_VALIDATION", http_status=status.HTTP_400_BAD_REQUEST, message=str(exc)
        ) from exc
    except BatchQuotaExceededError as exc:
        raise ApiError(
            code="BATCH_VALIDATION", http_status=status.HTTP_429_TOO_MANY_REQUESTS, message=str(exc), headers={"Retry-After": "60"}
        ) from exc
    except ConcurrencyError as exc:
        raise ApiError(
            code="BATCH_VALIDATION", http_status=status.HTTP_409_CONFLICT, message=str(exc)
        ) from exc

    temporal_client = getattr(ctx, "temporal_client", None)
    if temporal_client is None:
        raise ApiError(
            code="TEMPORAL_UNAVAILABLE", http_status=status.HTTP_503_SERVICE_UNAVAILABLE, message="Workflow execution engine is unavailable; batch was recorded but not started"
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
        logger.exception(
            "Failed to start batch workflow",
            extra=sanitize_extra({"job_id": str(job.id), "event": "batch_intake.dispatch_failed"}),
        )
        raise ApiError(
            code="BATCH_DISPATCH_FAILED", http_status=status.HTTP_502_BAD_GATEWAY, message="Failed to start batch workflow; batch was recorded but not started"
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
        raise ApiError(
            code="CONCURRENCY_CONFLICT", http_status=status.HTTP_409_CONFLICT, message=str(exc)
        ) from exc

    from investigation_agent_platform.infrastructure.messaging.job_fanout import (
        maybe_publish_job_progress,
    )

    await maybe_publish_job_progress(ctx, x_tenant_id, queued if current is not None else job)
    response_payload = BatchSubmitAck(
        job_id=str(job.id),
        workflow_id=workflow_id,
        status=BackgroundJobStatus.QUEUED.value,
        total=len(records),
    )
    await ctx.idempotency_store.complete(
        x_tenant_id,
        x_idempotency_key,
        response_payload.model_dump(mode="json", exclude_unset=True),
        operation="batch-intake",
    )
    return response_payload


@router.get(
    "/batch-intake/{job_id}",
    response_model=BatchIntakeDetail,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def get_batch_intake(
    job_id: str,
    x_tenant_id: str = Depends(require_tenant),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> BatchIntakeDetail:
    """Batch job + summary + paginated child records."""
    ctx = get_app_context()
    try:
        job_uuid = UUID(job_id)
    except ValueError as exc:
        raise ApiError(
            code="INVALID_BATCH_ID", http_status=status.HTTP_400_BAD_REQUEST, message="Invalid batch id format"
        ) from exc
    service = _service(ctx)
    job, records, total = await service.get_batch(x_tenant_id, job_uuid, limit, offset)
    if job is None:
        raise ApiError(
            code="BATCH_NOT_FOUND", http_status=status.HTTP_404_NOT_FOUND, message="Batch not found"
        )
    summary = await service.summarize(x_tenant_id, job_uuid)
    return BatchIntakeDetail.model_validate(
        {
            "job": job.model_dump(mode="json"),
            "summary": summary.model_dump(mode="json") if summary is not None else None,
            "items": [record.model_dump(mode="json") for record in records],
            "total": total,
            "offset": offset,
            "limit": limit,
        }
    )


@router.post(
    "/batch-intake/{job_id}/cancel",
    response_model=BatchIntakeAck,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_batch_intake(
    job_id: str,
    x_tenant_id: str = Depends(require_tenant),
) -> BatchIntakeAck:
    """Cancel a batch: job marked CANCEL_REQUESTED and the parent workflow
    execution canceled (children terminate via explicit fan-out)."""
    ctx = get_app_context()
    try:
        job_uuid = UUID(job_id)
    except ValueError as exc:
        raise ApiError(
            code="INVALID_BATCH_ID", http_status=status.HTTP_400_BAD_REQUEST, message="Invalid batch id format"
        ) from exc
    service = _service(ctx)
    job = await service.cancel_batch(x_tenant_id, job_uuid)
    if job is None:
        raise ApiError(
            code="BATCH_NOT_FOUND", http_status=status.HTTP_404_NOT_FOUND, message="Batch not found"
        )
    temporal_client = getattr(ctx, "temporal_client", None)
    workflow_id = getattr(job, "workflow_id", None)
    if temporal_client is not None and workflow_id:
        try:
            handle = temporal_client.get_workflow_handle(workflow_id)
            await handle.cancel()
        except Exception:
            logger.exception(
                "Batch parent cancel failed; job row already CANCEL_REQUESTED",
                extra=sanitize_extra({"job_id": job_id, "event": "batch_intake.cancel_failed"}),
            )
    return BatchIntakeAck(job_id=job_id, status="CANCELLING")


@router.post(
    "/batch-intake/{job_id}/retry",
    response_model=BatchRetryAck,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_batch_intake(
    job_id: str,
    x_tenant_id: str = Depends(require_tenant),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
) -> BatchRetryAck:
    """Partial retry: reset FAILED rows and start a new parent run with
    attempt-suffixed child ids (no collisions with prior attempts)."""
    if not x_idempotency_key:
        raise ApiError(
            code="IDEMPOTENCY_KEY_REQUIRED", http_status=status.HTTP_400_BAD_REQUEST, message="X-Idempotency-Key header is required"
        )
    ctx = get_app_context()
    try:
        job_uuid = UUID(job_id)
    except ValueError as exc:
        raise ApiError(
            code="INVALID_BATCH_ID", http_status=status.HTTP_400_BAD_REQUEST, message="Invalid batch id format"
        ) from exc
    canonical = json.dumps({"job_id": job_id, "op": "retry"}, sort_keys=True)
    request_hash = hashlib.sha256(canonical.encode()).hexdigest()
    try:
        cached, reserved = await ctx.idempotency_store.reserve_or_get(
            x_tenant_id, x_idempotency_key, request_hash, operation="batch-intake-retry"
        )
    except IdempotencyConflictError as exc:
        raise ApiError(
            code="IDEMPOTENCY_CONFLICT", http_status=status.HTTP_409_CONFLICT, message=str(exc)
        ) from exc
    except IdempotencyInProgressError as exc:
        raise ApiError(
            code="IDEMPOTENCY_CONFLICT", http_status=status.HTTP_409_CONFLICT, message=str(exc)
        ) from exc
    if not reserved:
        return BatchRetryAck.model_validate(cached or {})

    service = _service(ctx)
    reset = await service.retry_failed(x_tenant_id, job_uuid)
    if not reset:
        raise ApiError(
            code="BATCH_NOTHING_TO_RETRY", http_status=status.HTTP_409_CONFLICT, message="No failed records to retry"
        )
    job = await ctx.background_job_repo.get_by_id(x_tenant_id, job_uuid)
    attempt = getattr(job, "attempt", 1) if job is not None else 1
    temporal_client = getattr(ctx, "temporal_client", None)
    if temporal_client is None:
        raise ApiError(
            code="TEMPORAL_UNAVAILABLE", http_status=status.HTTP_503_SERVICE_UNAVAILABLE, message="Workflow execution engine is unavailable"
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
        logger.exception(
            "Failed to start batch retry",
            extra=sanitize_extra({"job_id": job_id, "event": "batch_intake.retry_failed"}),
        )
        raise ApiError(
            code="BATCH_RETRY_FAILED", http_status=status.HTTP_502_BAD_GATEWAY, message="Failed to start batch retry"
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
            raise ApiError(
                code="CONCURRENCY_CONFLICT", http_status=status.HTTP_409_CONFLICT, message=str(exc)
            ) from exc
    from investigation_agent_platform.infrastructure.messaging.job_fanout import (
        maybe_publish_job_progress,
    )

    await maybe_publish_job_progress(ctx, x_tenant_id, updated if job is not None else None)
    response_payload = BatchRetryAck(
        job_id=job_id,
        workflow_id=workflow_id,
        status=BackgroundJobStatus.QUEUED.value,
        retried=reset,
    )
    await ctx.idempotency_store.complete(
        x_tenant_id,
        x_idempotency_key,
        response_payload.model_dump(mode="json", exclude_unset=True),
        operation="batch-intake-retry",
    )
    return response_payload


@router.delete(
    "/batch-intake/{job_id}",
    response_model=BatchIntakeAck,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def purge_batch_intake(
    job_id: str,
    x_tenant_id: str = Depends(require_tenant),
) -> BatchIntakeAck:
    """Purge record rows for terminal batches past retention (409 otherwise)."""
    ctx = get_app_context()
    try:
        job_uuid = UUID(job_id)
    except ValueError as exc:
        raise ApiError(
            code="INVALID_BATCH_ID", http_status=status.HTTP_400_BAD_REQUEST, message="Invalid batch id format"
        ) from exc
    service = _service(ctx)
    if not await service.purge_batch(x_tenant_id, job_uuid):
        raise ApiError(
            code="BATCH_PURGE_REFUSED", http_status=status.HTTP_409_CONFLICT, message="Batch is missing, active, or within retention"
        )
    return BatchIntakeAck(job_id=job_id, status="PURGED")
