# tests/unit/test_h8_failed_group_refetch.py
"""H8: failed-group re-fetch route (dormant under cbv1; live on cbv2/cbv3) + NO_TARGET gaps (LV_H).

H8d characterization: these tests pass on the current tree. The red-first step
is a mutation negative control (see each test's note), not a pre-fix failure.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_claim_binding_phase1 as fixtures
from test_claim_binding_phase3 import _def_hyp, _def_read

from investigation_agent_platform.application.investigation.verification import (
    VerificationEngine,
    build_claim_gaps,
)
from investigation_agent_platform.domain.hypothesis.models import ClaimKind, ClaimTarget


def _miss_hyp():  # type: ignore[no-untyped-def]
    read = _def_read()
    hyp = _def_hyp(read, statement="The other handler is defined",
                   target=ClaimTarget(symbol="otherHandler", file_path="src/alpha.py"))
    return hyp, read


async def _verify(metadata, hyp, rows):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.worker.activities import (
        VerifyRootCauseInput,
        verify_root_cause_activity,
    )

    inv_id = hyp.investigation_id
    fixtures._activity_ctx(metadata, hyp, rows, [])
    return await verify_root_cause_activity(
        VerifyRootCauseInput(tenant_id="t", investigation_id=str(inv_id))
    )


@pytest.mark.asyncio
async def test_lv3_activity_emits_the_failed_group_gap() -> None:
    hyp, read = _miss_hyp()
    inv_id = uuid4()
    hyp = hyp.model_copy(update={"investigation_id": inv_id, "tenant_id": "t"})
    result = await _verify(
        {"claim_binding_version": "cbv3", "verifier_version": "v2", "loop_version": "lv3"},
        hyp, [read],
    )
    assert result.success is True
    gaps = result.data.get("claim_gaps", [])
    assert any(
        g.get("kind") == "DEFINITION" and g.get("reason") == "cited_gate_failed" for g in gaps
    ), gaps
    # Negative control: without the lv3 gate no failed-group gap is emitted.


@pytest.mark.asyncio
async def test_lv2_activity_emits_no_failed_gap() -> None:
    hyp, read = _miss_hyp()
    inv_id = uuid4()
    hyp = hyp.model_copy(update={"investigation_id": inv_id, "tenant_id": "t"})
    result = await _verify(
        {"claim_binding_version": "cbv3", "verifier_version": "v2", "loop_version": "lv2"},
        hyp, [read],
    )
    assert result.success is True
    assert result.data.get("claim_gaps", []) == []


@pytest.mark.asyncio
async def test_cbv1_has_no_gap_feed() -> None:
    hyp, read = _miss_hyp()
    inv_id = uuid4()
    hyp = hyp.model_copy(update={"investigation_id": inv_id, "tenant_id": "t"})
    result = await _verify(
        {"claim_binding_version": "cbv1", "verifier_version": "v2", "loop_version": "lv3"},
        hyp, [read],
    )
    assert result.success is True
    assert "claim_gaps" not in result.data


@pytest.mark.asyncio
async def test_verified_verdict_emits_no_failed_gap() -> None:
    read = _def_read()
    hyp = _def_hyp(read)
    inv_id = uuid4()
    hyp = hyp.model_copy(update={"investigation_id": inv_id, "tenant_id": "t"})
    result = await _verify(
        {"claim_binding_version": "cbv3", "verifier_version": "v2", "loop_version": "lv3"},
        hyp, [read],
    )
    assert result.success is True
    assert result.data.get("verified") is True
    assert not [g for g in result.data.get("claim_gaps", []) if g.get("reason") == "cited_gate_failed"]


def _targetless_hyp(statement, kind):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.hypothesis.models import Hypothesis, HypothesisStatus

    iid = uuid4()
    hyp = Hypothesis(
        tenant_id="t", investigation_id=iid, title=statement[:256],
        description=statement, statement=statement, status=HypothesisStatus.PROPOSED,
    )
    from investigation_agent_platform.domain.hypothesis.models import (
        AssessmentStrength,
        AssessmentType,
        HypothesisEvidenceAssessment,
    )

    asm = HypothesisEvidenceAssessment(
        hypothesis_id=hyp.id, evidence_id=None, assessment=AssessmentType.SUPPORTS,
        strength=AssessmentStrength.MODERATE, reason="taste test",
        claim_kind=kind, claim_target=ClaimTarget(),
    )
    return hyp.model_copy(update={"assessments": [asm]})


def test_no_target_behavior_becomes_gap_under_lv_h() -> None:
    hyp = _targetless_hyp("retries exhaust the connection pool", ClaimKind.BEHAVIOR)
    out = build_claim_gaps(hyp, set(), None, include_no_target=True)
    assert len(out["gaps"]) == 1
    gap = out["gaps"][0]
    assert gap["gap_id"].startswith("BEHAVIOR|NO_TARGET|")
    assert gap["no_target"] is True
    assert gap["query"] != ""


def test_no_target_causal_builds_search_logs_order() -> None:
    from investigation_agent_platform.application.worker.workflows.forcing import (
        _carry_gap_orders,
    )

    hyp = _targetless_hyp("pool exhaustion causes the timeout", ClaimKind.CAUSAL)
    out = build_claim_gaps(hyp, set(), None, include_no_target=True)
    assert len(out["gaps"]) == 1
    carrier = _carry_gap_orders({"claim_gaps": out["gaps"]}, 2)
    assert carrier is not None
    order = carrier["orders"][0]
    assert order["action_type"] == "SEARCH_LOGS"
    assert order["action_parameters"].get("keywords")
    assert "_leg" not in order["action_parameters"]


def test_flag_off_keeps_today() -> None:
    hyp = _targetless_hyp("retries exhaust the connection pool", ClaimKind.BEHAVIOR)
    assert build_claim_gaps(hyp, set(), None)["gaps"] == []
    assert build_claim_gaps(hyp, set(), None, include_no_target=False)["gaps"] == []


def test_two_hypotheses_do_not_share_no_target_gap_id() -> None:
    g1 = build_claim_gaps(_targetless_hyp("pool exhaustion causes a", ClaimKind.CAUSAL), set(), None, include_no_target=True)["gaps"]
    g2 = build_claim_gaps(_targetless_hyp("pool exhaustion causes b", ClaimKind.CAUSAL), set(), None, include_no_target=True)["gaps"]
    assert g1 and g2 and g1[0]["gap_id"] != g2[0]["gap_id"]


def test_stop_words_and_short_tokens_dropped() -> None:
    from investigation_agent_platform.application.worker.workflows.forcing import _notarget_keywords

    assert _notarget_keywords("the pool of db") == ["pool"]
    assert _notarget_keywords("db of it") == []


def test_hostile_query_bounded() -> None:
    from investigation_agent_platform.application.worker.workflows.forcing import _notarget_keywords

    out = _notarget_keywords("x" * (10 * 1024) + " \x00\x01 pool")
    assert len(out) <= 8
    assert all(len(w) <= 10 * 1024 for w in out)


def test_per_kind_cap_bounds_no_target_gaps() -> None:
    carrier = _feed(
        _gap("g1", kind="CAUSAL", target={}), _gap("g2", kind="CAUSAL", target={}),
        iteration=2,
    )
    counts: dict = {}
    for _ in range(2):
        out = _cascade([carrier], iteration=4, counts=counts)
        assert out.reason_data.get("forced_smallest_fetch") is True
        counts = dict(out.counts)
    out = _cascade([carrier], iteration=4, counts=counts)
    assert out.reason_data.get("forced_smallest_fetch") is not True  # per-kind cap of 2


def test_failed_group_gap_stays_open_after_empty_refetch() -> None:
    h8 = _feed(_gap("g1", reason="cited_gate_failed"), iteration=3)
    failed = {"action": "GET_CODE", "evidence_added": 0, "parameters": {"file_path": "svc/g.py"},
              "outcome_status": "FAILED", "miss_reason": "timeout",
              "forced_smallest_fetch_gap_id": "g1", "forced_smallest_fetch_reason": "cited_gate_failed"}
    history = [h8, dict(failed)]
    out = _cascade(history, iteration=4)
    assert out.reason_data.get("forced_smallest_fetch") is True
    history2 = [h8, dict(failed), dict(failed)]
    out2 = _cascade(history2, iteration=5)
    assert out2.reason_data.get("forced_smallest_fetch") is not True  # attempt cap of 2


def _cascade(history, iteration=4, counts=None, pin="lv9"):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.worker.workflows.forcing import (
        PostReasonCascadeState,
        apply_post_reason_cascade,
    )

    state = PostReasonCascadeState(
        reason_data={"next_action": "FORMULATE_HYPOTHESIS"}, action_history=list(history),
        evidence_banked_total=9, triage_route="BUG", description="d", knowledge_seed={},
        max_iterations=10, iteration=iteration, prev_focus=None,
        is_defer=False, d6_fired=False, absence_forced=False, counts=dict(counts or {}),
        formulated_count=1, tenant_id="t", investigation_id="i", loop_version=pin,
    )
    return apply_post_reason_cascade(state)


def _feed(*gaps, iteration=2):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.worker.workflows.forcing import _carry_gap_orders

    carrier = _carry_gap_orders({"claim_gaps": list(gaps)}, iteration)
    assert carrier is not None
    return carrier


def _gap(gap_id="g1", kind="DEFINITION", target=None, **over):  # type: ignore[no-untyped-def]
    gap = {"gap_id": gap_id, "kind": kind, "claim_ref": "ck",
           "target": target or {"file_path": "svc/gap.py"}}
    gap.update(over)
    return gap


def test_cited_no_target_that_fails_is_refetched() -> None:
    from test_tranche6_verdict_semantics import _with_assessments

    read = _def_read()
    base = _def_hyp(read)
    from investigation_agent_platform.domain.hypothesis.models import (
        AssessmentStrength,
        AssessmentType,
        HypothesisEvidenceAssessment,
    )

    asm = HypothesisEvidenceAssessment(
        hypothesis_id=base.id, evidence_id=read.evidence_id,
        assessment=AssessmentType.SUPPORTS, strength=AssessmentStrength.MODERATE,
        reason="t", claim_kind=ClaimKind.CAUSAL, claim_target=ClaimTarget(),
    )
    hyp = _with_assessments(base, [asm])
    engine = VerificationEngine()
    from investigation_agent_platform.application.investigation.verification import (
        RootCauseVerificationPolicy,
    )

    failed = engine.failed_cited_group_ids(
        hyp, SimpleNamespace(status="INCONCLUSIVE"), [read], [], [], RootCauseVerificationPolicy(),
        include_no_target=True,
    )
    assert any(f.startswith("CAUSAL|NO_TARGET") for f in failed), failed
    out = build_claim_gaps(hyp, {"CAUSAL|NO_TARGET"}, set(failed), include_no_target=True)
    reasons = [g.get("reason") for g in out["gaps"]]
    assert "cited_gate_failed" in reasons


def _cascade(history, iteration=4, counts=None, pin="lv9"):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.worker.workflows.forcing import (
        PostReasonCascadeState,
        apply_post_reason_cascade,
    )

    state = PostReasonCascadeState(
        reason_data={"next_action": "FORMULATE_HYPOTHESIS"}, action_history=list(history),
        evidence_banked_total=9, triage_route="BUG", description="d", knowledge_seed={},
        max_iterations=10, iteration=iteration, prev_focus=None,
        is_defer=False, d6_fired=False, absence_forced=False, counts=dict(counts or {}),
        formulated_count=1, tenant_id="t", investigation_id="i", loop_version=pin,
    )
    return apply_post_reason_cascade(state)


def _feed(*gaps, iteration=2):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.worker.workflows.forcing import _carry_gap_orders

    carrier = _carry_gap_orders({"claim_gaps": list(gaps)}, iteration)
    assert carrier is not None
    return carrier


def _gap(gap_id="g1", kind="DEFINITION", target=None, **over):  # type: ignore[no-untyped-def]
    gap = {"gap_id": gap_id, "kind": kind, "claim_ref": "ck",
           "target": target or {"file_path": "svc/gap.py"}}
    gap.update(over)
    return gap
