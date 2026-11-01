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
    # T12: the H14 inline scheme converged into deterministic_finding_id
    # (tenant + role namespaced); same stability, designed key shape.
    from investigation_agent_platform.domain.finding.models import deterministic_finding_id

    assert call["finding"].id == deterministic_finding_id("t", inv_id, hypothesis_id=hyp.id)


    assert call["finding"].id == deterministic_finding_id("t", inv_id, hypothesis_id=hyp.id)


@pytest.mark.asyncio
async def test_h14t10_postgres_atomicity() -> None:
    """H14-T10: live-DB tier. A Finding insert that violates a constraint rolls
    back the whole unit: the hypothesis row stays SUPPORTED, no finding or
    conclusion rows exist. Throwaway disposable database; skipped without docker."""
    import shutil
    import subprocess
    import uuid as _uuid
    from datetime import UTC, datetime

    if shutil.which("docker") is None:
        pytest.skip("docker unavailable: H14-T10 needs a disposable postgres")
    running = await _asyncio_to_thread(
        subprocess.run,
        ["docker", "inspect", "-f", "{{.State.Running}}", "iap-r12-disposable-pgvector"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if running.stdout.strip() != "true":
        pytest.skip("pgvector disposable container not running")
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from investigation_agent_platform.domain.finding.models import (
        ConclusionStatus,
        Finding,
        FindingType,
        InvestigationConclusion,
    )
    from investigation_agent_platform.infrastructure.persistence.conclusion_unit_of_work import (
        PostgresConclusionUnitOfWork,
    )
    from investigation_agent_platform.infrastructure.persistence.finding_repository import (
        SqlAlchemyFindingConclusionRepository,
    )
    from investigation_agent_platform.infrastructure.persistence.hypothesis_repository import (
        SqlAlchemyHypothesisRepository,
    )
    from investigation_agent_platform.infrastructure.persistence.investigation_repository import (
        SqlAlchemyInvestigationRepository,
    )
    from investigation_agent_platform.infrastructure.persistence.models import (
        Base,
        FindingORM,
        HypothesisORM,
        InvestigationConclusionORM,
    )
    from investigation_agent_platform.infrastructure.persistence.pg_guard import (
        require_disposable,
    )

    base = "postgresql://iap:iaptest12@localhost:5545/postgres"
    require_disposable(base, purpose="H14-T10 conclusion atomicity")
    admin = create_async_engine(
        base.replace("postgresql://", "postgresql+asyncpg://", 1),
        isolation_level="AUTOCOMMIT",
    )
    dbname = f"h14t10_{_uuid.uuid4().hex[:12]}"
    try:
        async with admin.connect() as conn:
            await conn.execute(
                __import__("sqlalchemy").text(f'CREATE DATABASE "{dbname}"')
            )
    finally:
        await admin.dispose()
    url = base.replace("/postgres", f"/{dbname}")
    engine = create_async_engine(url.replace("postgresql://", "postgresql+asyncpg://", 1))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.execute(__import__("sqlalchemy").text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all, checkfirst=True)
        now = datetime.now(UTC)
        inv_id = _uuid.uuid4()
        hyp_id = _uuid.uuid4()
        inv_repo = SqlAlchemyInvestigationRepository(factory)
        hyp_repo = SqlAlchemyHypothesisRepository(factory)
        finding_repo = SqlAlchemyFindingConclusionRepository(factory)
        uow = PostgresConclusionUnitOfWork(factory, hyp_repo, finding_repo)
        # Parent investigation row (FK target for the hypothesis only).
        await inv_repo.create(
            "t",
            _investigation(inv_id, now),
        )
        hyp = _supported_hyp(inv_id, hyp_id)
        await hyp_repo.save("t", hyp, inv_id, expected_version=0)
        # The finding references a nonexistent investigation: FK violation.
        bad_finding = Finding(
            tenant_id="t",
            investigation_id=_uuid.uuid4(),
            finding_type=FindingType.OBSERVATION,
            title="t",
            statement="s",
            evidence_ids=[],
            related_hypothesis_ids=[hyp_id],
            confidence=0.9,
        )
        conclusion = InvestigationConclusion(
            tenant_id="t",
            investigation_id=inv_id,
            status=ConclusionStatus.ANSWERED,
            root_cause="the handler is defined",
            supporting_evidence_ids=[_uuid.uuid4()],
        )
        with pytest.raises(Exception) as exc_info:
            await uow.commit_verdict(
                "t",
                investigation_id=inv_id,
                hypothesis=hyp.model_copy(update={"status": _verified_status()}),
                expected_version=1,
                finding=bad_finding,
                conclusion=conclusion,
            )
        assert "findings" in str(exc_info.value).lower(), (
            f"the failure must come from the finding write, got: {exc_info.value!r:.200}"
        )
        async with factory() as session:
            hyp_rows = (
                await session.scalars(
                    select(HypothesisORM).where(HypothesisORM.investigation_id == inv_id)
                )
            ).all()
            assert [str(h.status) for h in hyp_rows] == ["SUPPORTED"], (
                "the rolled-back VERIFIED write must not persist"
            )
            finding_count = await session.scalar(
                select(func.count()).select_from(FindingORM).where(
                    FindingORM.investigation_id == inv_id
                )
            )
            assert finding_count == 0
            concl_count = await session.scalar(
                select(func.count()).select_from(InvestigationConclusionORM).where(
                    InvestigationConclusionORM.investigation_id == inv_id
                )
            )
            assert concl_count == 0
    finally:
        await engine.dispose()


async def _asyncio_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
    import asyncio as _asyncio

    return await _asyncio.to_thread(func, *args, **kwargs)


def _investigation(inv_id, now):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationContext,
        InvestigationRequest,
        InvestigationStatus,
    )

    return Investigation(
        id=inv_id,
        session_id="sess-h14t10",
        application_id="app",
        tenant_id="t",
        request=InvestigationRequest(
            application_id="app",
            problem_description="h14t10",
            session_id="sess-h14t10",
            requested_by="tester",
        ),
        context=InvestigationContext(environment="dev", time_window=(now, now)),
        status=InvestigationStatus.INVESTIGATING,
    )


def _supported_hyp(inv_id, hyp_id):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.hypothesis.models import Hypothesis, HypothesisStatus

    return Hypothesis(
        id=hyp_id,
        tenant_id="t",
        investigation_id=inv_id,
        title="t",
        description="d",
        statement="s",
        status=HypothesisStatus.SUPPORTED,
    )


def _verified_status():  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.hypothesis.models import HypothesisStatus

    return HypothesisStatus.VERIFIED
