# src/investigation_agent_platform/application/security/capability_steering.py
"""Authorization steering snapshot: which actions the loop must not force.

The workflow is deterministic and cannot read profiles, so the reason
activity computes the unavailable-action set where I/O is allowed and
carries it as plain data (R8-D1). The snapshot fails open: an unreadable
profile means "do not filter" — the authoritative gate downstream
(authorizer, registry, gateway, action service) still denies, fail-closed.
"""

from __future__ import annotations

from typing import Any

from investigation_agent_platform.domain.profile.capability_policy import (
    newest_active_revision,
    unavailable_actions,
)


async def steering_snapshot(
    profile_repo: Any, tenant_id: str, application_id: str | None
) -> tuple[tuple[str, ...], bool]:
    """(unavailable_actions, known). known=False on any fault: callers must not filter."""
    if profile_repo is None or not application_id:
        return (), False
    try:
        revisions = await profile_repo.list_revisions(tenant_id, application_id)
    except Exception:
        return (), False
    try:
        return unavailable_actions(newest_active_revision(revisions)), True
    except Exception:
        return (), False
