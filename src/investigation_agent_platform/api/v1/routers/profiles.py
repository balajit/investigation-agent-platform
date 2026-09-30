"""Application profiles router (Part 4, section 4.1). Exposes sanitized profile configurations."""

import logging
from typing import Any

from fastapi import APIRouter, Depends

from investigation_agent_platform.api.dependencies import get_app_context
from investigation_agent_platform.api.tenant import require_tenant

logger = logging.getLogger(__name__)

router = APIRouter(tags=["profiles"])


def _sanitize_profile(profile_dict: dict[str, Any]) -> dict[str, Any]:
    """Strips internal database query internals from the API response.

    Part 11.2 fix: the profile dict must be dumped with ``by_alias=True``
    (matching ``StateProfile.state_configuration``'s ``alias="state"`` and
    ``query_templates``'s ``alias="queryTemplates"``) — without aliasing this
    function's key lookups never matched anything and the strip was silently
    a no-op. ``connectionUrl`` is defensive: no current field carries that
    name, but stripping it costs nothing if one is added later.
    """
    sanitized = profile_dict.copy()
    if "state" in sanitized and isinstance(sanitized["state"], dict):
        state_copy = sanitized["state"].copy()
        state_copy.pop("queryTemplates", None)
        state_copy.pop("connectionUrl", None)
        sanitized["state"] = state_copy
    return sanitized


@router.get("/profiles")
async def list_profiles(
    x_tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Lists application profiles, sanitizing internal database and infrastructure details."""
    ctx = get_app_context()
    profiles = await ctx.profile_repo.list(x_tenant_id)
    sanitized_items = [
        _sanitize_profile(p.model_dump(mode="json", by_alias=True)) for p in profiles
    ]
    return {"items": sanitized_items, "total": len(sanitized_items)}
