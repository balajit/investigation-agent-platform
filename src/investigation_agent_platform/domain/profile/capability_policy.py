# src/investigation_agent_platform/domain/profile/capability_policy.py
"""Single source of truth for 'may this application run this action'.

Pure: no I/O, no clock. Profiles and capabilities are ops-owned; this module
only maps an action to the evidence type and the capability declaration that
the owner-approved authorization model already requires.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from investigation_agent_platform.domain.investigation.models import ActionType
from investigation_agent_platform.domain.profile.models import ProfileStatus


class DenyReason(StrEnum):
    """Typed, closed reasons. The string value is what rides outcomes and limitations."""

    UNKNOWN_ACTION = "unknown_action"
    NO_APPLICATION = "no_application_context"
    NO_ACTIVE_PROFILE = "no_active_profile"
    NO_POLICY_ROW = "no_policy_row"
    EVIDENCE_TYPE_NOT_ENABLED = "evidence_type_not_enabled"
    CAPABILITY_NOT_DECLARED = "capability_not_declared"


@dataclass(frozen=True)
class CapabilityRule:
    """What an action needs. ``None`` means 'not gated on that axis'."""

    evidence_type: str | None
    capability: str | None


@dataclass(frozen=True)
class CapabilityDecision:
    allowed: bool
    reason: DenyReason | None = None

    @staticmethod
    def allow() -> CapabilityDecision:
        return CapabilityDecision(True, None)

    @staticmethod
    def deny(reason: DenyReason) -> CapabilityDecision:
        return CapabilityDecision(False, reason)


# Internal reasoning actions never reach a provider and are never gated.
INTERNAL_ACTIONS: frozenset[ActionType] = frozenset(
    {ActionType.FORMULATE_HYPOTHESIS, ActionType.VERIFY_HYPOTHESIS, ActionType.CONCLUDE}
)

# The MAPPING table (R6). Every external ActionType has exactly one row.
# GET_CODE moves from COMMIT_HISTORY to SOURCE_CODE: it reads source, and a
# SOURCE_CODE-only profile could search code but never fetch it.
ACTION_POLICY: dict[ActionType, CapabilityRule] = {
    ActionType.SEARCH_LOGS: CapabilityRule("LOG", "SEARCH_LOGS"),
    ActionType.QUERY_STATE: CapabilityRule("DATABASE_STATE", "QUERY_STATE"),
    ActionType.GET_CODE: CapabilityRule("SOURCE_CODE", "GET_CODE"),
    ActionType.CORRELATE: CapabilityRule("TRACE", "CORRELATE"),
    ActionType.SEARCH_CODE: CapabilityRule("SOURCE_CODE", "SEARCH_CODE"),
    ActionType.FIND_SYMBOL: CapabilityRule("SOURCE_CODE", "FIND_SYMBOL"),
    ActionType.FIND_CALL_GRAPH: CapabilityRule("CALL_GRAPH", "FIND_CALL_GRAPH"),
    ActionType.GET_CODE_HISTORY: CapabilityRule("COMMIT_HISTORY", "GET_CODE_HISTORY"),
    ActionType.WALK_STACK: CapabilityRule("SOURCE_CODE", "WALK_STACK"),
    ActionType.TRACE_LOGIC: CapabilityRule("SOURCE_CODE", "TRACE_LOGIC"),
    ActionType.TRACE_JOURNEY: CapabilityRule("LOG", "TRACE_JOURNEY"),
    ActionType.WALK_SESSION_GRAPH: CapabilityRule("DATABASE_STATE", "WALK_SESSION_GRAPH"),
    ActionType.SEARCH_REF_DOCS: CapabilityRule("DOCUMENTATION", "SEARCH_REF_DOCS"),
    ActionType.PROBE_API: CapabilityRule("API_PROBE", "PROBE_API"),
}


class PlatformOperation(StrEnum):
    """Gateway-internal operations that are not agent actions (R6).

    They are NOT ActionType members: the model must never be able to propose
    them. They get their own row so the gateway can authorize them without
    inventing an action. PROPOSED mapping; ops confirms the evidence type.
    """

    CODE_TOPOLOGY = "QUERY_CODE_TOPOLOGY"
    DOMAIN_ATTRIBUTION = "RESOLVE_DOMAIN_ATTRIBUTION"


PLATFORM_POLICY: dict[PlatformOperation, CapabilityRule] = {
    PlatformOperation.CODE_TOPOLOGY: CapabilityRule("SOURCE_CODE", None),
    PlatformOperation.DOMAIN_ATTRIBUTION: CapabilityRule("SOURCE_CODE", None),
}


def newest_active_revision(revisions: Iterable[Any]) -> Any | None:
    """The newest ACTIVE revision from a newest-first list (R5), else None."""
    for revision in revisions or []:
        if getattr(revision, "status", None) == ProfileStatus.ACTIVE:
            return revision
    return None


def evidence_gate(profile: Any, rule: CapabilityRule) -> DenyReason | None:
    if rule.evidence_type is None:
        return None
    enabled = getattr(getattr(profile, "investigation", None), "enabled_evidence_types", None)
    try:
        enabled_set = set(enabled or [])
    except TypeError:
        return DenyReason.EVIDENCE_TYPE_NOT_ENABLED
    if not isinstance(enabled, list):
        return DenyReason.EVIDENCE_TYPE_NOT_ENABLED
    return None if rule.evidence_type in enabled_set else DenyReason.EVIDENCE_TYPE_NOT_ENABLED


def capability_gate(profile: Any, rule: CapabilityRule) -> DenyReason | None:
    if rule.capability is None:
        return None
    declared = getattr(profile, "allowed_capabilities", None)
    if not isinstance(declared, list):
        return DenyReason.CAPABILITY_NOT_DECLARED
    return None if rule.capability in set(declared) else DenyReason.CAPABILITY_NOT_DECLARED


def capability_available(profile: Any | None, action: ActionType | str) -> CapabilityDecision:
    """The one decision function. ``profile`` is the already-resolved ACTIVE revision or None.

    Gate order is capability-first: a missing declaration denies with
    ``CAPABILITY_NOT_DECLARED`` even when the evidence type is also off, so
    the R4 guarantee (an undeclared capability list is a denial with its own
    reason) stays reachable. Either gate failing denies fail-closed.
    """
    try:
        act = action if isinstance(action, ActionType) else ActionType(str(action))
    except (ValueError, TypeError):
        return CapabilityDecision.deny(DenyReason.UNKNOWN_ACTION)
    if act in INTERNAL_ACTIONS:
        return CapabilityDecision.allow()
    if profile is None:
        return CapabilityDecision.deny(DenyReason.NO_ACTIVE_PROFILE)
    rule = ACTION_POLICY.get(act)
    if rule is None:
        return CapabilityDecision.deny(DenyReason.NO_POLICY_ROW)
    reason = capability_gate(profile, rule) or evidence_gate(profile, rule)
    return CapabilityDecision.deny(reason) if reason else CapabilityDecision.allow()


def capability_available_by_name(
    profile: Any | None, name: Any, *, allow_platform: bool = False
) -> CapabilityDecision:
    """Decide by string name: an ActionType value, a platform operation, else unknown.

    ``allow_platform`` is False for every model-facing caller: a
    model-proposed ``action_type`` of ``QUERY_CODE_TOPOLOGY`` is denied with
    ``UNKNOWN_ACTION`` even though a platform row exists. Only the gateway
    invokes platform operations, and it reaches them through the registry,
    which passes ``allow_platform=True``. A platform operation can therefore
    never be selected as an action, only invoked by the gateway's own methods.
    """
    text = str(name or "").strip()
    try:
        return capability_available(profile, ActionType(text))
    except ValueError:
        pass
    try:
        operation = PlatformOperation(text)
    except ValueError:
        return CapabilityDecision.deny(DenyReason.UNKNOWN_ACTION)
    if not allow_platform:
        return CapabilityDecision.deny(DenyReason.UNKNOWN_ACTION)
    if profile is None:
        return CapabilityDecision.deny(DenyReason.NO_ACTIVE_PROFILE)
    rule = PLATFORM_POLICY.get(operation)
    if rule is None:
        return CapabilityDecision.deny(DenyReason.NO_POLICY_ROW)
    reason = capability_gate(profile, rule) or evidence_gate(profile, rule)
    return CapabilityDecision.deny(reason) if reason else CapabilityDecision.allow()


def unavailable_actions(profile: Any | None) -> tuple[str, ...]:
    """Sorted names of every external action the profile cannot run (steering input)."""
    return tuple(
        sorted(a.value for a in ACTION_POLICY if not capability_available(profile, a).allowed)
    )


def effective_capabilities(profile: Any | None) -> tuple[str, ...]:
    """What the prompt may offer: external actions that pass BOTH gates."""
    return tuple(
        sorted(a.value for a in ACTION_POLICY if capability_available(profile, a).allowed)
    )


def assert_table_complete(actions: Sequence[ActionType] = tuple(ActionType)) -> list[str]:
    """Contract check used by a unit test: every action is internal or has a row."""
    return [a.value for a in actions if a not in INTERNAL_ACTIONS and a not in ACTION_POLICY]
