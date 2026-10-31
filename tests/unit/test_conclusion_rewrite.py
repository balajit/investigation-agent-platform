"""LWW-removal contract: conclusion rewrite transition predicate."""

from __future__ import annotations

from investigation_agent_platform.domain.finding.models import (
    HANDOFF_VERSION_CURRENT,
    TERMINAL_CONCLUSION_STATUSES,
    HandoffState,
    classify_terminal_handoff,
    conclusion_rewrite_allowed,
)


def test_no_row_allows_insert() -> None:
    assert conclusion_rewrite_allowed(None, "ANSWERED") is True
    assert conclusion_rewrite_allowed(None, "INSUFFICIENT_EVIDENCE") is True


def test_same_status_allows_idempotent_rewrite() -> None:
    for s in (
        "ANSWERED",
        "ROOT_CAUSE_ESTABLISHED",
        "ROOT_CAUSE_LIKELY",
        "INSUFFICIENT_EVIDENCE",
        "NO_ROOT_CAUSE_FOUND",
    ):
        assert conclusion_rewrite_allowed(s, s) is True


def test_non_terminal_current_allows_anything() -> None:
    for cur in ("INSUFFICIENT_EVIDENCE", "NO_ROOT_CAUSE_FOUND"):
        for new in (
            "ANSWERED",
            "ROOT_CAUSE_ESTABLISHED",
            "INSUFFICIENT_EVIDENCE",
            "NO_ROOT_CAUSE_FOUND",
        ):
            assert conclusion_rewrite_allowed(cur, new) is True


def test_terminal_current_denies_different_status() -> None:
    assert TERMINAL_CONCLUSION_STATUSES == frozenset(
        {"ANSWERED", "ROOT_CAUSE_ESTABLISHED", "ROOT_CAUSE_LIKELY"}
    )
    for cur in ("ANSWERED", "ROOT_CAUSE_ESTABLISHED", "ROOT_CAUSE_LIKELY"):
        for new in (
            "ANSWERED",
            "ROOT_CAUSE_ESTABLISHED",
            "ROOT_CAUSE_LIKELY",
            "INSUFFICIENT_EVIDENCE",
            "NO_ROOT_CAUSE_FOUND",
        ):
            assert conclusion_rewrite_allowed(cur, new) is (cur == new)


def test_s7_handoff_version_is_pinned() -> None:
    assert HANDOFF_VERSION_CURRENT == "handoff-v1"


def test_s7_verdict_handoff_has_no_target_or_steps() -> None:
    handoff = classify_terminal_handoff(status="ROOT_CAUSE_ESTABLISHED")
    assert handoff.version == HANDOFF_VERSION_CURRENT
    assert handoff.state == HandoffState.PROVED
    assert handoff.target == "n/a"
    assert handoff.next_fetch is None
    assert handoff.resume_pointer is None
    assert handoff.recommended_next_steps == []


def test_s7_likely_is_not_proved_and_keeps_follow_up() -> None:
    # ROOT_CAUSE_LIKELY is provisional, never proved: the follow-up must
    # survive instead of being dropped by a PROVED mapping.
    fetch = "Rule out the strongest live rival with ruling-out evidence, then re-verify."
    handoff = classify_terminal_handoff(
        status="ROOT_CAUSE_LIKELY",
        next_check=fetch,
        ranked_alternatives=["Downstream latency"],
    )
    assert handoff.state != HandoffState.PROVED
    # Prose names no concrete target: it stays in the steps, never as next_fetch.
    assert handoff.next_fetch is None
    assert handoff.price is None
    assert fetch in handoff.recommended_next_steps
    assert any("rival" in step.lower() for step in handoff.recommended_next_steps)


