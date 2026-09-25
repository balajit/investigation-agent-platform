"""Hypotheses router (Part 4, section 4.1)."""

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.tenant import require_tenant

logger = logging.getLogger(__name__)

router = APIRouter(tags=["hypotheses"])


@router.get("/investigations/{investigation_id}/hypotheses")
async def list_hypotheses(
    investigation_id: str,
    x_tenant_id: str = Depends(require_tenant),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """Retrieves paginated hypotheses for an investigation scoped to the authorized tenant."""
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

    hypotheses, total = await ctx.hypothesis_repo.find_by_investigation_and_tenant(
        investigation_id=investigation_uuid,
        tenant_id=x_tenant_id,
        offset=offset,
        limit=limit,
    )
    return {
        "items": [h.model_dump(mode="json") for h in hypotheses],
        "total": total,
        "offset": offset,
        "limit": limit,
    }
