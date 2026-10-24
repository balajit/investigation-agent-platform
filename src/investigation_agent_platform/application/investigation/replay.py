# src/investigation_agent_platform/application/investigation/replay.py
"""G6 replay harness, unit half (pure, deterministic, side-effect-free).

Replays recorded loop histories through the WP-D2 shadow policy
(``action_policy.decide``) and measures shadow-vs-recorded divergence.
Like the policy itself this module never executes anything, touches
I/O, reads the clock, or feeds a derivation: it takes parsed structures
and returns frozen reports. Fixture loading lives in tests; the live
measurement leg (replay on real runs) is operator-owned and OPEN.

Report vocabulary is frozen and append-only: ``match`` means the shadow
selected exactly the recorded action; anything else is a divergence
with a bounded cause (forcing rule, terminal state, withheld-recorded,
or plain rank disagreement). Recovery shaping (failure penalties,
blind-retry withholding) steers the ranking behind matches and surfaces
via ``withheld_reason`` behind divergences.
Turn-level scope: each turn replays independently through ``decide``;
cross-turn continuity (budget monotonicity across a recorded history)
is live-leg scope, not checked here.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.application.investigation.action_policy import (
    PlannerSnapshot,
    PolicyDecision,
    PolicyError,
    PolicyErrorCode,
    decide,
)

_RECORDED_OUTCOME_DEFAULT = "SUCCEEDED"


class RecordedStep(BaseModel):
    """One recorded loop turn: the frozen snapshot plus what the loop did."""

    model_config = ConfigDict(frozen=True)

    snapshot: PlannerSnapshot
    recorded_action: str = Field(..., max_length=64)
    # Carried for the operator-owned live-measurement leg
    # (recorded-vs-shadow outcome comparison); the unit harness scores
    # selection only and never reads this field.
    recorded_outcome: str = Field(default=_RECORDED_OUTCOME_DEFAULT, max_length=32)


class StepReport(BaseModel):
    """Shadow-vs-recorded comparison for a single turn (frozen)."""

    model_config = ConfigDict(frozen=True)

    index: int = Field(..., ge=0)
    recorded: str
    selected: str | None
    match: bool = Field(default=False)
    cause: str = Field(default="")
    shadow_terminal: bool = Field(default=False)
    recorded_was_withheld: bool = Field(default=False)
    withheld_reason: str = Field(default="")


class ReplayReport(BaseModel):
    """Aggregate over a replayed history (frozen)."""

    model_config = ConfigDict(frozen=True)

    steps: list[StepReport] = Field(default_factory=list)
    total: int = Field(default=0, ge=0)
    matched: int = Field(default=0, ge=0)
    diverged: int = Field(default=0, ge=0)
    terminal_steps: int = Field(default=0, ge=0)


def _cause_for(
    *,
    decision: PolicyDecision,
    withheld_reason: str,
) -> str:
    """Bounded divergence cause for a mismatched turn."""
    if decision.terminal:
        return "shadow-terminal: loop should have concluded"
    if withheld_reason:
        return f"recorded-withheld: {withheld_reason}"
    selected = next((c for c in decision.candidates if c.action == decision.selected), None)
    if selected is not None and selected.forced:
        return f"forcing-rule: {selected.reason}"
    return "rank-disagreement: recorded action admissible but not top-ranked"


def replay_history(steps: list[RecordedStep]) -> ReplayReport:
    """Replay recorded turns through ``decide`` (pure, deterministic).

    Unknown recorded actions are tolerated and reported as divergences
    with cause (diagnostics-grade, fail-open on data); malformed steps
    (non-``RecordedStep``) fail closed with ``BAD_SNAPSHOT``.
    """
    reports: list[StepReport] = []
    for index, step in enumerate(steps):
        if not isinstance(step, RecordedStep):
            raise PolicyError(
                PolicyErrorCode.BAD_SNAPSHOT,
                f"step {index} must be a RecordedStep",
            )
        decision = decide(step.snapshot)
        withheld_reason = next(
            (w.reason for w in decision.withheld if w.action == step.recorded_action),
            "",
        )
        match = decision.selected == step.recorded_action
        cause = (
            ""
            if match
            else _cause_for(
                decision=decision,
                withheld_reason=withheld_reason,
            )
        )
        reports.append(
            StepReport(
                index=index,
                recorded=step.recorded_action,
                selected=decision.selected,
                match=match,
                cause=cause,
                shadow_terminal=decision.terminal,
                recorded_was_withheld=bool(withheld_reason),
                withheld_reason=withheld_reason,
            )
        )
    matched = sum(1 for r in reports if r.match)
    return ReplayReport(
        steps=reports,
        total=len(reports),
        matched=matched,
        diverged=len(reports) - matched,
        terminal_steps=sum(1 for r in reports if r.shadow_terminal),
    )


def report_to_json_dict(report: ReplayReport) -> dict[str, Any]:
    """JSON-scalar view of a report (determinism / snapshot comparisons)."""
    return report.model_dump(mode="json")
