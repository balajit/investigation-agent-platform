# src/investigation_agent_platform/api/v1/routers/aggregate_reports.py
"""Aggregate-report jobs and artifacts (Part 11.8).

Reports render one bound taxonomy generation to versioned HTML artifacts:
POST starts an `aggregate_report` background job (idempotent), GET latest
proxies metadata plus a short-lived signed URL (never raw storage keys),
GET history pages stored runs, DELETE purges history past retention while
honoring legal hold.
"""

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.tenant import require_principal, require_tenant
from investigation_agent_platform.application.reporting.artifacts import (
    history_prefix,
    job_id_from_history_key,
    list_history,
    read_latest,
    report_scope,
    scope_id_for_tenant,
)
from investigation_agent_platform.application.reporting.job_kinds import (
    AGGREGATE_REPORT_JOB_KIND,
    AGGREGATE_REPORT_WORKFLOW,
)
from investigation_agent_platform.domain.common.background_job import (
    BackgroundJob,
    BackgroundJobStatus,
)
from investigation_agent_platform.domain.common.exceptions import (
    ConcurrencyError,
    IdempotencyConflictError,
    IdempotencyInProgressError,
)
from investigation_agent_platform.domain.reporting.aggregate import (
    REPORT_HISTORY_RETENTION_DAYS,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["aggregate-reports"])

_SIGNED_URL_TTL_SECONDS = 3600
_HISTORY_PAGE_CAP = 10


class BuildReportBody(BaseModel):
    legal_hold: bool = Field(
        default=False,
        description="Pin a legal hold on the produced artifacts (blocks purge).",
    )


