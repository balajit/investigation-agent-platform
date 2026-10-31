"""H8: a cited claim group that fails its own gate gets a re-fetch route.

Review H8: ``build_claim_gaps`` skipped every cited group ("repair, not
smallest-fetch"), repair needs an already-banked uncited superset row, and
``select_ledger_requery`` fires only on unknown/never-retrieved/failed-search.
So a DEFINITION that cites a pinned range read whose window misses the symbol
ended INCONCLUSIVE with no route to a better read.

Fix (lv3): the verify activity computes which cited groups fail their own gate
over the rows they cite, and ``build_claim_gaps`` emits those as gaps tagged
``reason="cited_gate_failed"``. The existing smallest-fetch machinery consumes
them unchanged. Only retrieval is added: nothing here can publish a verdict.
"""

from __future__ import annotations

from test_claim_binding_phase3 import _code_row, _def_hyp, _def_read

from investigation_agent_platform.application.investigation.verification import (
    RootCauseVerificationPolicy,
    VerificationEngine,
    VerificationStatus,
    build_claim_gaps,
    target_group_key,
)
from investigation_agent_platform.application.worker.workflows.forcing import (
    _carry_gap_orders,
)
from investigation_agent_platform.domain.hypothesis.models import (
    AssessmentStrength,
    AssessmentType,
    ClaimKind,
    ClaimTarget,
    HypothesisEvidenceAssessment,
)


def _policy() -> RootCauseVerificationPolicy:
    return RootCauseVerificationPolicy()


def _miss_read():
    """A range read of lines 1-3 that does NOT contain the claimed symbol."""
    return _code_row(
        "GET_CODE",
        "src/alpha.py",
        "abc123",
        "1| import os\n2| import sys\n3| LIMIT = 3",
        start=1,
        end=3,
        read_mode="range",
    )


def _gid(hyp, kind="DEFINITION"):
    target = next(a.claim_target for a in hyp.assessments if a.claim_target is not None)
    return f"{kind}|{target_group_key(target)[0]}"


def _failed(hyp, rows, status=VerificationStatus.INCONCLUSIVE):
    engine = VerificationEngine()
    result = engine._fault_inconclusive(hyp)
    result = result.model_copy(update={"status": status})
    return engine.failed_cited_group_ids(hyp, result, rows, [], [], _policy())


def _asm(hyp, kind, target, evidence_id):
    return HypothesisEvidenceAssessment(
        hypothesis_id=hyp.id,
        evidence_id=evidence_id,
        assessment=AssessmentType.SUPPORTS,
        strength=AssessmentStrength.STRONG,
        reason="claim",
        claim_kind=kind,
        claim_target=target,
    )


# --- the reviewer's scenario, reproduced -------------------------------------------------


def test_today_a_cited_group_that_misses_the_symbol_has_no_re_fetch_route() -> None:
    row = _miss_read()
    hyp = _def_hyp(row)
    # Premise: the group cites a bound row, but the window does not contain the symbol.
    assert VerificationEngine().validate_location_claim(hyp, [row], []) is None or (
        VerificationEngine().validate_location_claim(hyp, [row], []).status
        != VerificationStatus.VERIFIED
    )
    feed = build_claim_gaps(hyp, {_gid(hyp)})
    assert feed["gaps"] == [], "unchanged default: a cited group is never a gap"


# --- which cited groups failed their own gate -----------------------------------------------


def test_a_cited_group_failing_its_gate_is_reported() -> None:
    row = _miss_read()
    hyp = _def_hyp(row)
    assert _failed(hyp, [row]) == [_gid(hyp)]


def test_a_cited_group_that_verifies_is_not_reported() -> None:
    row = _def_read()
    hyp = _def_hyp(row)
    assert _failed(hyp, [row]) == []


def test_a_verified_result_reports_nothing() -> None:
    row = _miss_read()
    hyp = _def_hyp(row)
    assert _failed(hyp, [row], status=VerificationStatus.VERIFIED) == []


def test_a_cited_but_unbound_definition_is_reported() -> None:
    row = _def_read()
    hyp = _def_hyp(row)
    assert _failed(hyp, []) == [_gid(hyp)], "the cited row evaporated: no bound row exists"


