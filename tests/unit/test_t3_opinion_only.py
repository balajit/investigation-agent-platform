"""T3: D6 opinion-only fast path routes to an honest abstention (+T8 code half).

Covers the gate branch (T3.01-T3.07), the pure helpers (T3.08-T3.09),
the opinion join negative control target (T3.19 shape), the battery
grading of the D6 abstention label (T3.15), and the T8 code half
(T8.01-T8.05, T8.10). T3.17 is a negative control run manually, not kept.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from investigation_agent_platform.api.dependencies import AppContext, set_app_context
from investigation_agent_platform.domain.finding.models import (
    ConclusionStatus,
    InvestigationConclusion,
)
from investigation_agent_platform.domain.hypothesis.models import (
    AssessmentStrength,
    AssessmentType,
    ClaimKind,
    ClaimTarget,
    Hypothesis,
    HypothesisEvidenceAssessment,
    HypothesisStatus,
)
from investigation_agent_platform.domain.investigation.loop_version import (
    LOOP_VERSION_LVT,
)
from investigation_agent_platform.domain.investigation.models import (
    InvestigationStatus,
)

NC_STATEMENT = "the design feels over-engineered"
STALE_STATEMENT = "OrderServiceHandler is invoked by SampleOrderFilter"
STALE_PATH = "service/src/main/java/com/sample/orders/filter/SampleOrderFilter.java"


def _asm(hyp_id, kind, target=None):
    return HypothesisEvidenceAssessment(
        hypothesis_id=hyp_id,
        evidence_id=None,
        assessment=AssessmentType.NEUTRAL,
        strength=AssessmentStrength.WEAK,
        reason="taste judgment" if kind == ClaimKind.NOT_CHECKABLE else "read the code",
        claim_kind=kind,
        claim_target=target or ClaimTarget(),
    )


def _hyp(inv_id, statement, kind, target=None):
    hyp_id = uuid4()
    asm = _asm(hyp_id, kind, target) if kind is not None else None
    return Hypothesis(
        tenant_id="t",
        investigation_id=inv_id,
        title=statement[:200],
        description=statement,
        statement=statement,
        status=HypothesisStatus.PROPOSED,
        assessments=[asm] if asm is not None else [],
    )


def _stale_hyp(inv_id):
    return _hyp(
        inv_id,
        STALE_STATEMENT,
        ClaimKind.DEFINITION,
        ClaimTarget(file_path=STALE_PATH, symbol="SampleOrderFilter"),
    )


def _ctx(metadata, hyps):
    from datetime import UTC, datetime

    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationContext,
        InvestigationRequest,
    )

    saved: dict = {}

    async def _save_conclusion(tenant_id, conclusion):
        saved["conclusion"] = conclusion
        return conclusion

    now = datetime.now(UTC)
    inv_id = uuid4()
    inv = Investigation(
        id=inv_id,
        session_id="sess-t3",
        application_id="app",
        tenant_id="t",
        request=InvestigationRequest(
            application_id="app",
            problem_description="t3 probe",
            session_id="sess-t3",
            requested_by="tester",
        ),
        context=InvestigationContext(environment="dev", time_window=(now, now)),
        status=InvestigationStatus.INVESTIGATING,
        metadata=dict(metadata),
    )
    ctx = AppContext()
    ctx.investigation_repo = SimpleNamespace(  # type: ignore[attr-defined]
        get_by_id=AsyncMock(return_value=inv),
        save=AsyncMock(),
    )
    ctx.evidence_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=[])
    )
    ctx.hypothesis_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=list(hyps)),
        save=AsyncMock(),
    )
    ctx.finding_repo = SimpleNamespace(  # type: ignore[attr-defined]
        save_conclusion=AsyncMock(side_effect=_save_conclusion),
        save_finding_record=AsyncMock(return_value=None),
    )
    ctx.timeline_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=[]),
        append=AsyncMock(return_value=None),
    )
    set_app_context(ctx)
    return ctx, saved


async def _gate(metadata, hyps, statements):
    from investigation_agent_platform.application.worker.activities.lifecycle import (
        _gate_conclusion_or_fail,
    )

    inv_id = uuid4()
    ctx, saved = _ctx(metadata, hyps)
    status = await _gate_conclusion_or_fail(
        "t", inv_id, ctx, opinion_only_statements=list(statements)
    )
    return status, saved.get("conclusion")


def _lv13_metadata():
    return {"loop_version": LOOP_VERSION_LVT}


@pytest.mark.asyncio
async def test_t301_d6_with_nothing_persisted_abstains() -> None:
    status, conclusion = await _gate(_lv13_metadata(), [], [NC_STATEMENT])
    assert status == InvestigationStatus.COMPLETED
    assert conclusion is not None
    assert conclusion.status == ConclusionStatus.INSUFFICIENT_EVIDENCE
    assert list(conclusion.limitations or [])[:1] == ["NOT_CHECKABLE_SKIPPED"]
    assert list(conclusion.supporting_hypothesis_ids or []) == []


@pytest.mark.asyncio
async def test_t302_stale_checkable_selects_no_stale_candidate() -> None:
    inv_id = uuid4()
    stale = _stale_hyp(inv_id)
    status, conclusion = await _gate(_lv13_metadata(), [stale], [NC_STATEMENT])
    assert status == InvestigationStatus.COMPLETED
    assert conclusion is not None
    assert "NOT_CHECKABLE_SKIPPED" in list(conclusion.limitations or [])
    alternatives = list(conclusion.ranked_alternatives or [])
    assert alternatives, "the stale hypothesis must survive as handoff content"
    assert all(a.startswith("UNVERIFIED (") for a in alternatives)
    assert not any(a.startswith("PROPOSED:") for a in alternatives)
    assert list(conclusion.supporting_hypothesis_ids or []) == []
    handoff = conclusion.handoff
    assert handoff is not None
    assert handoff.next_fetch == f"GET_CODE {STALE_PATH} for SampleOrderFilter"


@pytest.mark.asyncio
async def test_t303_nc_plus_stale_picks_the_nc_candidate() -> None:
    inv_id = uuid4()
    stale = _stale_hyp(inv_id)
    nc = _hyp(inv_id, NC_STATEMENT, ClaimKind.NOT_CHECKABLE)
    status, conclusion = await _gate(_lv13_metadata(), [stale, nc], [NC_STATEMENT])
    assert status == InvestigationStatus.COMPLETED
    assert conclusion is not None
    assert "NOT_CHECKABLE_SKIPPED" in list(conclusion.limitations or [])


@pytest.mark.asyncio
async def test_t304_matching_nc_behaves_as_before() -> None:
    inv_id = uuid4()
    nc = _hyp(inv_id, NC_STATEMENT, ClaimKind.NOT_CHECKABLE)
    status, conclusion = await _gate(_lv13_metadata(), [nc], [NC_STATEMENT])
    assert status == InvestigationStatus.COMPLETED
    assert conclusion is not None
    assert "NOT_CHECKABLE_SKIPPED" in list(conclusion.limitations or [])


@pytest.mark.asyncio
async def test_t305_older_pin_keeps_the_old_behavior() -> None:
    for pin in ("lv7", "lv3"):
        status, conclusion = await _gate({"loop_version": pin}, [], [NC_STATEMENT])
        assert status == InvestigationStatus.FAILED
        assert conclusion is None


@pytest.mark.asyncio
async def test_t306_unmarked_metadata_resolves_to_current() -> None:
    status, conclusion = await _gate({}, [], [NC_STATEMENT])
    assert status == InvestigationStatus.COMPLETED
    assert conclusion is not None
    assert list(conclusion.limitations or [])[:1] == ["NOT_CHECKABLE_SKIPPED"]


@pytest.mark.asyncio
async def test_t307_no_statements_and_no_hypotheses_stays_failed() -> None:
    status, conclusion = await _gate(_lv13_metadata(), [], [])
    assert status == InvestigationStatus.FAILED
    assert conclusion is None


def test_t308_malformed_statements_are_bounded() -> None:
    from investigation_agent_platform.application.worker.opinion_only import (
        OPINION_STATEMENT_CAP,
        OPINION_STATEMENT_MAX_CHARS,
        opinion_statements,
    )

    raw = [{"statement": f"statement {i}"} for i in range(50)]
    raw.append({"statement": "x" * 5000})
    raw.append({"statement": 42})
    raw.append({"statement": "   "})
    out = opinion_statements({"hypotheses_evaluated": raw})
    assert len(out) == OPINION_STATEMENT_CAP
    assert all(len(s) <= OPINION_STATEMENT_MAX_CHARS for s in out)
    assert out[0] == "statement 0"
    assert opinion_statements({"hypotheses_evaluated": "not-a-list"}) == []
    assert opinion_statements(None) == []


def test_t309_normalization_parity() -> None:
    from investigation_agent_platform.application.worker.opinion_only import (
        normalize_statement,
    )
    from investigation_agent_platform.application.worker.workflows.forcing import (
        _norm_statement,
    )

    corpus = [
        "  SampleOrderFilter  ",
        "SAMPLEORDERFILTER",
        "ß",
        "STRASSE",
        "İ",
        "i̇",
        None,
        42,
        {"statement": "dict is not a string here"},
        "",
        "   ",
    ]
    for text in corpus:
        assert normalize_statement(text) == _norm_statement(text)


def test_t318_readiness_is_not_synthesized() -> None:
    # T3-D6: the D6 block must not write a synthetic 1.0 into the turn's
    # history entry. The harness cannot observe post-break history (no
    # activity carries it), so this pins the exact line D6 deleted: the
    # test it replaces (test_d6_concluded_requests_completed) was itself a
    # source-string pin. Negative control: re-add the assignment and this fails.
    from investigation_agent_platform.application.worker.workflows import (
        run_investigation,
    )

    src = Path(run_investigation.__file__).read_text(encoding="utf-8")
    assert 'conclusion_readiness"] = 1.0' not in src
    assert '_reason_data.get("conclusion_readiness")' in src


def test_t315_battery_grades_the_d6_abstention_label() -> None:
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "scripts"))
    from tranche_b_battery import load_cases, score_supported

    case = next(c for c in load_cases() if c.get("id") == "L13")
    verdict, _detail = score_supported(
        case,
        {
            "status": "INSUFFICIENT_EVIDENCE",
            "limitations": ["NOT_CHECKABLE_SKIPPED", "no bound supporting evidence"],
            "supporting_evidence_ids": [],
            "supporting_hypothesis_ids": [],
        },
        {},
        {},
        {},
    )
    assert verdict == "PASS"


def test_t801_abstention_is_not_linked_to_a_hypothesis() -> None:
    import asyncio

    async def _run() -> None:
        inv_id = uuid4()
        stale = _stale_hyp(inv_id)
        nc = _hyp(inv_id, NC_STATEMENT, ClaimKind.NOT_CHECKABLE)
        for hyps in ([nc], [stale], [stale, nc]):
            _, conclusion = await _gate(_lv13_metadata(), hyps, [NC_STATEMENT])
            assert conclusion is not None
            assert list(conclusion.supporting_hypothesis_ids or []) == []

    asyncio.run(_run())


def test_t802_alternatives_carry_the_unverified_label() -> None:
    import asyncio

    from investigation_agent_platform.domain.finding.models import (
        ConclusionStatus,
        classify_terminal_handoff,
    )

    async def _run() -> None:
        inv_id = uuid4()
        stale = _stale_hyp(inv_id)
        _, conclusion = await _gate(_lv13_metadata(), [stale], [NC_STATEMENT])
        assert conclusion is not None
        alternatives = list(conclusion.ranked_alternatives or [])
        assert alternatives
        assert all(a.startswith("UNVERIFIED (") for a in alternatives)
        assert not any(a.startswith("PROPOSED:") for a in alternatives)

    asyncio.run(_run())
    # The handoff line carries the same label (denial-free path: no
    # capability context, so the alternatives record is the first line).
    handoff = classify_terminal_handoff(
        status=ConclusionStatus.INSUFFICIENT_EVIDENCE,
        reason="insufficient",
        next_check="re-fetch",
        limitations=["NOT_CHECKABLE_SKIPPED"],
        ranked_alternatives=["UNVERIFIED (PROPOSED): the design"],
        eliminated_rivals=[],
        gap_fetch=None,
    )
    steps = list(handoff.recommended_next_steps or [])
    assert any(s.startswith("Ranked alternatives (unverified,") for s in steps)
    assert not any(s.startswith("Ranked alternatives: ") for s in steps)


def test_t803_verdict_alternatives_keep_status_labels() -> None:
    # The verdict path builds its own runner-up alternatives beside a
    # published verdict; T8 only relabels the abstention closure. The
    # verdict builder keeps the plain STATUS form.
    from investigation_agent_platform.application.worker.activities import lifecycle

    src = Path(lifecycle.__file__).read_text(encoding="utf-8")
    assert '_alternatives.append(f"{_status}: {_statement}"[:500])' in src
    assert src.count("UNVERIFIED ({_status})") == 1


def test_t804_epistemic_path_stays_clean() -> None:
    import asyncio

    from investigation_agent_platform.application.worker.activities.inputs import (
        ConcludeInvestigationInput,
    )
    from investigation_agent_platform.application.worker.activities.lifecycle import (
        conclude_investigation_activity,
    )

    async def _run() -> None:
        inv_id = uuid4()
        ctx, saved = _ctx({"claim_binding_version": "cbv3"}, [_hyp(inv_id, "s", None)])
        result = await conclude_investigation_activity(
            ConcludeInvestigationInput(
                tenant_id="t",
                investigation_id=str(inv_id),
                status="FAILED",
                reason="budget_exceeded",
            )
        )
        assert result.success is True
        conclusion = saved.get("conclusion")
        assert conclusion is not None
        assert list(conclusion.supporting_hypothesis_ids or []) == []

    asyncio.run(_run())


def test_t805_g1_ignores_hypothesis_links() -> None:
    import sys

    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "scripts"))
    from tranche_b_battery import score_g1

    satisfied, kind, _detail = score_g1(
        {
            "status": "INSUFFICIENT_EVIDENCE",
            "supporting_evidence_ids": [],
            "supporting_hypothesis_ids": ["9f9f9f9f-9f9f-9f9f-9f9f-9f9f9f9f9f9f"],
        },
        {"9f9f9f9f-9f9f-9f9f-9f9f-9f9f9f9f9f9f": {"evidence_id": "e1"}},
    )
    assert kind != "bound"
    assert satisfied is False


def test_t810_older_rows_still_load() -> None:
    # Compatibility is by tolerance of the data: an old-shape abstention
    # (STATUS: alternatives, a hypothesis link) rehydrates without error.
    data = {
        "tenant_id": "t",
        "investigation_id": uuid4(),
        "status": "INSUFFICIENT_EVIDENCE",
        "supporting_hypothesis_ids": [uuid4()],
        "ranked_alternatives": ["PROPOSED: the old prose"],
        "limitations": ["no bound supporting evidence"],
    }
    conclusion = InvestigationConclusion.model_validate(data)
    assert conclusion.ranked_alternatives == ["PROPOSED: the old prose"]


@pytest.mark.asyncio
async def test_t314_end_to_end_through_real_conclude() -> None:
    """T3.14: investigation row, no hypotheses, conclude input with the D6
    statements -> COMPLETED with an INSUFFICIENT_EVIDENCE marker conclusion.
    Pgvector disposable (port 5545: the full schema needs the vector type);
    skips when Docker is unavailable. No live services."""
    import asyncio as _asyncio
    import shutil
    import subprocess
    import uuid as _uuid
    from datetime import UTC, datetime

    from r12_disposable_db import dispose_engine
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from investigation_agent_platform.application.worker.activities.inputs import (
        ConcludeInvestigationInput,
    )
    from investigation_agent_platform.application.worker.activities.lifecycle import (
        conclude_investigation_activity,
    )
    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationContext,
        InvestigationRequest,
    )
    from investigation_agent_platform.infrastructure.persistence.evidence_repository import (
        SqlAlchemyEvidenceRepository,
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
    from investigation_agent_platform.infrastructure.persistence.models import Base
    from investigation_agent_platform.infrastructure.persistence.pg_guard import (
        require_disposable,
    )
    from investigation_agent_platform.infrastructure.persistence.timeline_repository import (
        SqlAlchemyTimelineRepository,
    )

    if shutil.which("docker") is None:
        pytest.skip("docker unavailable: T3.14 needs a disposable postgres")
    running = await _asyncio.to_thread(
        subprocess.run,
        ["docker", "inspect", "-f", "{{.State.Running}}", "iap-r12-disposable-pgvector"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if running.stdout.strip() != "true":
        pytest.skip("pgvector disposable container not running")
    base = "postgresql://iap:iaptest12@localhost:5545/postgres"
    require_disposable(base, purpose="T3.14 end-to-end conclude")
    admin = create_async_engine(
        base.replace("postgresql://", "postgresql+asyncpg://", 1),
        isolation_level="AUTOCOMMIT",
    )
    dbname = f"t314_{_uuid.uuid4().hex[:12]}"
    try:
        async with admin.connect() as conn:
            await conn.execute(
                __import__("sqlalchemy").text(f'CREATE DATABASE "{dbname}"')
            )
    finally:
        await dispose_engine(admin)
    url = base.replace("/postgres", f"/{dbname}")
    engine = create_async_engine(
        url.replace("postgresql://", "postgresql+asyncpg://", 1)
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.execute(__import__("sqlalchemy").text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all, checkfirst=True)
        now = datetime.now(UTC)
        inv = Investigation(
            session_id="sess-t314",
            application_id="app",
            tenant_id="t",
            request=InvestigationRequest(
                application_id="app",
                problem_description="t3 e2e",
                session_id="sess-t314",
                requested_by="tester",
            ),
            context=InvestigationContext(environment="dev", time_window=(now, now)),
            status=InvestigationStatus.INVESTIGATING,
            metadata={"loop_version": LOOP_VERSION_LVT},
        )
        ctx = AppContext(
            investigation_repo=SqlAlchemyInvestigationRepository(factory),
            hypothesis_repo=SqlAlchemyHypothesisRepository(factory),
            evidence_repo=SqlAlchemyEvidenceRepository(factory),
            timeline_repo=SqlAlchemyTimelineRepository(factory),
        )
        # AppContext hard-wires the finding store to in-memory; swap the
        # durable one in (the abstention path writes via finding_repo
        # directly, never through conclusion_uow).
        ctx.finding_repo = SqlAlchemyFindingConclusionRepository(factory)  # type: ignore[assignment]
        await ctx.investigation_repo.create("t", inv)
        set_app_context(ctx)
        result = await conclude_investigation_activity(
            ConcludeInvestigationInput(
                tenant_id="t",
                investigation_id=str(inv.id),
                status="COMPLETED",
                opinion_only_statements=[NC_STATEMENT.lower()],
            )
        )
        assert result.success is True
        stored = await ctx.investigation_repo.get_by_id("t", inv.id)
        assert stored is not None
        assert stored.status == InvestigationStatus.COMPLETED
        conclusion = await ctx.finding_repo.get_conclusion("t", inv.id)
        assert conclusion is not None
        assert conclusion.status == ConclusionStatus.INSUFFICIENT_EVIDENCE
        assert list(conclusion.limitations or [])[:1] == ["NOT_CHECKABLE_SKIPPED"]
        assert list(conclusion.supporting_hypothesis_ids or []) == []
    finally:
        await dispose_engine(engine)
        cleanup = create_async_engine(
            base.replace("postgresql://", "postgresql+asyncpg://", 1),
            isolation_level="AUTOCOMMIT",
        )
        try:
            import sqlalchemy as _sa

            async with cleanup.connect() as conn:
                await conn.execute(
                    _sa.text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :db AND pid <> pg_backend_pid()"
                    ),
                    {"db": dbname},
                )
                await conn.execute(_sa.text(f'DROP DATABASE IF EXISTS "{dbname}"'))
        except Exception:
            pass
        finally:
            await dispose_engine(cleanup)
