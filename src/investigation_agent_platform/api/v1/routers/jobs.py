"""Background job status, cancellation, and progress streaming (Part 11.3C)."""

import asyncio
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.tenant import (
    require_tenant,
    resolve_identity_from_headers,
)
from investigation_agent_platform.domain.common.background_job import (
    TERMINAL_JOB_STATUSES,
    BackgroundJobStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["jobs"])

_STREAM_TIMEOUT_SECONDS = 20.0


async def _get_job_or_404(ctx: Any, tenant_id: str, job_id: UUID) -> Any:
    job = await ctx.background_job_repo.get_by_id(tenant_id, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    x_tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Poll one background job (Postgres read model)."""
    ctx = get_app_context()
    try:
        job_uuid = UUID(job_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid job id format"
        ) from exc
    job = await _get_job_or_404(ctx, x_tenant_id, job_uuid)
    return {"job": job.model_dump(mode="json")}


@router.get("/jobs")
async def list_jobs(
    x_tenant_id: str = Depends(require_tenant),
    kind: str | None = Query(default=None, max_length=64),
    status_filter: BackgroundJobStatus | None = Query(  # noqa: B008 - FastAPI Query idiom
        default=None, alias="status"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List background jobs for the tenant (newest first)."""
    ctx = get_app_context()
    jobs, total = await ctx.background_job_repo.list_jobs(
        x_tenant_id, kind=kind, status=status_filter, limit=limit, offset=offset
    )
    return {
        "items": [job.model_dump(mode="json") for job in jobs],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    x_tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Request cancellation (idempotent; terminal jobs return as-is)."""
    ctx = get_app_context()
    try:
        job_uuid = UUID(job_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid job id format"
        ) from exc
    job = await _get_job_or_404(ctx, x_tenant_id, job_uuid)
    if job.status in TERMINAL_JOB_STATUSES:
        return {"job": job.model_dump(mode="json"), "cancelled": False}
    updated = job.model_copy(
        update={"status": BackgroundJobStatus.CANCEL_REQUESTED, "version": job.version + 1}
    )
    await ctx.background_job_repo.save(x_tenant_id, updated, expected_version=job.version)
    # Best-effort Temporal cancellation when a live execution is attached;
    # the worker also observes CANCEL_REQUESTED on its next checkpoint.
    temporal_client = getattr(ctx, "temporal_client", None)
    if temporal_client is not None and job.workflow_id:
        try:
            handle = temporal_client.get_workflow_handle(job.workflow_id)
            await handle.cancel()
        except Exception as exc:
            logger.warning(
                "Temporal job cancel failed; row state still governs",
                extra={"job_id": job_id, "error": str(exc)},
            )
    refreshed = await ctx.background_job_repo.get_by_id(x_tenant_id, job_uuid)
    payload = refreshed if refreshed is not None else updated
    return {"job": payload.model_dump(mode="json"), "cancelled": True}


@router.websocket("/jobs/{job_id}/stream")
async def stream_job(websocket: WebSocket, job_id: str) -> None:
    """Live progress: DB snapshot first, then versioned events, then reconcile.

    Authorization happens before accept; cross-tenant/unknown jobs close with
    4404 without ever subscribing. Reconnect always re-reads the row first.
    Tenant identity resolves from handshake headers via the same verifier the
    REST dependency uses (Request injection is unavailable on websockets).
    """
    try:
        identity = await resolve_identity_from_headers(
            websocket.headers.get("x-tenant-id"),
            websocket.headers.get("authorization"),
        )
    except Exception:
        await websocket.close(code=4401)
        return
    x_tenant_id = identity.tenant_id
    await websocket.accept()
    ctx = get_app_context()
    try:
        job_uuid = UUID(job_id)
    except ValueError:
        await websocket.close(code=4400)
        return
    job = await ctx.background_job_repo.get_by_id(x_tenant_id, job_uuid)
    if job is None:
        await websocket.close(code=4404)
        return
    await websocket.send_json({"type": "snapshot", "job": job.model_dump(mode="json")})
    if job.status in TERMINAL_JOB_STATUSES:
        await websocket.close()
        return
    queue = await ctx.job_hub.subscribe(job_uuid)
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=_STREAM_TIMEOUT_SECONDS)
            except TimeoutError:
                await websocket.send_json({"type": "ping"})
                current = await ctx.background_job_repo.get_by_id(x_tenant_id, job_uuid)
                if current is not None and current.status in TERMINAL_JOB_STATUSES:
                    await websocket.send_json(
                        {"type": "snapshot", "job": current.model_dump(mode="json")}
                    )
                    break
                continue
            await websocket.send_json({"type": "event", "event": event.model_dump(mode="json")})
            if event.status in TERMINAL_JOB_STATUSES:
                break
    except WebSocketDisconnect:
        pass
    finally:
        await ctx.job_hub.unsubscribe(job_uuid, queue)
        try:
            await websocket.close()
        except Exception:
            pass
