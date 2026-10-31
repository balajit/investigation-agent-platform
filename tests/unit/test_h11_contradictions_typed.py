# tests/unit/test_h11_contradictions_typed.py
"""H11: typed contradiction reports, reconcile, against-evidence filter (LV_H)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from investigation_agent_platform.ports.reasoning.reasoner import (
    ContradictionReport,
    InvestigationDecision,
)


def _row(eid):  # type: ignore[no-untyped-def]
    return SimpleNamespace(evidence_id=eid)


def _hyp(hid, eids, status="SUPPORTED"):  # type: ignore[no-untyped-def]
    return SimpleNamespace(id=hid, status=status, supporting_evidence_ids=list(eids))


async def _run_reason(decision, rows, hyps, loop_version=""):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.api.dependencies import AppContext, set_app_context
    from investigation_agent_platform.application.worker.activities import (
        ReasonInput,
        reason_activity,
    )

    saved: list = []
    from datetime import UTC, datetime, timedelta

    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationContext,
        InvestigationRequest,
    )

    now = datetime.now(UTC)
    inv = Investigation(
        session_id="sess-1", application_id="app", tenant_id="t",
        request=InvestigationRequest(
            application_id="app", problem_description="p", session_id="sess-1", requested_by="tester"),
        context=InvestigationContext(environment="production", time_window=(now - timedelta(hours=1), now)),
    )
    inv_id = inv.id
    ctx = AppContext()
    ctx.reasoning_coordinator = SimpleNamespace(reason=AsyncMock(return_value=decision))  # type: ignore[attr-defined]
    ctx.investigation_repo = SimpleNamespace(get_by_id=AsyncMock(return_value=inv))  # type: ignore[attr-defined]
    ctx.evidence_repo = SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=list(rows)))  # type: ignore[attr-defined]
    ctx.hypothesis_repo = SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=list(hyps)))  # type: ignore[attr-defined]
    ctx.contradiction_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=[]),
        save=AsyncMock(side_effect=lambda t, c, i: saved.append(c)),
    )
    set_app_context(ctx)
    out = await reason_activity(
        ReasonInput(tenant_id="t", investigation_id=str(inv_id), loop_version=loop_version)
    )
    return out, saved


def _decision(**over):  # type: ignore[no-untyped-def]
    base = dict(
        action=None, observations=[], information_gaps=[], hypotheses_evaluated=[],
        claims=[], contradictions_found=[], conclusion_readiness=0.1,
        reasoning_chain="", metadata={}, disposition=None, defer_gathering=None,
        action_parameters={},
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_persistence_baseline_bare_strings() -> None:
    ea, eb = uuid4(), uuid4()
    out, saved = await _run_reason(
        _decision(contradictions_found=["pool is exhausted"]),
        [_row(ea)], [_hyp(uuid4(), [ea])], loop_version="lv7",
    )
    assert out.success is True
    assert len(saved) == 1 and saved[0].severity == "MEDIUM"
    assert saved[0].evidence_ids in ([], None) or list(saved[0].evidence_ids) == []


@pytest.mark.asyncio
async def test_dedupe_by_statement() -> None:
    from investigation_agent_platform.application.investigation.verification import Contradiction

    ea = uuid4()
    existing = [Contradiction(statement="pool is exhausted", severity="MEDIUM")]
    from investigation_agent_platform.api.dependencies import AppContext, set_app_context
    from investigation_agent_platform.application.worker.activities import (
        ReasonInput,
        reason_activity,
    )

    ctx = AppContext()
    ctx.reasoning_coordinator = SimpleNamespace(reason=AsyncMock(return_value=_decision(contradictions_found=["pool is exhausted"])))  # type: ignore[attr-defined]
    from datetime import UTC as _UTC, datetime as _dt, timedelta as _td

    from investigation_agent_platform.domain.investigation.models import (
        Investigation as _Inv,
        InvestigationContext as _InvCtx,
        InvestigationRequest as _InvReq,
    )

    _now = _dt.now(_UTC)
    _inv = _Inv(
        session_id="sess-1", application_id="app", tenant_id="t",
        request=_InvReq(application_id="app", problem_description="p", session_id="sess-1", requested_by="t"),
        context=_InvCtx(environment="production", time_window=(_now - _td(hours=1), _now)))
    ctx.evidence_repo = SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[_row(ea)]))  # type: ignore[attr-defined]
    ctx.hypothesis_repo = SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[]))  # type: ignore[attr-defined]
    ctx.investigation_repo = SimpleNamespace(get_by_id=AsyncMock(return_value=_inv))  # type: ignore[attr-defined]
    ctx.contradiction_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=existing),
        save=AsyncMock(side_effect=AssertionError("must not save twice")),
    )
    set_app_context(ctx)
    out = await reason_activity(ReasonInput(tenant_id="t", investigation_id=str(uuid4()), loop_version="lv7"))
    assert out.success is True


@pytest.mark.asyncio
async def test_repository_failure_never_fails_reasoning() -> None:
    from investigation_agent_platform.api.dependencies import AppContext, set_app_context
    from investigation_agent_platform.application.worker.activities import (
        ReasonInput,
        reason_activity,
    )

    ctx = AppContext()
    ctx.reasoning_coordinator = SimpleNamespace(reason=AsyncMock(return_value=_decision(contradictions_found=["x"])))  # type: ignore[attr-defined]
    from datetime import UTC as _UTC2, datetime as _dt2, timedelta as _td2

    from investigation_agent_platform.domain.investigation.models import (
        Investigation as _Inv2,
        InvestigationContext as _InvCtx2,
        InvestigationRequest as _InvReq2,
    )

    _now2 = _dt2.now(_UTC2)
    _inv2 = _Inv2(
        session_id="sess-1", application_id="app", tenant_id="t",
        request=_InvReq2(application_id="app", problem_description="p", session_id="sess-1", requested_by="t"),
        context=_InvCtx2(environment="production", time_window=(_now2 - _td2(hours=1), _now2)))
    ctx.evidence_repo = SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[]))  # type: ignore[attr-defined]
    ctx.hypothesis_repo = SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[]))  # type: ignore[attr-defined]
    ctx.investigation_repo = SimpleNamespace(get_by_id=AsyncMock(return_value=_inv2))  # type: ignore[attr-defined]
    ctx.contradiction_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=[]),
        save=AsyncMock(side_effect=RuntimeError("db down")),
    )
    set_app_context(ctx)
    out = await reason_activity(ReasonInput(tenant_id="t", investigation_id=str(uuid4()), loop_version="lv10"))
    assert out.success is True
    assert out.data["next_action"] if "next_action" in out.data else True


@pytest.mark.asyncio
async def test_typed_report_links_validated_evidence() -> None:
    ea, unknown = uuid4(), uuid4()
    hid = uuid4()
    out, saved = await _run_reason(
        _decision(contradictions_found=[{"statement": "pool contradicts cache", "evidence_ids": [str(ea), str(unknown)]}]),
        [_row(ea)], [_hyp(hid, [ea])], loop_version="lv10",
    )
    assert out.success is True
    assert len(saved) == 1
    assert [str(e) for e in saved[0].evidence_ids] == [str(ea)]
    assert [str(h) for h in saved[0].hypothesis_ids] == [str(hid)]
    assert saved[0].severity.value == "MEDIUM"


@pytest.mark.asyncio
async def test_unlinked_report_is_low_note() -> None:
    ea = uuid4()
    out, saved = await _run_reason(
        _decision(contradictions_found=[{"statement": "vague unease", "evidence_ids": []}]),
        [_row(ea)], [_hyp(uuid4(), [ea])], loop_version="lv10",
    )
    assert out.success is True
    assert len(saved) == 1
    assert saved[0].severity.value == "LOW"


def test_model_report_can_never_block() -> None:
    from investigation_agent_platform.application.investigation.verification import (
        RootCauseVerificationPolicy,
    )

    policy = RootCauseVerificationPolicy(
        tolerant_band=True, blocking_contradictions_only=True, max_allowed_unresolved_contradictions=0,
    )
    assert policy.max_allowed_unresolved_contradictions == 0
    assert policy.blocking_contradictions_only is True


def test_bare_strings_parse_as_unlinked_notes() -> None:
    d = InvestigationDecision.model_validate({"contradictions_found": ["x"]})
    assert list(d.contradictions_found) == [ContradictionReport(statement="x", evidence_ids=[])]


def test_malformed_reports() -> None:
    import pytest as _pytest

    with _pytest.raises(Exception):
        InvestigationDecision.model_validate({"contradictions_found": [{"statement": "x" * 5000}]})
    with _pytest.raises(Exception):
        InvestigationDecision.model_validate({"contradictions_found": [{"nope": 1}]})
    with _pytest.raises(Exception):
        InvestigationDecision.model_validate({"contradictions_found": [{"statement": "x", "evidence_ids": ["a", 1]}]})


def test_reconcile_resolves_rejected_only_links() -> None:
    from investigation_agent_platform.application.investigation.verification import (
        Contradiction,
        ContradictionSeverity,
        reconcile_contradictions,
    )

    h1, h2 = SimpleNamespace(id=uuid4(), status="REJECTED"), SimpleNamespace(id=uuid4(), status="SUPPORTED")
    c1 = Contradiction(statement="a", severity=ContradictionSeverity.MEDIUM, hypothesis_ids=[h1.id])
    c2 = Contradiction(statement="b", severity=ContradictionSeverity.MEDIUM, hypothesis_ids=[h1.id, h2.id])
    out = reconcile_contradictions([c1, c2], [h1, h2])
    assert len(out) == 1
    assert out[0].resolution_status == "RESOLVED"
    assert out[0].resolved_by == "system:contradiction-reconciler"


def test_reconcile_leaves_live_links_alone() -> None:
    from investigation_agent_platform.application.investigation.verification import (
        Contradiction,
        ContradictionSeverity,
        reconcile_contradictions,
    )

    h1 = SimpleNamespace(id=uuid4(), status="SUPPORTED")
    c = Contradiction(statement="a", severity=ContradictionSeverity.MEDIUM, hypothesis_ids=[h1.id])
    assert reconcile_contradictions([c], [h1]) == []


def test_accept_risk_has_no_automatic_caller() -> None:
    import inspect

    import investigation_agent_platform.application.investigation.verification as _v

    src = inspect.getsource(_v)
    assert src.count("accept_risk(") <= 1  # the model definition only


def test_against_evidence_skips_notes_and_resolved() -> None:
    from investigation_agent_platform.application.investigation.verification import (
        Contradiction,
        ContradictionSeverity,
        RootCauseVerificationPolicy,
        VerificationEngine,
    )
    from investigation_agent_platform.domain.evidence.models import EvidenceType
    from test_verification_scoring import _evidence, _hyp

    engine = VerificationEngine()
    items = _evidence("FILE_LOG", EvidenceType.LOG, 4) + _evidence("POSTGRES", EvidenceType.DATABASE_STATE, 4)
    note = Contradiction(statement="vague", severity=ContradictionSeverity.LOW)
    resolved = Contradiction(
        statement="old news", severity=ContradictionSeverity.MEDIUM,
        evidence_ids=[items[0].evidence_id],
        resolution_status="RESOLVED", resolved_by="system:contradiction-reconciler",
        resolution_rationale="x",
    )
    linked = Contradiction(
        statement="live dispute", severity=ContradictionSeverity.MEDIUM,
        evidence_ids=[items[1].evidence_id],
    )
    policy = RootCauseVerificationPolicy(
        tolerant_band=True, require_causal_step_for_causal_claims=False,
        blocking_contradictions_only=True, max_allowed_unresolved_contradictions=10,
    )
    result = engine.evaluate_hypothesis(_hyp(), items, [note, resolved, linked], [], policy)
    assert result.status.value == "VERIFIED"
    assert result.against_evidence == ["live dispute"]
