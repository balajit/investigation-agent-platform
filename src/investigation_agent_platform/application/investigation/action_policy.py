# src/investigation_agent_platform/application/investigation/action_policy.py
"""WP-D2 shadow action policy (frozen, pure, deterministic, side-effect-free).

A declarative mirror of the workflow's action-selection guardrails: forcing
caps, evidence floor, formulation/verification gating, terminal conditions,
and failure recovery. It ranks admissible investigation actions for a
frozen snapshot — it never executes anything, touches I/O, reads the
clock, or feeds a derivation (planner routes acquisition only).

Shadow status: the workflow does not call this module. It exists so a
G6 harness can replay recorded histories through ``decide`` and measure
divergence against what the loop actually did, with zero risk of the
policy altering behavior. All inputs are JSON scalars (Temporal
replay-safe); unknown history entries are tolerated and counted, never
fatal — policy is diagnostics-grade, fail-open on data, fail-closed on
shape (negative counts, non-object snapshots raise).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.investigation.models import ActionType

MAX_ITERATIONS_DEFAULT = 10
FORCING_CAP_DEFAULT = 2
EVIDENCE_FLOOR_MINIMUM = 3

# Cascade order mirrors the workflow (value read > walk > floor >
# formulate > synthesis > reread > callsite); lower priority number runs
# first. Base (non-forced) actions rank below every forcing rule.
_PRIORITY_CONCLUDE_TERMINAL = 0
_PRIORITY_CONCLUDE_VERIFIED = 5
_PRIORITY_FORCE_VALUE_READ = 10
_PRIORITY_FORCE_WALK_STACK = 20
_PRIORITY_FORCE_FLOOR = 30
_PRIORITY_FORCE_FORMULATE = 40
_PRIORITY_FORCE_SYNTHESIS = 50
_PRIORITY_FORCE_REREAD = 60
_PRIORITY_FORCE_CALLSITE = 70
_PRIORITY_GATHERING_BASE = 100
_PRIORITY_FORMULATE_BASE = 110
_PRIORITY_VERIFY_BASE = 120
_PRIORITY_CONCLUDE_BASE = 200
_PRIORITY_FAILED_PENALTY = 1000


class PolicyErrorCode(str, Enum):
    """Stable shadow-policy failure codes (frozen; append-only)."""

    BAD_SNAPSHOT = "BAD_SNAPSHOT"


class PolicyError(ValueError):
    """Fail-closed snapshot rejection carrying a stable code."""

    def __init__(self, code: PolicyErrorCode | str, detail: str = "") -> None:
        self.code = str(getattr(code, "value", code))
        super().__init__(f"{self.code}: {detail}" if detail else self.code)


class HistoryEntry(BaseModel):
    """One recorded action attempt (raw strings; unknown values tolerated)."""

    model_config = ConfigDict(frozen=True)

    action: str = Field(..., max_length=64)
    outcome: str = Field(default="SUCCEEDED", max_length=32)


class PlannerSnapshot(BaseModel):
    """Frozen loop state for one policy evaluation (JSON scalars only)."""

    model_config = ConfigDict(frozen=True)

    iteration: int = Field(default=0, ge=0)
    max_iterations: int = Field(default=MAX_ITERATIONS_DEFAULT, ge=1)
    actions_used: int = Field(default=0, ge=0)
    action_limit: int = Field(default=10, ge=1)
    tokens_used: int = Field(default=0, ge=0)
    token_limit: int = Field(default=0, ge=0, description="0 = unbounded")
    evidence_banked_total: int = Field(default=0, ge=0)
    formulated_count: int = Field(default=0, ge=0)
    unverified_count: int = Field(default=0, ge=0)
    verified_present: bool = Field(default=False)
    reasoning_available: bool = Field(default=True)
    value_target_present: bool = Field(default=False)
    stack_walk_owed: bool = Field(default=False)
    below_evidence_floor: bool = Field(default=False)
    formulate_owed: bool = Field(default=False)
    synthesis_owed: bool = Field(default=False)
    reread_path_present: bool = Field(default=False)
    callsite_owed: bool = Field(default=False)
    history: list[HistoryEntry] = Field(default_factory=list, max_length=500)
    forcing_used: dict[str, int] = Field(default_factory=dict)
    forcing_caps: dict[str, int] = Field(default_factory=dict)
    action_counts: dict[str, int] = Field(default_factory=dict)
    action_caps: dict[str, int] = Field(default_factory=dict)


class RankedAction(BaseModel):
    """One admissible action with its deterministic rank."""

    model_config = ConfigDict(frozen=True)

    action: str = Field(...)
    priority: int = Field(...)
    reason: str = Field(...)
    forced: bool = Field(default=False)


class WithheldAction(BaseModel):
    """An action considered and refused, with cause (observable, not silent)."""

    model_config = ConfigDict(frozen=True)

    action: str = Field(...)
    reason: str = Field(...)


class PolicyDecision(BaseModel):
    """The ranked field plus termination state (deterministic)."""

    model_config = ConfigDict(frozen=True)

    candidates: list[RankedAction] = Field(default_factory=list)
    withheld: list[WithheldAction] = Field(default_factory=list)
    terminal: bool = Field(default=False)
    terminal_reason: str = Field(default="")
    selected: str | None = Field(default=None)
    ignored_unknown_history: int = Field(default=0, ge=0)


_FORCING_RULES: tuple[tuple[str, str, int], ...] = (
    ("value_read", "GET_CODE", _PRIORITY_FORCE_VALUE_READ),
    ("walk_stack", "WALK_STACK", _PRIORITY_FORCE_WALK_STACK),
    ("floor", "SEARCH_CODE", _PRIORITY_FORCE_FLOOR),
    ("formulate", "FORMULATE_HYPOTHESIS", _PRIORITY_FORCE_FORMULATE),
    ("synthesis", "FORMULATE_HYPOTHESIS", _PRIORITY_FORCE_SYNTHESIS),
    ("reread", "GET_CODE", _PRIORITY_FORCE_REREAD),
    ("callsite", "GET_CODE", _PRIORITY_FORCE_CALLSITE),
)

_TRIGGER_BY_RULE = {
    "value_read": "value_target_present",
    "walk_stack": "stack_walk_owed",
    "floor": "below_evidence_floor",
    "formulate": "formulate_owed",
    "synthesis": "synthesis_owed",
    "reread": "reread_path_present",
    "callsite": "callsite_owed",
}

_GATHERING_ACTIONS: tuple[str, ...] = tuple(
    a.value
    for a in ActionType
    if a.value not in ("FORMULATE_HYPOTHESIS", "VERIFY_HYPOTHESIS", "CONCLUDE")
)


def _known_actions() -> frozenset[str]:
    return frozenset(a.value for a in ActionType)


def _forcing_cap(snapshot: PlannerSnapshot, rule: str) -> int:
    try:
        return int(snapshot.forcing_caps.get(rule, FORCING_CAP_DEFAULT))
    except (TypeError, ValueError):
        raise PolicyError(PolicyErrorCode.BAD_SNAPSHOT, f"bad forcing cap: {rule}") from None


def _forcing_used(snapshot: PlannerSnapshot, rule: str) -> int:
    try:
        used = int(snapshot.forcing_used.get(rule, 0))
    except (TypeError, ValueError):
        raise PolicyError(PolicyErrorCode.BAD_SNAPSHOT, f"bad forcing count: {rule}") from None
    if used < 0:
        raise PolicyError(PolicyErrorCode.BAD_SNAPSHOT, f"negative forcing count: {rule}")
    return used


def _last_outcome(history: list[HistoryEntry], action: str) -> str | None:
    for entry in reversed(history):
        if entry.action == action:
            return entry.outcome
    return None


def decide(snapshot: PlannerSnapshot) -> PolicyDecision:
    """Rank admissible actions for a frozen snapshot (pure, deterministic).

    Never returns an empty candidate list: when nothing else is
    admissible the terminal CONCLUDE carries the loop out instead of
    stalling it. BLOCKED actions are withheld (dispatcher denial means
    retrying blind loops); FAILED actions carry a recovery penalty.
    """
    if not isinstance(snapshot, PlannerSnapshot):
        raise PolicyError(PolicyErrorCode.BAD_SNAPSHOT, "snapshot must be a PlannerSnapshot")
    history = list(snapshot.history or [])
    ignored = sum(1 for e in history if e.action not in _known_actions())
    known_history = [e for e in history if e.action in _known_actions()]

    terminal_reason = ""
    if not snapshot.reasoning_available:
        terminal_reason = "reasoning-unavailable terminal"
    elif snapshot.actions_used >= snapshot.action_limit:
        terminal_reason = "budget-exhausted terminal"
    elif snapshot.iteration >= snapshot.max_iterations:
        terminal_reason = "iteration-exhausted terminal"
    elif snapshot.token_limit > 0 and snapshot.tokens_used >= snapshot.token_limit:
        terminal_reason = "token-exhausted terminal"
    if terminal_reason:
        conclude = RankedAction(
            action="CONCLUDE", priority=_PRIORITY_CONCLUDE_TERMINAL,
            reason=terminal_reason, forced=False,
        )
        return PolicyDecision(
            candidates=[conclude], withheld=[], terminal=True,
            terminal_reason=terminal_reason, selected="CONCLUDE",
            ignored_unknown_history=ignored,
        )

    candidates: list[RankedAction] = []
    withheld: list[WithheldAction] = []
    fired_forcing: set[str] = set()
    forced_actions: set[str] = set()

    for rule, action, priority in _FORCING_RULES:
        trigger = getattr(snapshot, _TRIGGER_BY_RULE[rule], False)
        if not trigger:
            continue
        if _forcing_used(snapshot, rule) >= _forcing_cap(snapshot, rule):
            withheld.append(
                WithheldAction(action=action, reason=f"forcing cap reached: {rule}")
            )
            continue
        if action == "FORMULATE_HYPOTHESIS":
            fired_forcing.add(rule)
        forced_actions.add(action)
        candidates.append(
            RankedAction(
                action=action, priority=priority,
                reason=f"forcing rule fires: {rule}", forced=True,
            )
        )

    counts: dict[str, int] = {}
    for entry in known_history:
        counts[entry.action] = counts.get(entry.action, 0) + 1
    for key, value in (snapshot.action_counts or {}).items():
        if key in _known_actions():
            counts[key] = max(counts.get(key, 0), int(value))

    for action in _GATHERING_ACTIONS:
        cap = snapshot.action_caps.get(action)
        if cap is not None and counts.get(action, 0) >= int(cap):
            withheld.append(
                WithheldAction(action=action, reason="per-action run cap reached")
            )
            continue
        last = _last_outcome(known_history, action)
        if last == "BLOCKED":
            withheld.append(
                WithheldAction(action=action, reason="dispatcher denied; no blind retry")
            )
            continue
        priority = _PRIORITY_GATHERING_BASE
        reason = "admissible gathering"
        if last == "FAILED":
            priority += _PRIORITY_FAILED_PENALTY
            reason = "admissible gathering; penalized after failure (recovery)"
        candidates.append(
            RankedAction(action=action, priority=priority, reason=reason, forced=False)
        )

    if "FORMULATE_HYPOTHESIS" not in forced_actions and snapshot.evidence_banked_total > 0:
        candidates.append(
            RankedAction(
                action="FORMULATE_HYPOTHESIS", priority=_PRIORITY_FORMULATE_BASE,
                reason="banked evidence available to commit", forced=False,
            )
        )
    elif "FORMULATE_HYPOTHESIS" not in forced_actions and not any(
        r == "formulate" or r == "synthesis" for r in fired_forcing
    ):
        withheld.append(
            WithheldAction(
                action="FORMULATE_HYPOTHESIS", reason="evidence floor: nothing banked to commit"
            )
        )
    if snapshot.unverified_count > 0:
        candidates.append(
            RankedAction(
                action="VERIFY_HYPOTHESIS", priority=_PRIORITY_VERIFY_BASE,
                reason="unverified hypotheses await evaluation", forced=False,
            )
        )
    else:
        withheld.append(
            WithheldAction(action="VERIFY_HYPOTHESIS", reason="no unverified hypotheses")
        )

    if snapshot.verified_present:
        candidates.append(
            RankedAction(
                action="CONCLUDE", priority=_PRIORITY_CONCLUDE_VERIFIED,
                reason="proof-backed conclusion available", forced=False,
            )
        )
    else:
        candidates.append(
            RankedAction(
                action="CONCLUDE", priority=_PRIORITY_CONCLUDE_BASE,
                reason="terminal fallback; commits whatever the loop established",
                forced=False,
            )
        )

    candidates.sort(key=lambda c: (c.priority, c.action))
    selected = candidates[0].action if candidates else None
    return PolicyDecision(
        candidates=candidates, withheld=withheld, terminal=False,
        terminal_reason="", selected=selected,
        ignored_unknown_history=ignored,
    )


def diff_decisions(before: PolicyDecision, after: PolicyDecision) -> dict[str, Any]:
    """Pure divergence summary between two decisions (G6 harness support).

    Reports selected-action flips, terminal flips, and candidate-set
    changes — the exact signals a shadow-vs-actual comparison needs.
    """
    before_set = {c.action for c in before.candidates}
    after_set = {c.action for c in after.candidates}
    return {
        "selected_before": before.selected,
        "selected_after": after.selected,
        "selected_flipped": before.selected != after.selected,
        "terminal_before": before.terminal,
        "terminal_after": after.terminal,
        "terminal_flipped": before.terminal != after.terminal,
        "added": sorted(after_set - before_set),
        "removed": sorted(before_set - after_set),
    }
