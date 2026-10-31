# tests/unit/test_h17_gate_integration.py
"""H17: gate-level cross-claim, binding-fault abstention, prefer-id routing (LV_H)."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
import test_claim_binding_phase1 as fixtures
from test_claim_binding_phase1 import (
    _activity_ctx,
    _attempt_event,
    _two_caller_hyp,
    _two_caller_rows,
)

from investigation_agent_platform.application.worker.activities.lifecycle import (
    _gate_conclusion_or_fail,
)


def _lv_h_policy():  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.investigation.verification import (
        RootCauseVerificationPolicy,
    )

    return RootCauseVerificationPolicy(cross_claim_checks=True)


@pytest.mark.asyncio
async def test_binding_fault_abstains_with_inspection_reason() -> None:
    row_a, row_b = _two_caller_rows()
    hyp = _two_caller_hyp(row_a, row_b)
    inv_id = uuid4()
    hyp = hyp.model_copy(update={"investigation_id": inv_id, "tenant_id": "t"})
    saved: dict = {}

    async def _save_conclusion(tenant_id, conclusion):  # type: ignore[no-untyped-def]
        saved["conclusion"] = conclusion

    async def _noop(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    class FaultEnforcement:
        fault = False

        def __init__(self):  # type: ignore[no-untyped-def]
            self._reads = 0

        def __getattr__(self, name):  # type: ignore[no-untyped-def]
            if name.startswith("__"):
                raise AttributeError(name)
            if name == "cited_ids":
                # the filter block reads cited ids once; the gate's blocks
                # computation reads them again and faults there (H17a)
                self._reads += 1
                if self._reads > 1:
                    raise RuntimeError("citation store down")
                return []
            if name in ("kept_ids",):
                return []
            if name in ("unmapped", "has_claim_local", "linkage_observed"):
                return False
            if name in ("cited_kept_count",):
                return 0
            if name in ("next_action", "missing_proof_kind"):
                return None
            return None

    import investigation_agent_platform.application.investigation.verification as _v

    orig = _v.VerificationEngine.claim_binding_enforcement

    def _faulty(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return FaultEnforcement()

    _v.VerificationEngine.claim_binding_enforcement = _faulty  # type: ignore[method-assign]
    try:
        ctx = _activity_ctx(
            {"claim_binding_version": "cbv3", "verifier_version": "v2", "loop_version": "lv7"},
            hyp, [row_a, row_b], [_attempt_event([str(row_a.evidence_id), str(row_b.evidence_id)])],
        )
        ctx.finding_repo = SimpleNamespace(
            save_conclusion=_save_conclusion, save=_noop, save_finding=_noop,
            save_finding_record=_noop,
        )
        status = await _gate_conclusion_or_fail("t", inv_id, ctx, shadow_notes=None, triage_route="")
    finally:
        _v.VerificationEngine.claim_binding_enforcement = orig  # type: ignore[method-assign]
    assert status is not None
    conclusion = saved.get("conclusion")
    assert conclusion is not None
    assert "inspection faulted" in " ".join(str(lim) for lim in (getattr(conclusion, "limitations", None) or []))


@pytest.mark.asyncio
@pytest.mark.parametrize("pin,expected", [("lv10", True), ("lv7", False)])
async def test_conclude_honors_preferred_id_only_on_lv_h(pin, expected) -> None:  # type: ignore[no-untyped-def]
    from test_claim_binding_phase3 import _def_hyp, _def_read

    read = _def_read()
    hyp = _def_hyp(read)
    inv_id = uuid4()
    hyp = hyp.model_copy(update={"investigation_id": inv_id, "tenant_id": "t"})
    saved: dict = {}

    async def _save_conclusion(tenant_id, conclusion):  # type: ignore[no-untyped-def]
        saved["conclusion"] = conclusion

    async def _noop(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    seen: dict = {}
    import investigation_agent_platform.application.investigation.verification as _vmod

    orig_select = _vmod.select_verifiable_candidate

    def _spy(ranked, evidence_by_id, exclude_statuses=("REJECTED", "CONTRADICTED"), prefer_id=None):  # type: ignore[no-untyped-def]
        seen["prefer_id"] = prefer_id
        return orig_select(ranked, evidence_by_id, exclude_statuses=exclude_statuses, prefer_id=prefer_id)

    ctx = fixtures._activity_ctx(
        {"claim_binding_version": "cbv1", "verifier_version": "v2", "loop_version": pin},
        hyp, [read], [],
    )
    ctx.finding_repo = SimpleNamespace(
        save_conclusion=_save_conclusion, save=_noop, save_finding=_noop,
        save_finding_record=_noop,
    )
    _vmod.select_verifiable_candidate = _spy  # type: ignore[method-assign]
    try:
        await _gate_conclusion_or_fail(
            "t", inv_id, ctx, shadow_notes=None, triage_route="",
            preferred_candidate_id=str(hyp.id),
        )
    finally:
        _vmod.select_verifiable_candidate = orig_select  # type: ignore[method-assign]
    assert ("prefer_id" in seen) is True
    assert (seen["prefer_id"] == str(hyp.id)) is expected


def test_workflow_passes_preferred_id() -> None:
    import inspect

    import investigation_agent_platform.application.worker.workflows.run_investigation as ri

    assert "preferred_candidate_id=last_verified_candidate_id" in inspect.getsource(ri)
    assert "last_verified_candidate_id = str(" in inspect.getsource(ri)
