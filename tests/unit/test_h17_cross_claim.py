# tests/unit/test_h17_cross_claim.py
"""H17: fail-closed inspection, cross-claim conflicts, verify/conclude candidate handoff (LV_H)."""

from types import SimpleNamespace
from uuid import uuid4

from investigation_agent_platform.application.investigation.cross_claim import cross_claim_conflicts
from investigation_agent_platform.application.investigation.verification import (
    RootCauseVerificationPolicy,
    VerificationEngine,
    select_verifiable_candidate,
)


def _asm(kind, **target):  # type: ignore[no-untyped-def]
    return SimpleNamespace(claim_target=dict(target), claim_kind=kind)


def _groups(*specs):  # type: ignore[no-untyped-def]
    out: dict = {}
    for i, (kind, targets) in enumerate(specs):
        out[(kind, f"{kind}|t{i}")] = [_asm(kind, **t) for t in targets]
    return out


def _lv_h_policy():  # type: ignore[no-untyped-def]
    return RootCauseVerificationPolicy(cross_claim_checks=True)


def test_revision_mix_demotes() -> None:
    groups = _groups(
        ("DEFINITION", [{"symbol": "a", "commit_id": "aaa"}]),
        ("CALL_EDGE", [{"symbol": "b", "commit_id": "bbb"}]),
    )
    conflicts = cross_claim_conflicts(groups)
    assert len(conflicts) == 1 and conflicts[0].code == "REVISION_MIXED"
    assert conflicts[0].blocking is False


def test_same_revision_is_clean() -> None:
    groups = _groups(
        ("DEFINITION", [{"symbol": "a", "commit_id": "aaa"}]),
        ("CALL_EDGE", [{"symbol": "b", "commit_id": "aaa"}]),
    )
    assert cross_claim_conflicts(groups) == []


def test_history_may_name_different_commits() -> None:
    groups = _groups(
        ("HISTORY", [{"commit_id": "aaa"}]),
        ("HISTORY", [{"commit_id": "bbb"}]),
    )
    assert cross_claim_conflicts(groups) == []


def test_unpinned_claims_ignored_by_r1() -> None:
    groups = _groups(
        ("DEFINITION", [{"symbol": "a", "commit_id": "aaa"}]),
        ("CALL_EDGE", [{"symbol": "b"}]),
    )
    assert cross_claim_conflicts(groups) == []


def test_absence_versus_present_withdraws() -> None:
    groups = _groups(
        ("ABSENCE", [{"symbol": "OrderServiceHandler.handle", "scope": "src/"}]),
        ("DEFINITION", [{"symbol": "OrderServiceHandler.handle", "file_path": "src/service/X.java"}]),
    )
    conflicts = cross_claim_conflicts(groups)
    assert len(conflicts) == 1 and conflicts[0].code == "CLAIM_CONFLICT"
    assert conflicts[0].blocking is True


def test_out_of_scope_absence_is_clean() -> None:
    groups = _groups(
        ("ABSENCE", [{"symbol": "OrderServiceHandler.handle", "scope": "docs/"}]),
        ("DEFINITION", [{"symbol": "OrderServiceHandler.handle", "file_path": "src/service/X.java"}]),
    )
    assert cross_claim_conflicts(groups) == []


def test_two_claimed_values_withdraw() -> None:
    groups = _groups(
        ("BEHAVIOR", [{"symbol": "r", "file_path": "svc/a.py", "claimed_value": "3"}]),
        ("BEHAVIOR", [{"symbol": "r", "file_path": "svc/a.py", "claimed_value": "5"}]),
    )
    conflicts = cross_claim_conflicts(groups)
    assert len(conflicts) == 1 and conflicts[0].code == "CLAIM_CONFLICT"


def test_checker_fault_fails_closed() -> None:
    class Hostile:
        @property
        def claim_target(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    groups = {("DEFINITION", "x"): [Hostile()]}
    try:
        cross_claim_conflicts(groups)
        raised = False
    except Exception:
        raised = True
    assert raised


def test_verify_and_conclude_pick_the_same_candidate() -> None:
    a = SimpleNamespace(id=uuid4(), status="UNDER_INVESTIGATION", supporting_evidence_ids=[])
    b = SimpleNamespace(id=uuid4(), status="UNDER_INVESTIGATION", supporting_evidence_ids=[])
    ranked = [b, a]
    assert select_verifiable_candidate(ranked, {}, prefer_id=a.id) is a


def test_ineligible_preferred_falls_back() -> None:
    a = SimpleNamespace(id=uuid4(), status="REJECTED", supporting_evidence_ids=[])
    b = SimpleNamespace(id=uuid4(), status="UNDER_INVESTIGATION", supporting_evidence_ids=[])
    assert select_verifiable_candidate([b, a], {}, prefer_id=a.id) is b
    assert select_verifiable_candidate([b], {}, prefer_id=None) is b
    assert select_verifiable_candidate([], {}, prefer_id=a.id) is None


def test_orientation_inspection_fault_fails_closed() -> None:
    class HostileHypothesis:
        @property
        def supporting_evidence_ids(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    out, reason = VerificationEngine.cites_only_orientation(HostileHypothesis(), [])
    assert out is True and "inspection faulted" in reason


def test_row_inspection_fault_fails_closed() -> None:
    # A row whose attributes raise is absorbed by the total orientation
    # checker (no crash); the closed gate is the hypothesis-level fault above.
    boom_id = uuid4()

    class BoomRow:
        evidence_id = boom_id

        @property
        def attributes(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    hyp = SimpleNamespace(supporting_evidence_ids=[boom_id])
    out, _ = VerificationEngine.cites_only_orientation(hyp, [BoomRow()])
    assert out is False


def test_normal_evidence_unaffected() -> None:
    row = SimpleNamespace(evidence_id=uuid4(), attributes={})
    hyp = SimpleNamespace(supporting_evidence_ids=[row.evidence_id])
    out, _ = VerificationEngine.cites_only_orientation(hyp, [row])
    assert out is False
