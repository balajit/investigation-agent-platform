# src/investigation_agent_platform/infrastructure/security/profile_authorizer.py
"""Profile-grounded action authorization and capability registry.

This is the default production ``ActionAuthorizerPort`` /
``CapabilityRegistryPort`` implementation. Authorization decisions are
derived from the tenant-owned ``ApplicationProfile`` (enabled evidence
types, configured code/state/observability providers) rather than an
unconditional allow — an action is denied whenever the profile does not
explicitly enable the evidence class it requires, or the profile/tenant
cannot be resolved at all (fail closed).
"""

from __future__ import annotations

import logging
from typing import Any

from investigation_agent_platform.domain.investigation.models import ActionType
from investigation_agent_platform.ports.persistence.repositories import ApplicationProfileRepository

logger = logging.getLogger(__name__)

# Actions that only manipulate in-process reasoning state and never reach an
# external evidence provider or action-execution surface.
_INTERNAL_ACTION_TYPES = {
    ActionType.FORMULATE_HYPOTHESIS,
    ActionType.VERIFY_HYPOTHESIS,
    ActionType.CONCLUDE,
}

# Evidence-producing actions must be backed by an explicitly enabled
# evidence type on the tenant's application profile.
_ACTION_REQUIRED_EVIDENCE_TYPE: dict[ActionType, str] = {
    ActionType.SEARCH_LOGS: "LOG",
    ActionType.QUERY_STATE: "DATABASE_STATE",
    ActionType.GET_CODE: "COMMIT_HISTORY",
    ActionType.CORRELATE: "TRACE",
}


class ProfileBasedActionAuthorizer:
    """Authorizes actions against the tenant's ``ApplicationProfile``."""

    def __init__(self, profile_repo: ApplicationProfileRepository) -> None:
        self._profile_repo = profile_repo

    async def authorize_action(
        self, tenant_id: str, action_type: str, target_resource: str, context: dict[str, Any]
    ) -> bool:
        try:
            action = ActionType(action_type)
        except ValueError:
            logger.warning("Denying unknown action_type", extra={"action_type": action_type})
            return False

        if action in _INTERNAL_ACTION_TYPES:
            return True

        application_id = context.get("application_id")
        if not application_id:
            logger.warning(
                "Denying action: no application_id in authorization context",
                extra={"tenant_id": tenant_id, "action_type": action_type},
            )
            return False

        profile = await self._profile_repo.get_by_application_id(tenant_id, str(application_id))
        if profile is None:
            logger.warning(
                "Denying action: application profile not found",
                extra={"tenant_id": tenant_id, "application_id": application_id},
            )
            return False

        required_type = _ACTION_REQUIRED_EVIDENCE_TYPE.get(action)
        if required_type is None:
            # Unrecognized-but-valid action type with no explicit policy: deny by default.
            return False

        enabled_types = set(profile.investigation_configuration.enabled_evidence_types)
        return required_type in enabled_types


class ProfileBasedCapabilityRegistry:
    """Capability registry mirroring the same profile-driven policy.

    A capability is enabled only if it maps to an evidence type/action the
    tenant's profile explicitly enables (or is an internal reasoning action).
    """

    def __init__(self, profile_repo: ApplicationProfileRepository) -> None:
        self._profile_repo = profile_repo

    async def is_capability_enabled(self, tenant_id: str, capability_name: str) -> bool:
        try:
            action = ActionType(capability_name)
        except ValueError:
            return False
        return action in _INTERNAL_ACTION_TYPES or action in _ACTION_REQUIRED_EVIDENCE_TYPE

    async def list_allowed_capabilities(self, tenant_id: str) -> list[str]:
        return [a.value for a in ActionType]
