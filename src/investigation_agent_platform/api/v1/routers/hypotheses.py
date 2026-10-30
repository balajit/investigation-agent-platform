"""Hypotheses router (Part 4, section 4.1)."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.errors import ApiError
from investigation_agent_platform.api.tenant import require_tenant
from investigation_agent_platform.api.v1.schemas.envelopes import ListEnvelope
from investigation_agent_platform.api.v1.schemas.errors import COMMON_ERROR_RESPONSES
from investigation_agent_platform.domain.hypothesis.models import Hypothesis

logger = logging.getLogger(__name__)

router = APIRouter(tags=["hypotheses"])


@router.get(
    "/investigations/{investigation_id}/hypotheses",
    response_model=ListEnvelope[Hypothesis],
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def list_hypotheses(
    investigation_id: str,
    x_tenant_id: str = Depends(require_tenant),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> ListEnvelope[Hypothesis]:
    """Retrieves paginated hypotheses for an investigation scoped to the authorized tenant."""
    ctx = get_app_context()
    try:
        investigation_uuid = UUID(investigation_id)
    except ValueError as exc:
        raise ApiError(
            code="INVALID_INVESTIGATION_ID",
            http_status=status.HTTP_400_BAD_REQUEST,
            message="Invalid investigation id format",
        ) from exc

    investigation = await ctx.get_investigation_service().execute(x_tenant_id, investigation_uuid)
    if not investigation:
        raise ApiError(
            code="INVESTIGATION_NOT_FOUND",
            http_status=status.HTTP_404_NOT_FOUND,
            message="Investigation not found",
        )

    if getattr(investigation, "tenant_id", x_tenant_id) != x_tenant_id:
        raise ApiError(
            code="TENANT_MISMATCH",
            http_status=status.HTTP_403_FORBIDDEN,
            message="Tenant authorization mismatch",
        )

    hypotheses, total = await ctx.hypothesis_repo.find_by_investigation_and_tenant(
        investigation_id=investigation_uuid,
        tenant_id=x_tenant_id,
        offset=offset,
        limit=limit,
    )
    return ListEnvelope[Hypothesis].model_validate(
        {
            "items": [h.model_dump(mode="json") for h in hypotheses],
            "total": total,
            "offset": offset,
            "limit": limit,
        }
    )