@router.post("/aggregate-reports", status_code=status.HTTP_202_ACCEPTED)
async def build_report(
    body: BuildReportBody,
    x_tenant_id: str = Depends(require_tenant),
    principal_id: str = Depends(require_principal),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
) -> dict[str, Any]:
    """Start one aggregate-report background job (idempotent)."""
    if not x_idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Idempotency-Key header is required",
        )
    ctx = get_app_context()
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True)
    request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    try:
        cached, reserved = await ctx.idempotency_store.reserve_or_get(
            x_tenant_id,
            x_idempotency_key,
            request_hash,
            operation="aggregate-reports",
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IdempotencyInProgressError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not reserved:
        logger.info("Replaying cached aggregate-reports response for key=%s", x_idempotency_key)
        return cached or {}

    job = BackgroundJob(
        id=uuid4(),
        tenant_id=x_tenant_id,
        kind=AGGREGATE_REPORT_JOB_KIND,
        created_by=principal_id,
        input_ref=canonical[:1024],
        retention_class="reports",
        quota_class="analytics",
    )
    try:
        await ctx.background_job_repo.create(job)
    except ConcurrencyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    temporal_client = getattr(ctx, "temporal_client", None)
    if temporal_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Workflow execution engine is unavailable; report was not started",
        )
    try:
        from investigation_agent_platform.application.worker.workflows import (
            AggregateReportInput,
        )

        wf_input = AggregateReportInput(
            tenant_id=x_tenant_id,
            job_id=str(job.id),
            legal_hold=body.legal_hold,
        )
        await temporal_client.start_workflow(
            AGGREGATE_REPORT_WORKFLOW,
            wf_input,
            id=f"wf-report-{job.id}",
            task_queue=getattr(
                getattr(ctx, "temporal_config", None),
                "analytics_task_queue",
                "analytics-tasks",
            ),
            execution_timeout=timedelta(seconds=3600),
            run_timeout=timedelta(seconds=3600),
        )
    except Exception as exc:
        logger.error(
            "Failed to start aggregate-report workflow",
            extra={"error": str(exc), "job_id": str(job.id)},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to start report workflow; report was not started",
        ) from exc

    try:
        current = await ctx.background_job_repo.get_by_id(x_tenant_id, job.id)
        if current is not None:
            queued = current.model_copy(
                update={
                    "status": BackgroundJobStatus.QUEUED,
                    "workflow_id": f"wf-report-{job.id}",
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
        "workflow_id": f"wf-report-{job.id}",
        "status": BackgroundJobStatus.QUEUED.value,
    }
    await ctx.idempotency_store.complete(
        x_tenant_id, x_idempotency_key, response_payload, operation="aggregate-reports"
    )
    return response_payload


@router.get("/aggregate-reports/latest")
async def get_latest_report(
    x_tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Latest report metadata plus a short-lived signed URL (no raw keys)."""
    ctx = get_app_context()
    store = getattr(ctx, "artifact_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Artifact store is unavailable",
        )
    latest = await read_latest(store, x_tenant_id)
    if latest is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No report yet")
    url = await store.signed_url(
        report_scope(x_tenant_id), latest.ref, ttl_seconds=_SIGNED_URL_TTL_SECONDS
    )
    return {
        "artifact": latest.ref.model_dump(mode="json"),
        "metadata": latest.metadata,
        "signed_url": url,
        "expires_in_seconds": _SIGNED_URL_TTL_SECONDS,
    }


@router.get("/aggregate-reports/history")
async def get_report_history(
    x_tenant_id: str = Depends(require_tenant),
    cursor: str | None = Query(default=None, max_length=4096),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """Paginated stored report runs (newest first where the adapter orders so)."""
    ctx = get_app_context()
    store = getattr(ctx, "artifact_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Artifact store is unavailable",
        )
    page = await list_history(store, x_tenant_id, cursor, limit)
    return {
        "items": [ref.model_dump(mode="json") for ref in page.items],
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
    }


@router.delete("/aggregate-reports", status_code=status.HTTP_202_ACCEPTED)
async def purge_reports(
    x_tenant_id: str = Depends(require_tenant),
    purge_latest: bool = Query(
        default=False,
        description="Also delete the latest artifact (blocked by legal hold).",
    ),
) -> dict[str, Any]:
    """Purge history past retention; honors legal hold (409 when held)."""
    ctx = get_app_context()
    store = getattr(ctx, "artifact_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Artifact store is unavailable",
        )
    latest = await read_latest(store, x_tenant_id)
    if latest is not None and latest.metadata.get("legal_hold") == "true":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Legal hold is set; purge refused",
        )
    scope = report_scope(x_tenant_id)
    scope_id = scope_id_for_tenant(x_tenant_id)
    purged: list[str] = []
    now = datetime.now(UTC)
    cursor: str | None = None
    for _ in range(_HISTORY_PAGE_CAP):
        page = await store.list(scope, history_prefix(scope_id), cursor, 50)
        for ref in page.items:
            job_id = job_id_from_history_key(ref.key)
            if job_id is None:
                continue  # never delete unattributable keys
            if await _retention_expired(ctx, x_tenant_id, job_id, now):
                await store.delete(scope, ref)
                purged.append(ref.key)
        cursor = page.next_cursor
        if not page.has_more or cursor is None:
            break
    if purge_latest and latest is not None:
        from investigation_agent_platform.application.reporting.artifacts import (
            latest_keys,
            ref_for_key,
        )

        _, json_key = latest_keys(scope_id)
        await store.delete(scope, latest.ref)
        purged.append(latest.ref.key)
        try:
            await store.delete(scope, ref_for_key(json_key, "application/json", 0))
            purged.append(json_key)
        except Exception as exc:
            logger.warning("Latest sidecar purge skipped", extra={"error": str(exc)})
    return {"status": "PURGED", "purged": purged}


async def _retention_expired(ctx: Any, tenant_id: str, job_id: UUID, now: datetime) -> bool:
    """True when the owning job is terminal and older than retention.

    Orphaned artifacts (job row gone) are treated as expired: their
    retention already elapsed with the job record.
    """
    repo = getattr(ctx, "background_job_repo", None)
    if repo is None:
        return False
    job = await repo.get_by_id(tenant_id, job_id)
    if job is None:
        return True
    if not job.is_terminal:
        return False
    created: datetime = job.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (now - created).days > REPORT_HISTORY_RETENTION_DAYS
