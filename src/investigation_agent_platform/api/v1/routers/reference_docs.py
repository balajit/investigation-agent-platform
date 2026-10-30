# src/investigation_agent_platform/api/v1/routers/reference_docs.py
"""Reference-document status/reindex/search endpoints (Part 11.6).

Read paths serve platform-owned retrieval; reindex dispatches the
indexing-queue workflow as a background job (idempotent). Deletion purges
one source's chunks + generations.

Serialization (OpenAPI spec 1.0): `response_model_exclude_unset=True`
universally — explicit nulls (e.g. `next_cursor: null`) are preserved because
constructors pass them explicitly, while never-provided optionals stay absent.
"""

import hashlib
import json
import logging
from datetime import timedelta
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, Query, status

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.errors import ApiError, sanitize_extra
from investigation_agent_platform.api.tenant import require_principal, require_tenant
from investigation_agent_platform.api.v1.schemas.envelopes import CursorEnvelope, ItemsEnvelope
from investigation_agent_platform.api.v1.schemas.errors import COMMON_ERROR_RESPONSES
from investigation_agent_platform.api.v1.schemas.reference_docs import (
    IndexStatusView,
    ReferenceSearchHitView,
    ReferenceSourceView,
    ReindexAck,
    ReindexBody,
    RollbackBody,
    SourceDeleteAck,
)
from investigation_agent_platform.api.validation import NO_NUL_PATTERN
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


# Router-local input models moved to api.v1.schemas.reference_docs (TO_DO-018);
# re-exported here so existing imports keep working.
__all__ = ["ReindexBody", "RollbackBody", "router"]


def _service(ctx: Any) -> ReferenceDocumentService:
    chunk_repo = getattr(ctx, "reference_repo", None)
    if chunk_repo is None:
        raise ApiError(
            code="REFERENCE_STORE_UNAVAILABLE",
            http_status=503,
            message="Reference store is unavailable",
        )
    return ReferenceDocumentService(
        chunk_repo=chunk_repo,
        profile_repo=getattr(ctx, "profile_repo", None),
        embedder=None,
    )


