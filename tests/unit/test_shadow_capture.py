"""G6-measure shadow capture: pure mapping + replay (Option A building blocks).

The seven-line owed-flag wiring lives in the workflow (review-verified;
helpers are deterministic); everything testable without a Temporal
environment lives here and is pinned here.
"""

from __future__ import annotations

from investigation_agent_platform.application.investigation.action_policy import (
    PlannerSnapshot,
)
from investigation_agent_platform.application.investigation.shadow_capture import (
    build_shadow_snapshot,
    capture_turn,
    history_entries,
    summarize_trace,
    verification_state,
)


def test_verification_state_splits_statuses() -> None:
    hyps = [
        {"status": "VERIFIED"},
        {"status": "PROPOSED"},
        {"status": "UNDER_INVESTIGATION"},
        {"status": "REJECTED"},
        {"status": "CONTRADICTED"},
        {"status": "SUPPORTED"},
        {},
        {"status": "FAILED"},
    ]
    unverified, verified = verification_state(hyps)
    assert verified is True
    assert unverified == 4
    assert verification_state([]) == (0, False)
    assert verification_state(None) == (0, False)
    assert verification_state([{"status": "verified"}])[1] is True
    assert verification_state([{"status": "UNVERIFIED"}]) == (1, False)


def test_history_entries_map_outcomes() -> None:
    history = [
        {"action": "SEARCH_CODE", "outcome_status": "SUCCEEDED"},
        {"action": "GET_CODE", "outcome_status": "BLOCKED"},
        {"action": "WALK_STACK", "outcome_status": "FAILED"},
        {"action": "SEARCH_LOGS"},
        {"no-action": True},
        "not-a-dict",
    ]
    entries = history_entries(history)
    assert [e.action for e in entries] == ["SEARCH_CODE", "GET_CODE", "WALK_STACK", "SEARCH_LOGS"]
    assert [e.outcome for e in entries] == ["SUCCEEDED", "BLOCKED", "FAILED", "SUCCEEDED"]
    assert history_entries(None) == []
    assert history_entries("nope") == []


def _snap(**over: object) -> PlannerSnapshot:
    base: dict[str, object] = {
        "iteration": 3,
        "max_iterations": 10,
        "actions_used": 3,
        "action_limit": 10,
        "tokens_used": 100,
        "token_limit": 0,
        "evidence_banked_total": 5,
        "formulated_count": 0,
    }
    base.update(over)
    return build_shadow_snapshot(**base)  # type: ignore[arg-type]


def test_build_snapshot_maps_fields() -> None:
    s = _snap(
        unverified_count=2,
        verified_present=True,
        stack_walk_owed=True,
        forcing_used={"walk_stack": 1, "bogus-rule": 1, "floor": -1, "reread": "x"},
        action_history=[{"action": "SEARCH_CODE", "outcome_status": "SUCCEEDED"}],
    )
    assert (s.unverified_count, s.verified_present) == (2, True)
    assert s.stack_walk_owed is True
    assert s.forcing_used == {"walk_stack": 1, "bogus-rule": 1}
    s2 = _snap(forcing_used={"walk_stack": True, "floor": 2})
    assert s2.forcing_used == {"floor": 2}
    assert [e.action for e in s.history] == ["SEARCH_CODE"]


def test_build_snapshot_rejects_malformed() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _snap(iteration=-1)
    with pytest.raises(ValidationError):
        _snap(max_iterations=0)


def test_capture_turn_agrees_and_diverges() -> None:
    agree = capture_turn(_snap(evidence_banked_total=5), recorded_action="CORRELATE")
    assert agree["selected"] == "CORRELATE" and agree["match"] is True and agree["cause"] == ""
    assert agree["snapshot"]["evidence_banked_total"] == 5
    div = capture_turn(
        _snap(evidence_banked_total=5, stack_walk_owed=True),
        recorded_action="SEARCH_CODE",
    )
    assert div["selected"] == "WALK_STACK" and div["match"] is False
    assert "walk_stack" in div["cause"]


def test_capture_turn_tolerates_unknown_recorded() -> None:
    d = capture_turn(_snap(evidence_banked_total=2), recorded_action="FUTURE_ACTION")
    assert d["match"] is False and d["cause"] != ""


def test_capture_deterministic() -> None:
    kw = {"evidence_banked_total": 5, "unverified_count": 1}
    assert capture_turn(_snap(**kw), "CORRELATE") == capture_turn(_snap(**kw), "CORRELATE")


def test_summarize_trace_eligibility_semantics() -> None:
    trace = [
        {"iteration": 0, "match": True},
        {"iteration": 1, "match": False, "cause": "forcing-rule: x"},
        {"iteration": 2, "capture_error": True},
        "not-a-dict",
    ]
    assert summarize_trace(trace) == {
        "turns": 4,
        "matched": 1,
        "capture_errors": 2,
        "eligible": 2,
        "diverged": 1,
    }
    assert summarize_trace([]) == {
        "turns": 0,
        "matched": 0,
        "capture_errors": 0,
        "eligible": 0,
        "diverged": 0,
    }
