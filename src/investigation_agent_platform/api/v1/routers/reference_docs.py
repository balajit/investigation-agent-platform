# src/investigation_agent_platform/api/v1/routers/reference_docs.py
"""Reference-document status/reindex/search endpoints (Part 11.6).

Read paths serve platform-owned retrieval; reindex dispatches the
indexing-queue workflow as a background job (idempotent). Deletion purges
one source's chunks + generations.
"""

import hashlib
import json
import logging
from datetime import timedelta
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.tenant import require_principal, require_tenant
from investigation_agent_platform.application.reference.job_kinds import (
    REFERENCE_DOCS_INDEX_WORKFLOW,
    REFERENCE_DOCS_REINDEX_JOB_KIND,
)
from investigation_agent_platform.application.reference.reference_service import (
    ReferenceDocumentService,
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

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reference-docs"])


def _service(ctx: Any) -> ReferenceDocumentService:
    chunk_repo = getattr(ctx, "reference_repo", None)
    if chunk_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reference store is unavailable",
        )
    return ReferenceDocumentService(
        chunk_repo=chunk_repo,
        profile_repo=getattr(ctx, "profile_repo", None),
        embedder=None,
    )


class ReindexBody(BaseModel):
    application_id: str | None = Field(default=None, max_length=128)
    source_id: str = Field(..., min_length=1, max_length=256)


class RollbackBody(BaseModel):
    application_id: str | None = Field(default=None, max_length=128)
    source_id: str = Field(..., min_length=1, max_length=256)
    generation: int = Field(..., ge=1)


@router.get("/reference-docs/sources")
async def list_reference_sources(
    x_tenant_id: str = Depends(require_tenant),
    application_id: str | None = Query(default=None, max_length=128),
) -> dict[str, Any]:
    """Configured reference sources with stable ids."""
    service = _service(get_app_context())
    return {
        "items": await service.list_sources(x_tenant_id, application_id),
    }


@router.get("/reference-docs/search")
async def search_reference_docs(
    x_tenant_id: str = Depends(require_tenant),
    q: str = Query(..., min_length=1, max_length=2000),
    application_id: str | None = Query(default=None, max_length=128),
    kinds: str | None = Query(
        default=None, max_length=256, description="Comma-separated source kinds."
    ),
    cursor: str | None = Query(default=None, max_length=4096),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """Hybrid search over active reference chunks (tombstones excluded)."""
    service = _service(get_app_context())
    try:
        hits, next_cursor = await service.search(
            x_tenant_id,
            q,
            application_id=application_id,
            kinds=[kind.strip() for kind in (kinds or "").split(",") if kind.strip()] or None,
            cursor=cursor,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {
        "items": [hit.model_dump(mode="json") for hit in hits],
        "next_cursor": next_cursor,
        "has_more": next_cursor is not None,
    }


@router.get("/reference-docs/status")
async def reference_docs_status(
    x_tenant_id: str = Depends(require_tenant),
    source_id: str = Query(..., min_length=1, max_length=256),
    application_id: str | None = Query(default=None, max_length=128),
) -> dict[str, Any]:
    """Current generation status for one source."""
    service = _service(get_app_context())
    try:
        result = await service.index_status(x_tenant_id, source_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    _ = application_id
    return result.model_dump(mode="json")


@router.post("/reference-docs/reindex", status_code=status.HTTP_202_ACCEPTED)
async def reindex_reference_docs(
    body: ReindexBody,
    x_tenant_id: str = Depends(require_tenant),
    principal_id: str = Depends(require_principal),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
) -> dict[str, Any]:
    """Start a source reindex as a background job on the indexing queue."""
    if not x_idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Idempotency-Key header is required",
        )
    ctx = get_app_context()
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True)
    request_hash = hashlib.sha256(canonical.encode()).hexdigest()
    try:
        cached, reserved = await ctx.idempotency_store.reserve_or_get(
            x_tenant_id,
            x_idempotency_key,
            request_hash,
            operation="reference-docs-reindex",
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IdempotencyInProgressError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not reserved:
        logger.info("Replaying cached reference reindex for key=%s", x_idempotency_key)
        return cached or {}

    job = BackgroundJob(
        id=uuid4(),
        tenant_id=x_tenant_id,
        kind=REFERENCE_DOCS_REINDEX_JOB_KIND,
        created_by=principal_id,
        input_ref=canonical[:1024],
        quota_class="indexing",
    )
    try:
        await ctx.background_job_repo.create(job)
    except ConcurrencyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    temporal_client = getattr(ctx, "temporal_client", None)
    if temporal_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Workflow execution engine is unavailable; reindex was not started",
        )
    try:
        from investigation_agent_platform.application.worker.workflows import (
            ReferenceDocsIndexInput,
        )

        wf_input = ReferenceDocsIndexInput(
            tenant_id=x_tenant_id,
            job_id=str(job.id),
            source_id=body.source_id,
            application_id=body.application_id or "",
        )
        await temporal_client.start_workflow(
            REFERENCE_DOCS_INDEX_WORKFLOW,
            wf_input,
            id=f"wf-reindex-{job.id}",
            task_queue=getattr(
                getattr(ctx, "temporal_config", None),
                "indexing_task_queue",
                "indexing-tasks",
            ),
            execution_timeout=timedelta(seconds=3600),
            run_timeout=timedelta(seconds=3600),
        )
    except Exception as exc:
        logger.error(
            "Failed to start reindex workflow",
            extra={"error": str(exc), "job_id": str(job.id)},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to start reindex workflow; reindex was not started",
        ) from exc

    try:
        current = await ctx.background_job_repo.get_by_id(x_tenant_id, job.id)
        if current is not None:
            queued = current.model_copy(
                update={
                    "status": BackgroundJobStatus.QUEUED,
                    "workflow_id": f"wf-reindex-{job.id}",
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
        "workflow_id": f"wf-reindex-{job.id}",
        "status": BackgroundJobStatus.QUEUED.value,
    }
    await ctx.idempotency_store.complete(
        x_tenant_id, x_idempotency_key, response_payload, operation="reference-docs-reindex"
    )
    return response_payload


@router.post("/reference-docs/rollback")
async def rollback_reference_generation(
    body: RollbackBody,
    x_tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Reactivate a prior generation (blue/green rollback window)."""
    service = _service(get_app_context())
    try:
        result = await service.rollback_generation(x_tenant_id, body.source_id, body.generation)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return result.model_dump(mode="json")


@router.delete("/reference-docs/sources/{source_id}")
async def delete_reference_source(
    source_id: str,
    x_tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Hard-delete one source's chunks + generations."""
    service = _service(get_app_context())
    removed = await service.purge_source(x_tenant_id, source_id)
    return {"source_id": source_id, "status": "DELETED", "rows_removed": removed}
