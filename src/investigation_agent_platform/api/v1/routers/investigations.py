"""Investigation life-cycle and control-plane router (Part 4, section 4.1).

Read paths read through the investigation repository; write paths dispatch
through the investigation services. The FastAPI dependency provider is wired
via ``api.dependencies`` (in-memory solids by default; swap in the SQLAlchemy
session factory for production).
"""

import hashlib
import json
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.tenant import require_principal, require_tenant
from investigation_agent_platform.domain.common.exceptions import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
)
from investigation_agent_platform.domain.investigation.models import InvestigationRequest

logger = logging.getLogger(__name__)

router = APIRouter(tags=["investigations"])


class CreateInvestigationBody(BaseModel):
    # F-061: edge payload bounds — oversized descriptions/parameter blobs are
    # rejected at the API boundary before they reach services, workflows, or
    # model prompts.
    application_id: str = Field(
        ..., min_length=1, max_length=128, description="Target application profile identifier"
    )
    problem_description: str = Field(
        ...,
        min_length=1,
        max_length=8000,
        description="Description of the observed incident or system anomaly",
    )
    session_id: str | None = Field(
        default=None, max_length=256, description="Optional telemetry session correlation ID"
    )
    priority: str = Field(
        default="NORMAL",
        max_length=16,
        description="Investigation priority level (LOW, NORMAL, HIGH, CRITICAL)",
    )
    requested_by: str = Field(default="api-user", max_length=256)
    parameters: dict[str, Any] = Field(
        default_factory=dict, max_length=50, description="Custom parameters for analysis workflow"
    )

    def to_request(self, requested_by: str) -> InvestigationRequest:
        return InvestigationRequest(
            application_id=self.application_id,
            problem_description=self.problem_description,
            session_id=self.session_id or f"sess_{self.application_id}",
            requested_by=requested_by,
            priority=self.priority,
            parameters=self.parameters,
        )


