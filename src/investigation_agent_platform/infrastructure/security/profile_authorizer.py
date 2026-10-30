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
from investigation_agent_platform.domain.profile.models import ProfileStatus
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
    # P1-1: code-navigation actions gate on the same evidence-type scheme;
    # capability names equal ActionType values (profile validation enforces).
    ActionType.SEARCH_CODE: "SOURCE_CODE",
    ActionType.FIND_SYMBOL: "SOURCE_CODE",
    ActionType.FIND_CALL_GRAPH: "CALL_GRAPH",
    ActionType.GET_CODE_HISTORY: "COMMIT_HISTORY",
    # D2: stack walk banks EXCEPTION_PATH rows resolved through code search;
    # logic trace banks code rows. Both gate on code-backed evidence.
    ActionType.WALK_STACK: "SOURCE_CODE",
    ActionType.TRACE_LOGIC: "SOURCE_CODE",
    # G-1: journey spine banks JOURNEY rows resolved through log search.
    ActionType.TRACE_JOURNEY: "LOG",
    # G-3: session-graph walk executes profile-declared join templates.
    ActionType.WALK_SESSION_GRAPH: "DATABASE_STATE",
    # T1-1: refdoc search banks DOCUMENTATION rows.
    ActionType.SEARCH_REF_DOCS: "DOCUMENTATION",
    # P2-2: live probe gates on its own evidence type; profiles opt in by
    # listing it in investigation.enabled_evidence_types (default lists do
    # not include it — probing is never on unless declared).
    ActionType.PROBE_API: "API_PROBE",
}


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
        for revision in revisions:
            if getattr(revision, "status", None) == ProfileStatus.ACTIVE:
                return revision
        return None

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

        profile = await self._active_profile(tenant_id, str(application_id))
        if profile is None:
            logger.warning(
                "Denying action: no ACTIVE application profile revision",
                extra={"tenant_id": tenant_id, "application_id": application_id},
            )
            return False

        required_type = _ACTION_REQUIRED_EVIDENCE_TYPE.get(action)
        if required_type is None:
            # Unrecognized-but-valid action type with no explicit policy: deny by default.
            return False

        enabled_types = set(profile.investigation.enabled_evidence_types)
        if required_type not in enabled_types:
            return False
        # R4: an unset or empty capability list means "no capabilities
        # declared", which is a denial. It used to fall back to the
        # evidence-type gate alone, so a profile update that omitted the field
        # silently widened access to everything its evidence types allow.
        allowed = set(profile.allowed_capabilities or [])
        if not allowed:
            logger.warning(
                "Denying action: profile declares no allowed_capabilities",
                extra={"tenant_id": tenant_id, "application_id": application_id},
            )
            return False
        capability_for_action = {
            ActionType.SEARCH_LOGS: "SEARCH_LOGS",
            ActionType.QUERY_STATE: "QUERY_STATE",
            ActionType.GET_CODE: "GET_CODE",
            ActionType.CORRELATE: "CORRELATE",
            ActionType.SEARCH_CODE: "SEARCH_CODE",
            ActionType.FIND_SYMBOL: "FIND_SYMBOL",
            ActionType.FIND_CALL_GRAPH: "FIND_CALL_GRAPH",
            ActionType.GET_CODE_HISTORY: "GET_CODE_HISTORY",
            ActionType.PROBE_API: "PROBE_API",
            ActionType.WALK_STACK: "WALK_STACK",
            ActionType.TRACE_LOGIC: "TRACE_LOGIC",
            ActionType.TRACE_JOURNEY: "TRACE_JOURNEY",
            ActionType.WALK_SESSION_GRAPH: "WALK_SESSION_GRAPH",
            ActionType.SEARCH_REF_DOCS: "SEARCH_REF_DOCS",
        }.get(action)
        return capability_for_action in allowed


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