@router.get(
    "/reference-docs/sources",
    response_model=ItemsEnvelope[ReferenceSourceView],
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def list_reference_sources(
    x_tenant_id: str = Depends(require_tenant),
    application_id: str | None = Query(default=None, max_length=128, pattern=NO_NUL_PATTERN),
) -> ItemsEnvelope[ReferenceSourceView]:
    """Configured reference sources with stable ids."""
    service = _service(get_app_context())
    rows = await service.list_sources(x_tenant_id, application_id)
    return ItemsEnvelope[ReferenceSourceView](
        items=[ReferenceSourceView.model_validate(r) for r in rows]
    )


@router.get(
    "/reference-docs/search",
    response_model=CursorEnvelope[ReferenceSearchHitView],
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def search_reference_docs(
    x_tenant_id: str = Depends(require_tenant),
    q: str = Query(..., min_length=1, max_length=2000, pattern=NO_NUL_PATTERN),
    application_id: str | None = Query(default=None, max_length=128, pattern=NO_NUL_PATTERN),
    kinds: str | None = Query(
        default=None,
        max_length=256,
        pattern=NO_NUL_PATTERN,
        description="Comma-separated source kinds.",
    ),
    cursor: str | None = Query(default=None, max_length=4096, pattern=NO_NUL_PATTERN),
    limit: int = Query(default=50, ge=1, le=200),
) -> CursorEnvelope[ReferenceSearchHitView]:
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
        raise ApiError(code="REFERENCE_VALIDATION", http_status=400, message=str(exc)) from exc
    return CursorEnvelope[ReferenceSearchHitView](
        items=[ReferenceSearchHitView.model_validate(h.model_dump(mode="json")) for h in hits],
        next_cursor=next_cursor,
        has_more=next_cursor is not None,
    )


@router.get(
    "/reference-docs/status",
    response_model=IndexStatusView,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def reference_docs_status(
    x_tenant_id: str = Depends(require_tenant),
    source_id: str = Query(..., min_length=1, max_length=256, pattern=NO_NUL_PATTERN),
    application_id: str | None = Query(default=None, max_length=128, pattern=NO_NUL_PATTERN),
) -> IndexStatusView:
    """Current generation status for one source."""
    service = _service(get_app_context())
    try:
        result = await service.index_status(x_tenant_id, source_id)
    except ValueError as exc:
        raise ApiError(code="REFERENCE_NOT_FOUND", http_status=404, message=str(exc)) from exc
    _ = application_id
    return IndexStatusView.model_validate(result.model_dump(mode="json"))


@router.post(
    "/reference-docs/reindex",
    response_model=ReindexAck,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reindex_reference_docs(
    body: ReindexBody,
    x_tenant_id: str = Depends(require_tenant),
    principal_id: str = Depends(require_principal),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
) -> ReindexAck:
    """Start a source reindex as a background job on the indexing queue."""
    if not x_idempotency_key:
        raise ApiError(
            code="REINDEX_KEY_REQUIRED",
            http_status=400,
            message="X-Idempotency-Key header is required",
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
        raise ApiError(code="IDEMPOTENCY_CONFLICT", http_status=409, message=str(exc)) from exc
    except IdempotencyInProgressError as exc:
        raise ApiError(code="IDEMPOTENCY_CONFLICT", http_status=409, message=str(exc)) from exc
    if not reserved:
        logger.info("Replaying cached reference reindex for key=%s", x_idempotency_key)
        return ReindexAck.model_validate(cached or {})

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
        raise ApiError(code="CONCURRENCY_CONFLICT", http_status=409, message=str(exc)) from exc

    temporal_client = getattr(ctx, "temporal_client", None)
    if temporal_client is None:
        raise ApiError(
            code="TEMPORAL_UNAVAILABLE",
            http_status=503,
            message="Workflow execution engine is unavailable; reindex was not started",
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
        logger.exception(
            "Failed to start reindex workflow",
            extra=sanitize_extra({"job_id": str(job.id), "event": "reindex.dispatch_failed"}),
        )
        raise ApiError(
            code="REINDEX_DISPATCH_FAILED",
            http_status=502,
            message="Failed to start reindex workflow; reindex was not started",
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
        raise ApiError(code="CONCURRENCY_CONFLICT", http_status=409, message=str(exc)) from exc

    from investigation_agent_platform.infrastructure.messaging.job_fanout import (
        maybe_publish_job_progress,
    )

    await maybe_publish_job_progress(ctx, x_tenant_id, queued if current is not None else job)
    response_payload = ReindexAck(
        job_id=str(job.id),
        workflow_id=f"wf-reindex-{job.id}",
        status=BackgroundJobStatus.QUEUED.value,
    )
    await ctx.idempotency_store.complete(
        x_tenant_id,
        x_idempotency_key,
        response_payload.model_dump(mode="json"),
        operation="reference-docs-reindex",
    )
    return response_payload


@router.post(
    "/reference-docs/rollback",
    response_model=IndexStatusView,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def rollback_reference_generation(
    body: RollbackBody,
    x_tenant_id: str = Depends(require_tenant),
) -> IndexStatusView:
    """Reactivate a prior generation (blue/green rollback window).

    NOTE: the service returns the refreshed IndexStatus (not the generation
    row) — the response model matches that wire shape.
    """
    service = _service(get_app_context())
    try:
        result = await service.rollback_generation(x_tenant_id, body.source_id, body.generation)
    except ValueError as exc:
        raise ApiError(code="REFERENCE_NOT_FOUND", http_status=404, message=str(exc)) from exc
    return IndexStatusView.model_validate(result.model_dump(mode="json"))


@router.delete(
    "/reference-docs/sources/{source_id}",
    response_model=SourceDeleteAck,
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def delete_reference_source(
    source_id: str,
    x_tenant_id: str = Depends(require_tenant),
) -> SourceDeleteAck:
    """Hard-delete one source's chunks + generations."""
    service = _service(get_app_context())
    removed = await service.purge_source(x_tenant_id, source_id)
    return SourceDeleteAck(source_id=source_id, status="DELETED", rows_removed=removed)