@router.post("/investigations", status_code=status.HTTP_201_CREATED)
async def create_investigation(
    body: CreateInvestigationBody,
    x_tenant_id: str = Depends(require_tenant),
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    principal_id: str = Depends(require_principal),
) -> dict[str, Any]:
    """Creates a new investigation specification in database with idempotency guarantee.

    Idempotency (F-013/F-014) is enforced via a durable, DB-unique-constraint
    reservation (safe across any number of API replicas — no process-local
    lock is involved) bound to a canonical hash of the request body: reusing
    a key with a different payload is rejected with 409 rather than silently
    replaying the first response.
    """
    ctx = get_app_context()

    if x_idempotency_key:
        canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        request_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        try:
            cached, reserved = await ctx.idempotency_store.reserve_or_get(
                x_tenant_id, x_idempotency_key, request_hash
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except IdempotencyInProgressError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        if not reserved:
            logger.info(
                "Replaying cached investigation creation response for key=%s", x_idempotency_key
            )
            return cached or {}

        service = ctx.create_investigation_service()
        request = body.to_request(requested_by=principal_id)
        investigation = await service.execute(request, tenant_id=x_tenant_id)
        response_payload: dict[str, Any] = {
            "id": str(investigation.id),
            "session_id": investigation.session_id,
            "application_id": investigation.application_id,
            "status": investigation.status.value,
            "tenant_id": x_tenant_id,
            "investigation": investigation.model_dump(mode="json"),
        }
        await ctx.idempotency_store.complete(x_tenant_id, x_idempotency_key, response_payload)
        return response_payload

    service = ctx.create_investigation_service()
    request = body.to_request(requested_by=principal_id)
    investigation = await service.execute(request, tenant_id=x_tenant_id)
    response_payload_direct: dict[str, Any] = {
        "id": str(investigation.id),
        "session_id": investigation.session_id,
        "application_id": investigation.application_id,
        "status": investigation.status.value,
        "tenant_id": x_tenant_id,
        "investigation": investigation.model_dump(mode="json"),
    }
    return response_payload_direct


@router.post("/investigations/{investigation_id}/start", status_code=status.HTTP_202_ACCEPTED)
async def start_investigation_workflow(
    investigation_id: str,
    request: Request,
    x_tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Dispatches and starts the Temporal workflow execution for an existing investigation."""
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid investigation id format"
        ) from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investigation not found")

    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Tenant authorization mismatch"
        )

    correlation_id = getattr(request.state, "correlation_id", None) or request.headers.get(
        "X-Correlation-ID"
    )

    temporal_client = getattr(ctx, "temporal_client", None)
    if temporal_client is None:
        logger.error(
            "Cannot start workflow: Temporal client is not wired",
            extra={"investigation_id": investigation_id, "correlation_id": correlation_id},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Workflow execution engine is unavailable; investigation was not started",
        )

    # Propagate correlation_id to Temporal via workflow memo/headers.
    try:
        from investigation_agent_platform.application.worker.workflows import RunInvestigationInput

        wf_input = RunInvestigationInput(
            application_id=str(investigation.application_id),
            session_id=getattr(investigation, "session_id", None),
            tenant_id=x_tenant_id,
            description=getattr(investigation, "problem_description", None),
            correlation_id=correlation_id,
        )
        memo = (
            {"correlation_id": correlation_id, "tenant_id": x_tenant_id} if correlation_id else None
        )
        headers = {"X-Correlation-ID": correlation_id} if correlation_id else None
        from datetime import timedelta as _timedelta

        await temporal_client.start_workflow(
            "RunInvestigationWorkflow",
            wf_input,
            id=f"wf-investigation-{investigation_id}",
            task_queue=getattr(
                getattr(ctx, "temporal_config", None), "task_queue", "investigation-tasks"
            ),
            memo=memo,
            headers=headers,  # type: ignore[arg-type]
            # F-055: Temporal workflow execution timeout is the outer boundary
            # for investigation wall-clock; application timestamps alone cannot
            # bound provider calls, retries, or worker restarts.
            execution_timeout=_timedelta(seconds=7200),
            run_timeout=_timedelta(seconds=7200),
        )
    except Exception as exc:
        # A failed start must never be reported as success (F-008): the
        # investigation remains in its current (non-running) persisted state
        # and the caller receives an explicit failure.
        logger.error(
            "Failed to start Temporal workflow",
            extra={
                "error": str(exc),
                "correlation_id": correlation_id,
                "investigation_id": investigation_id,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to start workflow execution; investigation was not started",
        ) from exc

    logger.info(
        "Dispatched Temporal workflow start for investigation=%s",
        investigation_id,
        extra={"correlation_id": correlation_id, "tenant_id": x_tenant_id},
    )
    return {
        "investigation_id": str(investigation.id),
        "status": "RUNNING",
        "workflow_id": f"wf-investigation-{investigation_id}",
        "correlation_id": correlation_id,
        "message": "Workflow started successfully",
    }


@router.get("/investigations/{investigation_id}")
async def get_investigation(
    investigation_id: str,
    x_tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Retrieves an investigation record scoped to the authorized tenant context."""
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid investigation id format"
        ) from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investigation not found")

    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Tenant authorization mismatch"
        )

    return {"investigation": investigation.model_dump(mode="json")}


@router.post("/investigations/{investigation_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_investigation(
    investigation_id: str,
    reason: str = Query(default="API cancellation requested", max_length=1000),
    x_tenant_id: str = Depends(require_tenant),
) -> dict[str, str]:
    """Issues cancellation signal to Temporal workflow execution and updates persistence status."""
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid investigation id format"
        ) from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investigation not found")

    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Tenant authorization mismatch"
        )

    await ctx.cancel_investigation_service().execute(x_tenant_id, investigation_uuid, reason=reason)
    return {"investigation_id": investigation_id, "status": "CANCELLING", "reason": reason}
