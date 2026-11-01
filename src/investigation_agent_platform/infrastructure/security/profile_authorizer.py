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
from investigation_agent_platform.domain.profile.capability_policy import (
    INTERNAL_ACTIONS,
    CapabilityDecision,
    DenyReason,
    capability_available,
    capability_available_by_name,
    effective_capabilities,
    newest_active_revision,
)
from investigation_agent_platform.ports.persistence.repositories import ApplicationProfileRepository

logger = logging.getLogger(__name__)


class ProfileBasedActionAuthorizer:
    """Authorizes actions against the tenant's ``ApplicationProfile``."""

    def __init__(self, profile_repo: ApplicationProfileRepository) -> None:
        self._profile_repo = profile_repo

    async def _active_profile(self, tenant_id: str, application_id: str) -> Any:
        """Newest ACTIVE revision for the application, or None (R5).

        Authorization must never come from a DRAFT (unreviewed) revision that
        merely has the highest version, nor from DEPRECATED/ARCHIVED ones.
        Revisions arrive newest-first; the first ACTIVE one wins.
        """
        revisions = await self._profile_repo.list_revisions(tenant_id, application_id)
        return newest_active_revision(revisions)

    async def decide(
        self, tenant_id: str, action_type: str, target_resource: str, context: dict[str, Any]
    ) -> CapabilityDecision:
        """Typed authorization decision; ``authorize_action`` is the boolean wrapper."""
        try:
            action = ActionType(action_type)
        except ValueError:
            logger.warning("Denying unknown action_type", extra={"action_type": action_type})
            return CapabilityDecision.deny(DenyReason.UNKNOWN_ACTION)
        # Internal reasoning actions never reach a provider: allowed without
        # any application context, exactly as capability_available decides.
        if action in INTERNAL_ACTIONS:
            return CapabilityDecision.allow()
        application_id = context.get("application_id")
        if not application_id:
            logger.warning(
                "Denying action: no application_id in authorization context",
                extra={"tenant_id": tenant_id, "action_type": action_type},
            )
            return CapabilityDecision.deny(DenyReason.NO_APPLICATION)
        profile = await self._active_profile(tenant_id, str(application_id))
        if profile is None:
            logger.warning(
                "Denying action: no ACTIVE application profile revision",
                extra={"tenant_id": tenant_id, "application_id": application_id},
            )
            return CapabilityDecision.deny(DenyReason.NO_ACTIVE_PROFILE)
        decision = capability_available(profile, action)
        if not decision.allowed and decision.reason == DenyReason.CAPABILITY_NOT_DECLARED:
            if not getattr(profile, "allowed_capabilities", None):
                logger.warning(
                    "Denying action: profile declares no allowed_capabilities",
                    extra={"tenant_id": tenant_id, "application_id": application_id},
                )
        return decision

    async def authorize_action(
        self, tenant_id: str, action_type: str, target_resource: str, context: dict[str, Any]
    ) -> bool:
        return (
            await self.decide(tenant_id, action_type, target_resource, context)
        ).allowed


class ProfileBasedCapabilityRegistry:
    """Capability registry enforcing the same profile-driven policy.

    A capability is enabled only when the tenant's newest ACTIVE profile
    passes both gates (evidence type + declaration). The redundancy with
    the authorizer is deliberate defense in depth: the second reason
    becomes reachable whenever the authorizer is permissive.
    """

    def __init__(self, profile_repo: ApplicationProfileRepository) -> None:
        self._profile_repo = profile_repo

    async def decide(
        self, tenant_id: str, capability_name: str, application_id: str | None
    ) -> CapabilityDecision:
        if not application_id:
            return CapabilityDecision.deny(DenyReason.NO_APPLICATION)
        revisions = await self._profile_repo.list_revisions(tenant_id, application_id)
        profile = newest_active_revision(revisions)
        if profile is None:
            return CapabilityDecision.deny(DenyReason.NO_ACTIVE_PROFILE)
        # allow_platform=True: the registry serves the gateway, which invokes
        # platform operations through its own methods. Model-facing name
        # resolution (ActionType-typed actions) can never name one, and the
        # default False in capability_available_by_name keeps it that way.
        return capability_available_by_name(profile, capability_name, allow_platform=True)

    async def is_capability_enabled(
        self, tenant_id: str, capability_name: str, application_id: str | None = None
    ) -> bool:
        return (await self.decide(tenant_id, capability_name, application_id)).allowed

    async def list_allowed_capabilities(
        self, tenant_id: str, application_id: str | None = None
    ) -> list[str]:
        if not application_id:
            return []
        revisions = await self._profile_repo.list_revisions(tenant_id, application_id)
        profile = newest_active_revision(revisions)
        return list(effective_capabilities(profile))
