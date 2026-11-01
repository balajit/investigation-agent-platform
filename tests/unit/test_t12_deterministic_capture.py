# tests/unit/test_t12_deterministic_capture.py
"""T12: D17 deterministic finding ids + D18 capture only what a conclusion earns.

D17: a conclude retry after a committed finding must be a no-op (same id),
and journey fusion must never fuse itself.
D18: FAILED runs capture nothing; abstentions and LIKELY verdicts stay
TENANT; only ANSWERED / ROOT_CAUSE_ESTABLISHED may share cross-tenant.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest


def _run(coro: Any) -> Any:
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


# --- D17: deterministic ids -------------------------------------------------


def test_t1202_id_is_stable_and_role_separated() -> None:
    from investigation_agent_platform.domain.finding.models import deterministic_finding_id

    inv, hyp = uuid4(), uuid4()
    a = deterministic_finding_id("t", inv, hypothesis_id=hyp)
    assert a == deterministic_finding_id("t", inv, hypothesis_id=hyp)
    assert a != deterministic_finding_id("t", inv, role="journey-fusion")
    assert a != deterministic_finding_id("t", inv, hypothesis_id=uuid4())
    assert a != deterministic_finding_id("t", uuid4(), hypothesis_id=hyp)
    assert a != deterministic_finding_id("other", inv, hypothesis_id=hyp)


def _wide_hyp():  # type: ignore[no-untyped-def]
    from test_h14_finding_persist_order import _hyp_with_wide_causal_chain

    return _hyp_with_wide_causal_chain()


async def _gate_twice(hyp, rows, inv_id):  # type: ignore[no-untyped-def]
    import test_claim_binding_phase1 as fixtures

    from investigation_agent_platform.application.worker.activities import lifecycle

    saved: dict = {"findings": []}

    async def _save_finding_record(tenant_id, finding):  # type: ignore[no-untyped-def]
        saved["findings"].append(finding)

    async def _save_conclusion(tenant_id, conclusion):  # type: ignore[no-untyped-def]
        saved["conclusion"] = conclusion

    async def _noop(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    ctx = fixtures._activity_ctx(
        {"claim_binding_version": "cbv1", "verifier_version": "v2"}, hyp, rows, []
    )
    ctx.finding_repo = SimpleNamespace(
        save_conclusion=_save_conclusion,
        save=_noop,
        save_finding=_noop,
        save_finding_record=_save_finding_record,
    )
    for _ in range(2):
        await lifecycle._gate_conclusion_or_fail("t", inv_id, ctx)
    return saved


@pytest.mark.asyncio
async def test_t1201_conclude_retry_saves_one_finding_id() -> None:
    hyp, rows = _wide_hyp()
    inv_id = uuid4()
    hyp = hyp.model_copy(update={"investigation_id": inv_id, "tenant_id": "t"})
    saved = await _gate_twice(hyp, rows, inv_id)
    assert len(saved["findings"]) == 2
    ids = {f.id for f in saved["findings"]}
    assert len(ids) == 1, f"retry must reuse the finding id, got {ids}"


def _observation(inv_id, statement="step note"):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.finding.models import Finding, FindingType

    return Finding(
        tenant_id="t",
        investigation_id=inv_id,
        finding_type=FindingType.OBSERVATION,
        title="journey step",
        statement=statement,
        evidence_ids=[uuid4()],
        confidence=0.8,
    )


def _journey_row(inv_id):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
    from investigation_agent_platform.domain.provenance.models import (
        EvidenceFreshness,
        EvidenceProvenance,
        QueryFingerprint,
        SourceLocation,
    )

    now = datetime.now(tz=datetime.now().astimezone().tzinfo)
    return Evidence(
        tenant_id="t",
        investigation_id=inv_id,
        evidence_type=EvidenceType.JOURNEY,
        provider="test",
        source="test://journey/1",
        title="j",
        summary="session spine",
        observed_at=now,
        retrieved_at=now,
        provenance=EvidenceProvenance(
            tenant_id="t",
            investigation_id=inv_id,
            provider_type="TEST",
            requested_provider_id="test",
            actual_provider_id="test",
            source_system="test",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="TEST", operation="test", normalized_query_hash="abc"
            ),
            source_location=SourceLocation(system="test", identifier="test://journey/1"),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint="fp-j-1",
    )


async def _fuse_twice(obs_rows, evidence_rows):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.worker.activities.execution import (
        _fuse_journey_findings,
    )

    inv_id = obs_rows[0].investigation_id
    stored: list = list(obs_rows)

    async def _list(tenant_id, investigation_id, limit=50):  # type: ignore[no-untyped-def]
        return list(stored), len(stored)

    async def _save(tenant_id, finding):  # type: ignore[no-untyped-def]
        stored.append(finding)

    finding_repo = SimpleNamespace(list_findings=_list, save_finding_record=_save)
    evidence_repo = SimpleNamespace(
        find_by_investigation_id=AsyncMock(return_value=list(evidence_rows))
    )
    fused_ids: list = []
    orig_save = _save

    async def _save_and_track(tenant_id, finding):  # type: ignore[no-untyped-def]
        await orig_save(tenant_id, finding)
        fused_ids.append(finding.id)

    finding_repo.save_finding_record = _save_and_track  # type: ignore[attr-defined]
    for _ in range(2):
        await _fuse_journey_findings(
            tenant_id="t",
            investigation_id=inv_id,
            finding_repo=finding_repo,
            evidence_repo=evidence_repo,
        )
    return stored, fused_ids


@pytest.mark.asyncio
async def test_t1204_fusion_does_not_fuse_itself_or_repeat() -> None:
    inv_id = uuid4()
    obs = [_observation(inv_id, "step one"), _observation(inv_id, "step two")]
    stored, fused_ids = await _fuse_twice(obs, [_journey_row(inv_id)])
    assert len(fused_ids) == 2
    assert fused_ids[0] == fused_ids[1], "retry must reuse the fusion id"
    fused = [f for f in stored if f.id == fused_ids[0]]
    assert len(fused) == 2  # the row is still one logical row, saved twice (do-nothing)
    assert "across 2 journey observations" in fused[1].title


@pytest.mark.asyncio
async def test_t1205_fusion_without_a_journey_spine_is_a_no_op() -> None:
    inv_id = uuid4()
    obs = [_observation(inv_id, "step one"), _observation(inv_id, "step two")]
    stored, fused_ids = await _fuse_twice(obs, [])
    assert fused_ids == []
    assert len(stored) == 2


# --- D18: capture only what a conclusion earns ------------------------------


def _capture_service(  # type: ignore[no-untyped-def]
    monkeypatch,
    *,
    conclusion_status=None,
    conclusion_raises=False,
    fingerprint="hk1:test-fingerprint",
    seed=True,
    opt_in=True,
):
    from investigation_agent_platform.api.dependencies import InMemoryArtifactRepository
    from investigation_agent_platform.application.knowledge.capture import (
        KnowledgeCaptureService,
    )

    inv_id = uuid4()

    async def _find_evidence(tenant_id, investigation_id):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.domain.evidence.models import (
            Evidence,
            EvidenceType,
        )
        from investigation_agent_platform.domain.provenance.models import (
            EvidenceFreshness,
            EvidenceProvenance,
            QueryFingerprint,
            SourceLocation,
        )

        now = datetime.now(tz=datetime.now().astimezone().tzinfo)
        return [
            Evidence(
                tenant_id="t",
                investigation_id=inv_id,
                evidence_type=EvidenceType.SOURCE_CODE,
                provider="test",
                source="test://code/1",
                title="c",
                summary="OrderServiceHandler defines handleRequest",
                observed_at=now,
                retrieved_at=now,
                provenance=EvidenceProvenance(
                    tenant_id="t",
                    investigation_id=inv_id,
                    provider_type="TEST",
                    requested_provider_id="test",
                    actual_provider_id="test",
                    source_system="test",
                    retrieval_timestamp=now,
                    query_fingerprint=QueryFingerprint(
                        provider_type="TEST", operation="test", normalized_query_hash="abc"
                    ),
                    source_location=SourceLocation(system="test", identifier="test://code/1"),
                ),
                freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
                fingerprint="fp-ev-1",
            )
        ]

    investigation = SimpleNamespace(
        code_issue_fingerprint=fingerprint,
        application_id="app-1",
        metadata=(
            {
                "intake_seed": {
                    "error_class": "NullPointer",
                    "failing_symbol": "OrderService.handle",
                    "repository": "svc",
                    "revision": "abc123",
                }
            }
            if seed
            else {}
        ),
    )

    async def _get_conclusion(tenant_id, investigation_id):  # type: ignore[no-untyped-def]
        if conclusion_raises:
            raise RuntimeError("finding store down")
        if conclusion_status is None:
            return None
        return SimpleNamespace(status=conclusion_status)

    async def _opt_in(profile_repo, tenant_id, application_id):  # type: ignore[no-untyped-def]
        return opt_in

    import investigation_agent_platform.application.knowledge.sharing as _sharing

    monkeypatch.setattr(_sharing, "sharing_opted_in", _opt_in)
    service = KnowledgeCaptureService(
        artifact_repo=InMemoryArtifactRepository(),
        evidence_repo=SimpleNamespace(find_by_investigation_id=_find_evidence),
        investigation_repo=SimpleNamespace(get_by_id=AsyncMock(return_value=investigation)),
        hypothesis_repo=None,
        finding_repo=(
            None
            if conclusion_status == "NO_REPO"
            else SimpleNamespace(get_conclusion=_get_conclusion)
        ),
    )
    return service, inv_id


@pytest.mark.asyncio
async def test_t1206_capture_skips_a_failed_run(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    service, inv_id = _capture_service(monkeypatch, conclusion_status=None)
    stored = await service.capture_for_investigation("t", inv_id)
    assert stored == []


@pytest.mark.asyncio
async def test_t1207_abstention_captures_tenant_only(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.knowledge.models import ArtifactVisibility

    service, inv_id = _capture_service(monkeypatch, conclusion_status="INSUFFICIENT_EVIDENCE")
    stored = await service.capture_for_investigation("t", inv_id)
    assert stored, "evidence summaries still land for the same tenant"
    assert all(a.visibility == ArtifactVisibility.TENANT for a in stored)
    assert any(a.kind == "evidence_summary" for a in stored)


@pytest.mark.asyncio
async def test_t1208_a_shareable_verdict_still_shares(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.knowledge.models import ArtifactVisibility

    service, inv_id = _capture_service(monkeypatch, conclusion_status="ANSWERED")
    stored = await service.capture_for_investigation("t", inv_id)
    shared = [a for a in stored if a.visibility == ArtifactVisibility.SHARED_CODE_ISSUE]
    assert len(shared) == 1 and shared[0].kind == "error_signature"


@pytest.mark.asyncio
async def test_t1209_likely_is_tenant_only(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.knowledge.models import ArtifactVisibility

    service, inv_id = _capture_service(monkeypatch, conclusion_status="ROOT_CAUSE_LIKELY")
    stored = await service.capture_for_investigation("t", inv_id)
    assert stored
    assert all(a.visibility == ArtifactVisibility.TENANT for a in stored)


@pytest.mark.asyncio
async def test_t1210_unreadable_conclusion_fails_closed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    service, inv_id = _capture_service(monkeypatch, conclusion_raises=True)
    stored = await service.capture_for_investigation("t", inv_id)
    assert stored == []


@pytest.mark.asyncio
async def test_t1211_no_finding_repo_keeps_today_capture_unshared(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.knowledge.models import ArtifactVisibility

    service, inv_id = _capture_service(monkeypatch, conclusion_status="NO_REPO")
    stored = await service.capture_for_investigation("t", inv_id)
    assert stored
    assert all(a.visibility == ArtifactVisibility.TENANT for a in stored)


@pytest.mark.asyncio
async def test_t1203_real_repository_dedupes_on_the_id() -> None:
    """T12.03: `iap_test` DB tier (opt-in). Same Finding twice, one row."""
    import shutil
    import subprocess
    import uuid as _uuid

    if shutil.which("docker") is None:
        pytest.skip("docker unavailable: T12.03 needs a disposable postgres")
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
        Finding,
        FindingType,
        deterministic_finding_id,
    )
    from investigation_agent_platform.infrastructure.persistence.finding_repository import (
        SqlAlchemyFindingConclusionRepository,
    )
    from investigation_agent_platform.infrastructure.persistence.models import (
        Base,
        FindingORM,
    )
    from investigation_agent_platform.infrastructure.persistence.pg_guard import (
        require_disposable,
    )

    base = "postgresql://iap:iaptest12@localhost:5545/postgres"
    require_disposable(base, purpose="T12.03 finding idempotency")
    admin = create_async_engine(
        base.replace("postgresql://", "postgresql+asyncpg://", 1),
        isolation_level="AUTOCOMMIT",
    )
    dbname = f"t1203_{_uuid.uuid4().hex[:12]}"
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
        inv_id = _uuid.uuid4()
        hyp_id = _uuid.uuid4()
        from datetime import UTC, datetime

        from investigation_agent_platform.domain.investigation.models import (
            Investigation,
            InvestigationContext,
            InvestigationRequest,
            InvestigationStatus,
        )
        from investigation_agent_platform.infrastructure.persistence.investigation_repository import (
            SqlAlchemyInvestigationRepository,
        )

        now = datetime.now(UTC)
        await SqlAlchemyInvestigationRepository(factory).create(
            "t",
            Investigation(
                id=inv_id,
                session_id="sess-t1203",
                application_id="app",
                tenant_id="t",
                request=InvestigationRequest(
                    application_id="app",
                    problem_description="t1203",
                    session_id="sess-t1203",
                    requested_by="tester",
                ),
                context=InvestigationContext(environment="dev", time_window=(now, now)),
                status=InvestigationStatus.INVESTIGATING,
            ),
        )
        finding = Finding(
            id=deterministic_finding_id("t", inv_id, hypothesis_id=hyp_id),
            tenant_id="t",
            investigation_id=inv_id,
            finding_type=FindingType.OBSERVATION,
            title="t",
            statement="s",
            evidence_ids=[],
            related_hypothesis_ids=[hyp_id],
            confidence=0.9,
        )
        repo = SqlAlchemyFindingConclusionRepository(factory)
        await repo.save_finding_record("t", finding)
        await repo.save_finding_record("t", finding)
        async with factory() as session:
            count = await session.scalar(
                select(func.count()).select_from(FindingORM).where(FindingORM.id == finding.id)
            )
        assert count == 1
    finally:
        await engine.dispose()


async def _asyncio_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
    import asyncio as _asyncio

    return await _asyncio.to_thread(func, *args, **kwargs)

