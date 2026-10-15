"""Capability discovery router (Part 11.1).

Authenticated ``GET /capabilities`` (mounted under ``/api/v1``) reporting
capability ids, contract versions, availability, modes, and safe limits —
never endpoints, config values, or secrets.
"""

import logging

from fastapi import APIRouter, Depends

from investigation_agent_platform.api.tenant import require_tenant
from investigation_agent_platform.api.v1.schemas.capabilities import CapabilityView
from investigation_agent_platform.api.v1.schemas.envelopes import ListEnvelope
from investigation_agent_platform.api.v1.schemas.errors import COMMON_ERROR_RESPONSES
from investigation_agent_platform.application.extensions.capabilities import list_capabilities
from investigation_agent_platform.application.extensions.registries import (
    build_default_registries,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["capabilities"])


@router.get(
    "/capabilities",
    response_model=ListEnvelope[CapabilityView],
    response_model_by_alias=True,
    response_model_exclude_unset=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def list_capabilities_endpoint(
    x_tenant_id: str = Depends(require_tenant),
) -> ListEnvelope[CapabilityView]:
    """Lists platform capabilities visible to the authenticated tenant."""
    _ = x_tenant_id  # authenticated scope; entries are deployment-wide metadata
    items = list_capabilities(build_default_registries())
    return ListEnvelope[CapabilityView].model_validate({"items": items, "total": len(items)})
