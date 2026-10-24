"""WP-C2 publication decision: linearization order, stale deny, no bare VERIFIED."""

from __future__ import annotations

import pytest

from investigation_agent_platform.domain.proof.certificate import CheckerOutcome
from investigation_agent_platform.domain.proof.publication import (
    DenyReason,
    PublicationDecision,
    PublicationRequest,
    build_commit,
    decide_publication,
)


def _ok(**over: object) -> PublicationRequest:
    base: dict[str, object] = {
        "checker_outcome": CheckerOutcome.VALID,
        "certificate_context_id": "ctx-1",
        "expected_context_id": "ctx-1",
        "derivation_id": "der-1",
        "reconciliation_adjudicated": True,
        "policy_eligible": True,
    }
    base.update(over)
    return PublicationRequest(**base)  # type: ignore[arg-type]


def test_commit_when_checklist_satisfied() -> None:
    decision, _ = decide_publication(_ok())
    assert decision == PublicationDecision.COMMIT


def test_no_pinned_context_denies_first() -> None:
    decision, reason = decide_publication(_ok(expected_context_id=None))
    assert decision == PublicationDecision.DENY
    assert reason == DenyReason.NO_PINNED_CONTEXT.value


def test_non_valid_checker_denies_k13() -> None:
    for outcome in (
        CheckerOutcome.INVALID_INFERENCE,
        CheckerOutcome.MISSING_PREMISE,
        CheckerOutcome.PREMISE_UNAVAILABLE,
        CheckerOutcome.CHECKER_FAILURE,
    ):
        decision, reason = decide_publication(_ok(checker_outcome=outcome))
        assert decision == PublicationDecision.DENY
        assert reason.startswith(DenyReason.NOT_CHECKER_ACCEPTED.value)


def test_unadjudicated_conflict_denies() -> None:
    decision, reason = decide_publication(_ok(reconciliation_adjudicated=False))
    assert decision == PublicationDecision.DENY
    assert reason == DenyReason.CONFLICT_UNRESOLVED.value


def test_stale_context_denies_k8_k15() -> None:
    decision, reason = decide_publication(
        _ok(certificate_context_id="ctx-old", expected_context_id="ctx-new")
    )
    assert decision == PublicationDecision.DENY
    assert reason == DenyReason.STALE_CONTEXT.value


def test_policy_denial_preserves_proof() -> None:
    decision, reason = decide_publication(
        _ok(policy_eligible=False, policy_reason="freshness window")
    )
    assert decision == PublicationDecision.DENY
    assert reason.startswith(DenyReason.POLICY_DENIED.value)


def test_checklist_order_no_pinned_beats_stale() -> None:
    # Deterministic order: first failure wins, attributable to one cause.
    decision, reason = decide_publication(
        _ok(
            expected_context_id=None,
            checker_outcome=CheckerOutcome.INVALID_INFERENCE,
            certificate_context_id="ctx-other",
        )
    )
    assert (decision, reason) == (
        PublicationDecision.DENY,
        DenyReason.NO_PINNED_CONTEXT.value,
    )


def test_build_commit_binds_ids() -> None:
    commit = build_commit("der-1", "ctx-1", "ctx-1")
    assert commit.derivation_id == "der-1" and commit.context_id == "ctx-1"


def test_build_commit_refuses_bare() -> None:
    with pytest.raises(ValueError, match="BARE_VERDICT"):
        build_commit("", "ctx-1", "ctx-1")
    with pytest.raises(ValueError, match="BARE_VERDICT"):
        build_commit("der-1", "", "ctx-1")


def test_build_commit_refuses_stale() -> None:
    with pytest.raises(ValueError, match="STALE_CONTEXT"):
        build_commit("der-1", "ctx-old", "ctx-new")


def test_build_commit_refuses_unpinned_f_c2() -> None:
    # F-C2: without a pinned context there is no freshness to enforce,
    # so an unpinned commit is refused rather than allowed unchecked.
    with pytest.raises(ValueError, match="STALE_CONTEXT"):
        build_commit("der-1", "ctx-1", None)
