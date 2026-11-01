# src/investigation_agent_platform/application/knowledge/sharing.py
"""Cross-tenant sharing opt-in gate (R1-D1/D5)."""

from __future__ import annotations

from typing import Any


async def sharing_opted_in(profile_repo: Any, tenant_id: str, application_id: str | None) -> bool:
    """True only when the newest ACTIVE profile revision explicitly opts in. Any fault -> False."""
    if profile_repo is None or not application_id:
        return False
    try:
        from investigation_agent_platform.domain.profile.capability_policy import (
            newest_active_revision,
        )

        revisions = await profile_repo.list_revisions(tenant_id, application_id)
        profile = newest_active_revision(revisions)
        knowledge = getattr(profile, "knowledge", None) if profile is not None else None
        return bool(getattr(knowledge, "enable_cross_tenant_fingerprints", False) is True)
    except Exception:
        return False
