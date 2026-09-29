# src/investigation_agent_platform/api/v1/routers/events.py
"""Investigation events router (Part 4, section 4.1).

Pause/resume issue real Temporal signals against the running workflow and
persist a durable audit transition record — they no longer just echo back
the investigation's current status without acting (F-009/F-010).
"""

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from investigation_agent_platform.api.dependencies import (
    WorkflowStatusEvent,
    get_app_context,
)
from investigation_agent_platform.api.tenant import require_principal, require_tenant
from investigation_agent_platform.domain.common.exceptions import (
    ConcurrencyError,
    InvalidLifecycleTransitionException,
)
from investigation_agent_platform.domain.investigation.models import ActorType, InvestigationStatus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

_TERMINAL_STATUSES = {
    InvestigationStatus.COMPLETED,
    InvestigationStatus.FAILED,
    InvestigationStatus.CANCELLED,
}


async def _signal_workflow(
    ctx: Any, investigation_id: str, signal_name: str, payload: Any = None
) -> None:
    """Send a control signal to the running Temporal workflow.

    Raises ``HTTPException`` (503/404/502) rather than silently succeeding —
    a pause/resume/approval call must never report success unless the signal was
    actually delivered to Temporal.
    """
    temporal_client = getattr(ctx, "temporal_client", None)
    if temporal_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Workflow execution engine is unavailable; cannot signal investigation",
        )
    workflow_id = f"wf-investigation-{investigation_id}"
    try:
        handle = temporal_client.get_workflow_handle(workflow_id)
        if payload is not None:
            await handle.signal(signal_name, payload)
        else:
            await handle.signal(signal_name)
    except Exception as exc:
        # Distinguish "workflow not found/already terminal" from generic
        # infrastructure failure where possible via message inspection;
        # either way this must not be reported as a successful operation.
        logger.error(
            "Failed to signal workflow",
            extra={"workflow_id": workflow_id, "signal": signal_name, "error": str(exc)},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to deliver '{signal_name}' signal to workflow",
        ) from exc


@router.post("/investigations/{investigation_id}/pause", status_code=status.HTTP_202_ACCEPTED)
async def pause_investigation(
    investigation_id: str,
    x_tenant_id: str = Depends(require_tenant),
    principal_id: str = Depends(require_principal),
) -> dict[str, Any]:
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid investigation id"
        ) from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investigation not found")
    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Tenant authorization mismatch"
        )
    if investigation.status in _TERMINAL_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot pause investigation in terminal status {investigation.status.value}",
        )
    if investigation.status == InvestigationStatus.PAUSED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Investigation is already paused"
        )

    await _signal_workflow(ctx, investigation_id, "pause")
    try:
        paused, _ = investigation.transition_to(
            InvestigationStatus.PAUSED, ActorType.USER, f"pause requested by {principal_id}"
        )
    except InvalidLifecycleTransitionException as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    try:
        await ctx.investigation_repo.save(x_tenant_id, paused, investigation.version)
    except ConcurrencyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await ctx.transition_repo.record_transition(
        tenant_id=x_tenant_id,
        investigation_id=investigation_uuid,
        from_state=investigation.status.value,
        to_state=InvestigationStatus.PAUSED.value,
        reason=f"pause requested by {principal_id}",
    )

    if hasattr(ctx, "outbox_queue"):
        await ctx.outbox_queue.put(
            WorkflowStatusEvent(
                investigation_id=investigation_uuid,
                tenant_id=x_tenant_id,
                new_status=InvestigationStatus.PAUSED.value,
            )
        )

    logger.info(
        "Pause signal delivered",
        extra={"investigation_id": investigation_id, "tenant_id": x_tenant_id},
    )
    return {"investigation_id": investigation_id, "status": InvestigationStatus.PAUSED.value}


@router.post("/investigations/{investigation_id}/resume", status_code=status.HTTP_202_ACCEPTED)
async def resume_investigation(
    investigation_id: str,
    x_tenant_id: str = Depends(require_tenant),
    principal_id: str = Depends(require_principal),
) -> dict[str, Any]:
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid investigation id"
        ) from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investigation not found")
    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Tenant authorization mismatch"
        )
    if investigation.status in _TERMINAL_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot resume investigation in terminal status {investigation.status.value}",
        )

    await _signal_workflow(ctx, investigation_id, "resume")
    if investigation.status == InvestigationStatus.PAUSED:
        try:
            resumed, _ = investigation.transition_to(
                InvestigationStatus.INVESTIGATING,
                ActorType.USER,
                f"resume requested by {principal_id}",
            )
        except InvalidLifecycleTransitionException as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        try:
            await ctx.investigation_repo.save(x_tenant_id, resumed, investigation.version)
        except ConcurrencyError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        to_state = InvestigationStatus.INVESTIGATING.value
    else:
        # Resume on an already-active investigation is a signal-only no-op;
        # the audit records the unchanged status, never a pseudo-status.
        to_state = investigation.status.value
    await ctx.transition_repo.record_transition(
        tenant_id=x_tenant_id,
        investigation_id=investigation_uuid,
        from_state=investigation.status.value,
        to_state=to_state,
        reason=f"resume requested by {principal_id}",
    )

    if hasattr(ctx, "outbox_queue"):
        await ctx.outbox_queue.put(
            WorkflowStatusEvent(
                investigation_id=investigation_uuid,
                tenant_id=x_tenant_id,
                new_status=to_state,
            )
        )

    logger.info(
        "Resume signal delivered",
        extra={"investigation_id": investigation_id, "tenant_id": x_tenant_id},
    )
    return {"investigation_id": investigation_id, "status": to_state}


@router.post("/investigations/{investigation_id}/approve-action", status_code=status.HTTP_200_OK)
async def approve_action(
    investigation_id: str,
    action_id: str,
    approved: bool,
    x_tenant_id: str = Depends(require_tenant),
    principal_id: str = Depends(require_principal),
) -> dict[str, Any]:
    """Signals Temporal workflow to proceed or abort a Human-in-the-Loop restricted action."""
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid investigation id"
        ) from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investigation not found")
    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Tenant authorization mismatch"
        )

    signal_name = "action_approval_response"
    payload = {"action_id": action_id, "approved": approved, "principal_id": principal_id}

    if getattr(ctx, "temporal_client", None) is None:
        logger.error(
            "Cannot signal workflow: Temporal client is not wired",
            extra={"investigation_id": investigation_id},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Workflow execution engine is unavailable; approval signal was not sent",
        )
    await _signal_workflow(ctx, investigation_id, signal_name, payload)

    if hasattr(ctx, "outbox_queue"):
        await ctx.outbox_queue.put(
            WorkflowStatusEvent(
                investigation_id=investigation_uuid,
                tenant_id=x_tenant_id,
                new_status=investigation.status.value,
                execution_result={"action_approval": payload},
            )
        )

    logger.info(
        "Action approval processed",
        extra={
            "investigation_id": investigation_id,
            "action_id": action_id,
            "approved": approved,
            "tenant_id": x_tenant_id,
        },
    )
    return {"investigation_id": investigation_id, "action_id": action_id, "approved": approved}
