"""Finding cluster taxonomy + on-demand clustering runs (Part 11.7)."""

import logging
from datetime import timedelta as _timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.tenant import require_principal, require_tenant
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

router = APIRouter(tags=["clusters"])

FINDING_CLUSTERING_JOB_KIND = "finding_clustering"
FINDING_CLUSTERING_WORKFLOW = "FindingClusteringWorkflow"


class RunClustersBody(BaseModel):
    limit: int = Field(default=500, ge=1, le=2000)
    batch_size: int = Field(default=25, ge=1, le=100)


@router.get("/clusters")
async def list_clusters(
    x_tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Lists the tenant's current cluster taxonomy (latest revision)."""
    ctx = get_app_context()
    cluster_repo = getattr(ctx, "finding_cluster_repo", None)
    if cluster_repo is None:
        return {"items": [], "total": 0}
    taxonomy = await cluster_repo.load_taxonomy(x_tenant_id)
    return {
        "items": [cluster.model_dump(mode="json") for cluster in taxonomy],
        "total": len(taxonomy),
    }


@router.get("/clusters/{cluster_id}")
async def get_cluster(
    cluster_id: str,
    x_tenant_id: str = Depends(require_tenant),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """Cluster detail with paginated member findings (Part 11.7)."""
    ctx = get_app_context()
    try:
        cluster_uuid = UUID(cluster_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid cluster id format"
        ) from exc
    cluster_repo = getattr(ctx, "finding_cluster_repo", None)
    if cluster_repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cluster store is unavailable",
        )
    taxonomy = await cluster_repo.load_taxonomy(x_tenant_id)
    cluster = next((c for c in taxonomy if c.id == cluster_uuid), None)
    if cluster is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cluster not found")
    assignments, total = await cluster_repo.cluster_assignments(
        x_tenant_id, cluster_uuid, limit=limit, offset=offset
    )
    finding_ids = [assignment.finding_id for assignment in assignments]
    findings = (
        await ctx.finding_repo.get_findings_by_ids(x_tenant_id, finding_ids) if finding_ids else []
    )
    return {
        "cluster": cluster.model_dump(mode="json"),
        "assignments": [a.model_dump(mode="json") for a in assignments],
        "findings": [finding.model_dump(mode="json") for finding in findings],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.post("/clusters/run", status_code=status.HTTP_202_ACCEPTED)
async def run_clustering(
    body: RunClustersBody,
    x_tenant_id: str = Depends(require_tenant),
    principal_id: str = Depends(require_principal),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
) -> dict[str, Any]:
    """Trigger one incremental clustering run as a background job (Part 11.7).

    Creates the BackgroundJob row, then starts FindingClusteringWorkflow on
    the analytics queue. Requires X-Idempotency-Key: same key + same payload
    replays the original response; same key + different payload is a 409.
    """
    import hashlib
    import json

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
            operation="clusters-run",
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IdempotencyInProgressError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not reserved:
        logger.info("Replaying cached clusters-run response for key=%s", x_idempotency_key)
        return cached or {}

    job = BackgroundJob(
        id=uuid4(),
        tenant_id=x_tenant_id,
        kind=FINDING_CLUSTERING_JOB_KIND,
        created_by=principal_id,
        input_ref=canonical[:1024],
    )
    try:
        await ctx.background_job_repo.create(job)
    except ConcurrencyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    temporal_client = getattr(ctx, "temporal_client", None)
    if temporal_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Workflow execution engine is unavailable; clustering was not started",
        )
    try:
        from investigation_agent_platform.application.worker.workflows import (
            FindingClusteringInput,
        )

        wf_input = FindingClusteringInput(
            tenant_id=x_tenant_id,
            job_id=str(job.id),
            limit=body.limit,
            batch_size=body.batch_size,
        )
        await temporal_client.start_workflow(
            FINDING_CLUSTERING_WORKFLOW,
            wf_input,
            id=f"wf-clustering-{job.id}",
            task_queue=getattr(
                getattr(ctx, "temporal_config", None),
                "analytics_task_queue",
                "analytics-tasks",
            ),
            execution_timeout=_timedelta(seconds=7200),
            run_timeout=_timedelta(seconds=7200),
        )
    except Exception as exc:
        logger.error(
            "Failed to start clustering workflow",
            extra={"error": str(exc), "job_id": str(job.id)},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to start clustering workflow; job was not started",
        ) from exc

    try:
        current = await ctx.background_job_repo.get_by_id(x_tenant_id, job.id)
        if current is not None:
            queued = current.model_copy(
                update={
                    "status": BackgroundJobStatus.QUEUED,
                    "workflow_id": f"wf-clustering-{job.id}",
                    "version": current.version + 1,
                }
            )
            await ctx.background_job_repo.save(x_tenant_id, queued, current.version)
    except ConcurrencyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    response_payload: dict[str, Any] = {
        "job_id": str(job.id),
        "workflow_id": f"wf-clustering-{job.id}",
        "status": BackgroundJobStatus.QUEUED.value,
    }
    await ctx.idempotency_store.complete(
        x_tenant_id, x_idempotency_key, response_payload, operation="clusters-run"
    )
    return response_payload