def test_uncited_groups_are_not_reported_they_are_already_gaps() -> None:
    row = _def_read()
    base = _def_hyp(row)
    uncited = _asm(
        base,
        ClaimKind.DEFINITION,
        ClaimTarget(symbol="other", file_path="src/alpha.py"),
        evidence_id=None,
    )
    hyp = base.model_copy(update={"assessments": [*base.assessments, uncited]})
    assert _failed(hyp, [row]) == []


def test_not_checkable_and_targetless_groups_are_never_reported() -> None:
    row = _miss_read()
    base = _def_hyp(row)
    nc = _asm(base, ClaimKind.NOT_CHECKABLE, ClaimTarget(symbol="elegant"), row.evidence_id)
    targetless = _asm(base, ClaimKind.CAUSAL, None, row.evidence_id)
    hyp = base.model_copy(update={"assessments": [nc, targetless]})
    assert _failed(hyp, [row]) == []


def test_only_the_failing_group_of_a_multi_group_hypothesis_is_reported() -> None:
    good = _def_read()
    bad = _miss_read()
    base = _def_hyp(good)
    second = _asm(
        base,
        ClaimKind.DEFINITION,
        ClaimTarget(symbol="missingFn", file_path="src/alpha.py"),
        evidence_id=bad.evidence_id,
    )
    hyp = base.model_copy(update={"assessments": [*base.assessments, second]})
    reported = _failed(hyp, [good, bad])
    assert reported == [f"DEFINITION|{target_group_key(second.claim_target)[0]}"]


def test_never_raises_on_garbage() -> None:
    engine = VerificationEngine()
    assert engine.failed_cited_group_ids(object(), object(), None, None, None, None) == []  # type: ignore[arg-type]


# --- the gap feed ------------------------------------------------------------------------------


def test_a_failed_cited_group_becomes_a_tagged_gap() -> None:
    row = _miss_read()
    hyp = _def_hyp(row)
    gid = _gid(hyp)
    feed = build_claim_gaps(hyp, {gid}, failed_cited_group_ids={gid})
    assert [g["gap_id"] for g in feed["gaps"]] == [gid]
    gap = feed["gaps"][0]
    assert gap["reason"] == "cited_gate_failed"
    assert gap["kind"] == "DEFINITION"
    assert gap["target"]["symbol"] == "handleRequest"


def test_a_cited_group_not_reported_failed_is_still_not_a_gap() -> None:
    row = _def_read()
    hyp = _def_hyp(row)
    feed = build_claim_gaps(hyp, {_gid(hyp)}, failed_cited_group_ids=set())
    assert feed["gaps"] == []


def test_uncited_gaps_keep_their_exact_shape() -> None:
    row = _def_read()
    base = _def_hyp(row)
    uncited = _asm(
        base,
        ClaimKind.DEFINITION,
        ClaimTarget(symbol="other", file_path="src/alpha.py"),
        evidence_id=None,
    )
    hyp = base.model_copy(update={"assessments": [uncited]})
    feed = build_claim_gaps(hyp, set(), failed_cited_group_ids={"DEFINITION|nope"})
    assert len(feed["gaps"]) == 1
    assert "reason" not in feed["gaps"][0]


def test_the_existing_smallest_fetch_machinery_consumes_a_failed_group_gap() -> None:
    row = _miss_read()
    hyp = _def_hyp(row)
    gid = _gid(hyp)
    feed = build_claim_gaps(hyp, {gid}, failed_cited_group_ids={gid})
    carrier = _carry_gap_orders({"claim_gaps": feed["gaps"]}, 3)
    assert carrier is not None
    assert len(carrier["orders"]) == 1, "a GAP_FILL order is built for the failed group"
    assert carrier["dropped"]["unbuildable"] == 0


# --- version pin ------------------------------------------------------------------------------


def test_the_activity_routes_the_refetch_on_the_loop_version() -> None:
    import inspect

    from investigation_agent_platform.application.worker.activities import verification

    source = inspect.getsource(verification)
    assert "failed_cited_group_ids" in source
    assert "loop_version_at_least" in source, "the H8 re-fetch is lv3-pinned"
