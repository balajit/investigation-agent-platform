"""Evidence router (Part 4, section 4.1). Read/list paths over normalized evidence."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.tenant import require_tenant

router = APIRouter(tags=["evidence"])


@router.get("/investigations/{investigation_id}/evidence")
async def list_evidence(
    investigation_id: str,
    x_tenant_id: str = Depends(require_tenant),
    limit: int = Query(default=50, ge=1, le=1000),
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

    items = await ctx.evidence_repo.find_by_investigation_id(x_tenant_id, investigation_uuid)
    return {"items": [e.model_dump(mode="json") for e in items[:limit]], "total": len(items)}


@router.get("/investigations/{investigation_id}/evidence/{evidence_id}")
async def get_evidence(
    investigation_id: str,
    evidence_id: str,
    x_tenant_id: str = Depends(require_tenant),
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
    evidence = await ctx.evidence_repo.get_by_id(x_tenant_id, UUID(evidence_id))
    if not evidence:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evidence not found")
    return {"evidence": evidence.model_dump(mode="json")}
