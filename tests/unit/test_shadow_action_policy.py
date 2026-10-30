"""WP-D2 shadow action policy: deterministic, replay-safe, side-effect-free.

The policy mirrors the workflow's guardrails (forcing caps, evidence
floor, formulation/verification gating, terminal conditions, failure
recovery) as a pure function over a frozen snapshot. The workflow does
not call it; a G6 harness replays recorded histories through decide()
and measures divergence.
"""

from __future__ import annotations

import pytest

from investigation_agent_platform.application.investigation.action_policy import (
    HistoryEntry,
    PlannerSnapshot,
    PolicyError,
    decide,
    diff_decisions,
)


def _snap(**over: object) -> PlannerSnapshot:
    return PlannerSnapshot(**over)  # type: ignore[arg-type]


def test_deterministic_same_snapshot_same_decision() -> None:
    snap = _snap(
        iteration=3,
        evidence_banked_total=5,
        unverified_count=1,
        history=[HistoryEntry(action="SEARCH_CODE", outcome="SUCCEEDED")],
    )
    assert decide(snap).model_dump() == decide(snap).model_dump()


def test_json_round_trip_replays_identically() -> None:
    snap = _snap(iteration=2, stack_walk_owed=True, evidence_banked_total=4)
    revived = PlannerSnapshot.model_validate_json(snap.model_dump_json())
    assert decide(revived).model_dump() == decide(snap).model_dump()


def test_terminal_on_action_limit() -> None:
    d = decide(_snap(actions_used=10, action_limit=10))
    assert d.terminal and d.selected == "CONCLUDE"
    assert [c.action for c in d.candidates] == ["CONCLUDE"]


def test_terminal_on_iteration_tokens_reasoning() -> None:
    assert decide(_snap(iteration=10, max_iterations=10)).terminal
    assert decide(_snap(tokens_used=9, token_limit=9)).terminal
    d = decide(_snap(reasoning_available=False))
    assert d.terminal and "reasoning" in d.terminal_reason


def test_verified_present_conclude_first() -> None:
    d = decide(_snap(verified_present=True, evidence_banked_total=6))
    assert d.selected == "CONCLUDE"
    assert d.candidates[0].priority < 100


def test_forcing_cascade_order() -> None:
    d = decide(
        _snap(
            stack_walk_owed=True,
            formulate_owed=True,
            callsite_owed=True,
            evidence_banked_total=4,
        )
    )
    order = [c.action for c in d.candidates if c.forced]
    assert order == ["WALK_STACK", "FORMULATE_HYPOTHESIS", "GET_CODE"]
    assert d.selected == "WALK_STACK"


def test_forcing_cap_reached_withholds_rule() -> None:
    d = decide(_snap(stack_walk_owed=True, forcing_used={"walk_stack": 2}, evidence_banked_total=4))
    assert not any(c.forced and c.action == "WALK_STACK" for c in d.candidates)
    assert any("walk_stack" in w.reason for w in d.withheld)


def test_formulate_gated_on_banked_evidence() -> None:
    d = decide(_snap(evidence_banked_total=0))
    assert "FORMULATE_HYPOTHESIS" not in {c.action for c in d.candidates}
    assert "FORMULATE_HYPOTHESIS" in {
        c.action for c in decide(_snap(evidence_banked_total=1)).candidates
    }


def test_verify_gated_on_unverified() -> None:
    assert "VERIFY_HYPOTHESIS" not in {c.action for c in decide(_snap()).candidates}
    assert "VERIFY_HYPOTHESIS" in {c.action for c in decide(_snap(unverified_count=2)).candidates}


def test_blocked_withheld_failed_penalized() -> None:
    d = decide(
        _snap(
            evidence_banked_total=5,
            history=[
                HistoryEntry(action="SEARCH_LOGS", outcome="BLOCKED"),
                HistoryEntry(action="GET_CODE", outcome="FAILED"),
            ],
        )
    )
    assert "SEARCH_LOGS" not in {c.action for c in d.candidates}
    get_code = next(c for c in d.candidates if c.action == "GET_CODE")
    assert get_code.priority > 1000
    assert "penalized" in get_code.reason


def test_per_action_cap_enforced_when_set() -> None:
    d = decide(
        _snap(
            evidence_banked_total=5,
            action_counts={"SEARCH_LOGS": 3},
            action_caps={"SEARCH_LOGS": 3},
        )
    )
    assert "SEARCH_LOGS" not in {c.action for c in d.candidates}


def test_unknown_history_tolerated_and_counted() -> None:
    d = decide(
        _snap(
            evidence_banked_total=2,
            history=[HistoryEntry(action="FUTURE_ACTION", outcome="SUCCEEDED")],
        )
    )
    assert d.ignored_unknown_history == 1
    assert d.selected is not None


def test_bad_snapshot_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PlannerSnapshot(iteration=-1)
    with pytest.raises(PolicyError):
        decide("not-a-snapshot")  # type: ignore[arg-type]


def test_diff_detects_flips() -> None:
    a = decide(_snap(evidence_banked_total=4))
    b = decide(_snap(evidence_banked_total=4, verified_present=True))
    diff = diff_decisions(a, b)
    assert diff["selected_flipped"] is True
    assert diff["selected_after"] == "CONCLUDE"
    same = diff_decisions(a, decide(_snap(evidence_banked_total=4)))
    assert same["selected_flipped"] is False and same["added"] == [] and same["removed"] == []


def test_never_stalls_first_turn() -> None:
    d = decide(_snap())
    assert d.selected is not None and d.candidates
    assert d.candidates[-1].action == "CONCLUDE"