def test_s7_limitations_carry_provider_block_to_telemetry_deferred() -> None:
    # Callers pass generic reasons; provider details live in limitations.
    handoff = classify_terminal_handoff(
        status="INSUFFICIENT_EVIDENCE",
        reason="insufficient",
        limitations=["provider_blocked:auth_failed on the telemetry fetch"],
        next_check="Restore the blocked provider fetch and resume, then re-verify.",
    )
    assert handoff.state == HandoffState.TELEMETRY_DEFERRED
    assert handoff.target == "n/a"
    assert handoff.resume_pointer is not None


def test_s7_refusal_steps_carry_elimination_record() -> None:
    from investigation_agent_platform.domain.finding.models import EliminatedRival

    handoff = classify_terminal_handoff(
        status="NO_ROOT_CAUSE_FOUND",
        reason="refuted",
        eliminated_rivals=[
            EliminatedRival(
                statement="Downstream latency",
                status="REJECTED",
                ruled_out_by_evidence_ids=["e1"],
            )
        ],
    )
    assert handoff.state == HandoffState.FRONTIER_EXHAUSTED
    assert any("1 rival(s) ruled out" in step for step in handoff.recommended_next_steps)


def test_s7_handoff_round_trips_on_conclusion() -> None:
    from uuid import uuid4

    from investigation_agent_platform.domain.finding.models import InvestigationConclusion

    conclusion = InvestigationConclusion(
        tenant_id="t",
        investigation_id=uuid4(),
        status="INSUFFICIENT_EVIDENCE",
        limitations=["no bound supporting evidence"],
        next_check="Cite and bind supporting evidence, then re-verify.",
        recommended_next_steps=["Cite and bind supporting evidence, then re-verify."],
        handoff=classify_terminal_handoff(
            status="INSUFFICIENT_EVIDENCE",
            reason="insufficient",
            next_check="Cite and bind supporting evidence, then re-verify.",
        ),
    )
    assert conclusion.handoff is not None
    assert conclusion.handoff.version == HANDOFF_VERSION_CURRENT
    assert conclusion.handoff.state == HandoffState.FRONTIER_EXHAUSTED
    revived = InvestigationConclusion.model_validate(conclusion.model_dump(mode="json"))
    assert revived.handoff is not None
    assert revived.handoff.state == HandoffState.FRONTIER_EXHAUSTED
    assert revived.handoff.next_fetch is None
    assert "Cite and bind supporting evidence, then re-verify." in (
        revived.handoff.recommended_next_steps
    )


def test_s7_budget_handoff_prices_resume_pointer() -> None:
    fetch = "Bind the range-read row showing the call expression, then re-verify."
    handoff = classify_terminal_handoff(
        status="INSUFFICIENT_EVIDENCE",
        reason="budget_exceeded",
        next_check=fetch,
    )
    assert handoff.state == HandoffState.BUDGET_DEFERRED
    assert handoff.target == "n/a"
    assert handoff.next_fetch is None  # prose names no concrete target
    assert handoff.resume_pointer is not None and fetch[:80] in handoff.resume_pointer
    assert handoff.recommended_next_steps[0] == fetch


def test_s7_telemetry_handoff_names_blocked_fetch_without_target() -> None:
    fetch = "Restore the blocked telemetry fetch, then re-verify."
    handoff = classify_terminal_handoff(
        status="INSUFFICIENT_EVIDENCE",
        reason="provider_blocked:auth_failed",
        next_check=fetch,
    )
    assert handoff.state == HandoffState.TELEMETRY_DEFERRED
    assert handoff.target == "n/a"
    assert handoff.recommended_next_steps == [fetch]


def test_s7_refusal_handoff_uses_elimination_record_not_filler() -> None:
    handoff = classify_terminal_handoff(
        status="NO_ROOT_CAUSE_FOUND",
        reason="refuted",
        ranked_alternatives=["Downstream latency"],
    )
    assert handoff.state == HandoffState.FRONTIER_EXHAUSTED
    assert handoff.target == "n/a"
    assert any("Ranked alternatives" in step for step in handoff.recommended_next_steps)
    assert not any(step.startswith("Resolve '") for step in handoff.recommended_next_steps)
