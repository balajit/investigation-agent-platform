"""Investigation events router (Part 4, section 4.1)."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.tenant import require_tenant

router = APIRouter(tags=["events"])


@router.post("/investigations/{investigation_id}/pause", status_code=status.HTTP_202_ACCEPTED)
async def pause_investigation(
    investigation_id: str, x_tenant_id: str = Depends(require_tenant)
) -> dict[str, Any]:
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid investigation id") from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investigation not found")
    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant authorization mismatch")
    return {"investigation_id": investigation_id, "status": investigation.status.value}


@router.post("/investigations/{investigation_id}/resume", status_code=status.HTTP_202_ACCEPTED)
async def resume_investigation(
    investigation_id: str, x_tenant_id: str = Depends(require_tenant)
) -> dict[str, Any]:
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid investigation id") from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investigation not found")
    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant authorization mismatch")
    return {"investigation_id": investigation_id, "status": investigation.status.value}
