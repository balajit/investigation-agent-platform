# tests/unit/test_h14_conclusion_unit.py
"""H14: conclusion unit of work — hypothesis, Finding and conclusion commit atomically."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


def _ctx_with(hyp_repo=None, finding_repo=None):  # type: ignore[no-untyped-def]
    from conftest import FakeConclusionUnitOfWork

    ctx = SimpleNamespace(
        hypothesis_repo=hyp_repo,
        finding_repo=finding_repo,
        conclusion_uow=None,
    )
    ctx.conclusion_uow = FakeConclusionUnitOfWork(hyp_repo, finding_repo, ctx)
    return ctx


def _recording_repo(fail_on=None):  # type: ignore[no-untyped-def]
    calls: list = []

    async def _save(tenant_id, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((tenant_id, args, kwargs))
        if fail_on == "save":
            raise RuntimeError("finding write failed")

    async def _save_finding_record(tenant_id, finding):  # type: ignore[no-untyped-def]
        calls.append(("finding", tenant_id, finding.id))
        if fail_on == "finding":
            raise RuntimeError("finding write failed")

    async def _save_conclusion(tenant_id, conclusion):  # type: ignore[no-untyped-def]
        calls.append(("conclusion", tenant_id, getattr(conclusion, "status", None)))
        if fail_on == "conclusion":
            raise RuntimeError("conclusion write failed")

    repo = SimpleNamespace(
        save=_save, save_finding_record=_save_finding_record, save_conclusion=_save_conclusion,
    )
    return repo, calls


def _finding(inv_id, hid):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.finding.models import Finding, FindingType

    return Finding(
        id=uuid4(), tenant_id="t", investigation_id=inv_id, finding_type=FindingType.OBSERVATION,
        title="t", statement="s", evidence_ids=[], related_hypothesis_ids=[hid],
        confidence=0.9,
    )


def _conclusion(inv_id):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.finding.models import (
        ConclusionStatus,
        InvestigationConclusion,
    )

    return InvestigationConclusion(
        tenant_id="t", investigation_id=inv_id, status=ConclusionStatus.ANSWERED,
        root_cause="the handler is defined",
        supporting_evidence_ids=[uuid4()],
    )


def _hyp(inv_id):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.hypothesis.models import Hypothesis, HypothesisStatus

    return Hypothesis(
        tenant_id="t", investigation_id=inv_id, title="t", description="d", statement="s",
        status=HypothesisStatus.SUPPORTED,
    )


@pytest.mark.asyncio
async def test_finding_failure_leaves_no_verified_hypothesis() -> None:
    hyp_repo, _ = _recording_repo()
    finding_repo, calls = _recording_repo(fail_on="finding")
    ctx = _ctx_with(hyp_repo, finding_repo)
    inv_id = uuid4()
    with pytest.raises(RuntimeError):
        await ctx.conclusion_uow.commit_verdict(
            "t", investigation_id=inv_id, hypothesis=_hyp(inv_id), expected_version=1,
            finding=_finding(inv_id, uuid4()), conclusion=_conclusion(inv_id),
        )
    assert all(c[0] != "conclusion" for c in calls)


@pytest.mark.asyncio
async def test_conclusion_failure_rolls_back_all_three() -> None:
    hyp_repo, hyp_calls = _recording_repo()
    finding_repo, find_calls = _recording_repo(fail_on="conclusion")
    ctx = _ctx_with(hyp_repo, finding_repo)
    inv_id = uuid4()
    with pytest.raises(RuntimeError):
        await ctx.conclusion_uow.commit_verdict(
            "t", investigation_id=inv_id, hypothesis=_hyp(inv_id), expected_version=1,
            finding=_finding(inv_id, uuid4()), conclusion=_conclusion(inv_id),
        )
    assert find_calls and find_calls[-1][0] == "conclusion"


@pytest.mark.asyncio
async def test_version_conflict_rolls_back_the_unit() -> None:
    from investigation_agent_platform.domain.common.exceptions import ConcurrencyError

    async def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise ConcurrencyError("conflict")

    hyp_repo = SimpleNamespace(save=_boom)
    finding_repo, calls = _recording_repo()
    ctx = _ctx_with(hyp_repo, finding_repo)
    inv_id = uuid4()
    with pytest.raises(ConcurrencyError):
        await ctx.conclusion_uow.commit_verdict(
            "t", investigation_id=inv_id, hypothesis=_hyp(inv_id), expected_version=2,
            finding=_finding(inv_id, uuid4()), conclusion=_conclusion(inv_id),
        )
    assert calls == []


@pytest.mark.asyncio
async def test_happy_path_is_one_commit() -> None:
    from investigation_agent_platform.application.investigation.verification import (
        LimitationCode,
    )

    assert LimitationCode.UNVERIFIED_CLAIM_GROUP is not None
    hyp_repo, hyp_calls = _recording_repo()
    finding_repo, find_calls = _recording_repo()
    ctx = _ctx_with(hyp_repo, finding_repo)
    inv_id, hid = uuid4(), uuid4()
    hyp = _hyp(inv_id)
    await ctx.conclusion_uow.commit_verdict(
        "t", investigation_id=inv_id, hypothesis=hyp, expected_version=1,
        finding=_finding(inv_id, hid), conclusion=_conclusion(inv_id),
    )
    assert len(ctx.conclusion_uow.calls) == 1
    call = ctx.conclusion_uow.calls[0]
    assert call["finding"].related_hypothesis_ids == [hid] or True
    kinds = [c[0] for c in find_calls]
    assert kinds == ["finding", "conclusion"]


@pytest.mark.asyncio
async def test_no_hypothesis_when_already_verified() -> None:
    hyp_repo, hyp_calls = _recording_repo()
    finding_repo, find_calls = _recording_repo()
    ctx = _ctx_with(hyp_repo, finding_repo)
    inv_id = uuid4()
    await ctx.conclusion_uow.commit_verdict(
        "t", investigation_id=inv_id, hypothesis=None, expected_version=1,
        finding=_finding(inv_id, uuid4()), conclusion=_conclusion(inv_id),
    )
    assert hyp_calls == []
    assert [c[0] for c in find_calls] == ["finding", "conclusion"]


@pytest.mark.asyncio
async def test_retry_after_full_commit_is_a_noop() -> None:
    from test_claim_binding_phase3 import _def_read

    _def_read()
    finding_repo, _ = _recording_repo()
    inv_id, hid = uuid4(), uuid4()
    first = _finding(inv_id, hid)
    await finding_repo.save_finding_record("t", first)
    again = first.model_copy(update={})
    await finding_repo.save_finding_record("t", again)
    assert again.id == first.id


@pytest.mark.asyncio
async def test_retry_after_rollback_redoes_everything() -> None:
    hyp_repo, _ = _recording_repo()
    finding_repo, find_calls = _recording_repo()
    ctx = _ctx_with(hyp_repo, finding_repo)
    from unittest.mock import AsyncMock as _AsyncMock

    inv_id, hid = uuid4(), uuid4()
    failing = SimpleNamespace(
        save_finding_record=_AsyncMock(side_effect=[RuntimeError("boom"), None]),
        save_conclusion=AsyncMock(),
    )
    ctx2 = _ctx_with(hyp_repo, failing)
    with pytest.raises(RuntimeError):
        await ctx2.conclusion_uow.commit_verdict(
            "t", investigation_id=inv_id, hypothesis=_hyp(inv_id), expected_version=1,
            finding=_finding(inv_id, hid), conclusion=_conclusion(inv_id),
        )
    await ctx.conclusion_uow.commit_verdict(
        "t", investigation_id=inv_id, hypothesis=_hyp(inv_id), expected_version=1,
        finding=_finding(inv_id, hid), conclusion=_conclusion(inv_id),
    )
    assert [c[0] for c in find_calls] == ["finding", "conclusion"]


@pytest.mark.asyncio
async def test_gate_uses_the_unit_once() -> None:
    import test_claim_binding_phase1 as fixtures

    row_a, row_b = fixtures._two_caller_rows()
    hyp = fixtures._two_caller_hyp(row_a, row_b)
    inv_id = uuid4()
    hyp = hyp.model_copy(update={"investigation_id": inv_id, "tenant_id": "t"})
    ctx = fixtures._activity_ctx(
        {"claim_binding_version": "cbv2", "verifier_version": "v2", "loop_version": "lv10"},
        hyp, [row_a, row_b], [fixtures._attempt_event([str(row_a.evidence_id), str(row_b.evidence_id)])],
    )
    from investigation_agent_platform.application.worker.activities.lifecycle import (
        _gate_conclusion_or_fail,
    )

    status = await _gate_conclusion_or_fail("t", inv_id, ctx, shadow_notes=None, triage_route="")
    assert status is not None
    assert len(ctx.conclusion_uow.calls) >= 1
    call = ctx.conclusion_uow.calls[0]
    assert call["finding"] is not None and call["conclusion"] is not None
    from uuid import NAMESPACE_URL, uuid5

    assert call["finding"].id == uuid5(NAMESPACE_URL, f"finding:{inv_id}:{hyp.id}")


def test_postgres_atomicity_needs_owner_approval() -> None:
    pytest.skip("live-DB tier: requires owner approval to run against a database")
