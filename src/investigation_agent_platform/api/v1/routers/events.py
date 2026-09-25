"""Investigation events router (Part 4, section 4.1).

Pause/resume issue real Temporal signals against the running workflow and
persist a durable audit transition record — they no longer just echo back
the investigation's current status without acting (F-009/F-010).
"""

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.tenant import require_principal, require_tenant
from investigation_agent_platform.domain.investigation.models import InvestigationStatus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["events"])

_TERMINAL_STATUSES = {
    InvestigationStatus.COMPLETED,
    InvestigationStatus.FAILED,
    InvestigationStatus.CANCELLED,
}


async def _signal_workflow(ctx: Any, investigation_id: str, signal_name: str) -> None:
    """Send a control signal to the running Temporal workflow.

    Raises ``HTTPException`` (503/404/502) rather than silently succeeding —
    a pause/resume call must never report success unless the signal was
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
        await handle.signal(signal_name)
    except Exception as exc:
        # Distinguish "workflow not found/already terminal" from generic
        # infrastructure failure where possible via message inspection;
        # either way this must not be reported as a successful pause/resume.
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

    await _signal_workflow(ctx, investigation_id, "pause")
    await ctx.transition_repo.record_transition(
        tenant_id=x_tenant_id,
        investigation_id=investigation_uuid,
        from_state=investigation.status.value,
        to_state="PAUSE_REQUESTED",
        reason=f"pause requested by {principal_id}",
    )
    logger.info(
        "Pause signal delivered",
        extra={"investigation_id": investigation_id, "tenant_id": x_tenant_id},
    )
    return {"investigation_id": investigation_id, "status": "PAUSE_REQUESTED"}


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
    await ctx.transition_repo.record_transition(
        tenant_id=x_tenant_id,
        investigation_id=investigation_uuid,
        from_state=investigation.status.value,
        to_state="RESUME_REQUESTED",
        reason=f"resume requested by {principal_id}",
    )
    logger.info(
        "Resume signal delivered",
        extra={"investigation_id": investigation_id, "tenant_id": x_tenant_id},
    )
    return {"investigation_id": investigation_id, "status": "RESUME_REQUESTED"}
