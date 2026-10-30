"""WP-C2 publication linearization: order, stale deny, no bare VERIFIED.

Pins the private linearization sub-step (``_decide_linearization``) used
by the guarded promotion entry. The public promotion contract lives on
``decide_guarded_promotion`` (obligations layer); no test here can
produce an authoritative promotion decision, by construction — the
seam takes gate-derived values, not caller requests.
"""

from __future__ import annotations

import pytest

from investigation_agent_platform.domain.proof.certificate import CheckerOutcome
from investigation_agent_platform.domain.proof.publication import (
    DenyReason,
    PublicationDecision,
    _decide_linearization,
    build_commit,
)


def _ok(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "checker_outcome": CheckerOutcome.VALID,
        "certificate_context_id": "ctx-1",
        "expected_context_id": "ctx-1",
        "reconciliation_adjudicated": True,
        "policy_eligible": True,
        "policy_reason": "",
    }
    base.update(over)
    return base


def _decide(**over: object) -> tuple[PublicationDecision, str]:
    args = _ok(**over)
    return _decide_linearization(
        checker_outcome=args["checker_outcome"],  # type: ignore[arg-type]
        certificate_context_id=str(args["certificate_context_id"]),
        expected_context_id=args["expected_context_id"],  # type: ignore[arg-type]
        reconciliation_adjudicated=bool(args["reconciliation_adjudicated"]),
        policy_eligible=bool(args["policy_eligible"]),
        policy_reason=str(args["policy_reason"]),
    )


def test_commit_when_checklist_satisfied() -> None:
    decision, _ = _decide()
    assert decision == PublicationDecision.COMMIT


def test_no_pinned_context_denies_first() -> None:
    decision, reason = _decide(expected_context_id=None)
    assert decision == PublicationDecision.DENY
    assert reason == DenyReason.NO_PINNED_CONTEXT.value


def test_non_valid_checker_denies_k13() -> None:
    for outcome in (
        CheckerOutcome.MISSING_PREMISE,
        CheckerOutcome.INADMISSIBLE_PREMISE,
        CheckerOutcome.INVALID_INFERENCE,
        CheckerOutcome.SCOPE_MISMATCH,
        CheckerOutcome.UNSUPPORTED_CONSTRUCT,
        CheckerOutcome.STALE_CERTIFICATE,
        CheckerOutcome.CONFLICTING_EVIDENCE,
        CheckerOutcome.PREMISE_UNAVAILABLE,
        CheckerOutcome.CHECKER_FAILURE,
    ):
        decision, reason = _decide(checker_outcome=outcome)
        assert decision == PublicationDecision.DENY
        assert reason.startswith(DenyReason.NOT_CHECKER_ACCEPTED.value)


def test_unadjudicated_conflict_denies() -> None:
    decision, reason = _decide(reconciliation_adjudicated=False)
    assert decision == PublicationDecision.DENY
    assert reason == DenyReason.CONFLICT_UNRESOLVED.value


def test_stale_context_denies_k8_k15() -> None:
    decision, reason = _decide(certificate_context_id="ctx-old", expected_context_id="ctx-new")
    assert decision == PublicationDecision.DENY
    assert reason == DenyReason.STALE_CONTEXT.value


def test_policy_denial_preserves_proof() -> None:
    decision, reason = _decide(policy_eligible=False, policy_reason="freshness window")
    assert decision == PublicationDecision.DENY
    assert reason.startswith(DenyReason.POLICY_DENIED.value)


def test_checklist_order_no_pinned_beats_stale() -> None:
    # Deterministic order: first failure wins, attributable to one cause.
    decision, reason = _decide(
        expected_context_id=None,
        checker_outcome=CheckerOutcome.INVALID_INFERENCE,
        certificate_context_id="ctx-other",
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
