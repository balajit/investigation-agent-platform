"""Timeline router (Part 4, section 4.1)."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.errors import ApiError
from investigation_agent_platform.api.tenant import require_tenant
from investigation_agent_platform.api.v1.schemas.envelopes import ListEnvelope
from investigation_agent_platform.api.v1.schemas.errors import COMMON_ERROR_RESPONSES
from investigation_agent_platform.domain.timeline.models import TimelineEvent

logger = logging.getLogger(__name__)

router = APIRouter(tags=["timeline"])


@router.get(
    "/investigations/{investigation_id}/timeline",
    response_model=ListEnvelope[TimelineEvent],
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def list_timeline(
    investigation_id: str,
    x_tenant_id: str = Depends(require_tenant),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
) -> ListEnvelope[TimelineEvent]:
    """Lists timeline events for an investigation scoped to the authorized tenant and paginated."""
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise ApiError(
            code="INVALID_INVESTIGATION_ID", http_status=status.HTTP_400_BAD_REQUEST, message="Invalid investigation id format"
        ) from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise ApiError(
            code="INVESTIGATION_NOT_FOUND", http_status=status.HTTP_404_NOT_FOUND, message="Investigation not found"
        )

    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise ApiError(
            code="TENANT_MISMATCH", http_status=status.HTTP_403_FORBIDDEN, message="Tenant authorization mismatch"
        )

    all_events = await ctx.timeline_repo.find_by_investigation_id(x_tenant_id, investigation_uuid)
    total = len(all_events)
    events = all_events[offset : offset + limit]
    return ListEnvelope[TimelineEvent].model_validate(
        {
            "items": [e.model_dump(mode="json") for e in events],
            "total": total,
            "offset": offset,
            "limit": limit,
        }
    )
