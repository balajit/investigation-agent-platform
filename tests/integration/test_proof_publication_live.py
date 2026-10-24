"""Live proof-publication suite: Review Stage C1+C2 on the dev database.

Gated: the whole module skips unless ``IAP_LIVE_TESTS=1`` is set with the
dev stack up (``scripts/start-platform.sh``). Uses the dev Postgres
directly (``IAP_DATABASE_URI``, overridable via
``IAP_PROOF_TEST_PG_URL``) on a dedicated tenant; every test cleans its
rows before and after itself, so reruns are idempotent. Each test gets a
fresh engine (function-scoped async fixture) so no connection ever
crosses event loops.

Covers, on the production-shaped path (real repos, real RLS, real
transactions through ``ProofPublicationService``):
C1 — derivation persist/reload round-trip, K6 invalidation over the
persisted graph, K5 alternative survival, K12 policy-version decisions,
K14 duplicate-premise rejection.
C2 — happy-path commit with lifecycle projections, interleaved
version-bump denial, evidence-append denial, unadjudicated-conflict
denial, forged/policy-denied inputs, idempotent recommit, and RLS
enforcement for the least-privilege role.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.skipif(
    os.environ.get("IAP_LIVE_TESTS", "").lower() not in ("1", "true", "yes"),
    reason="live proof-publication suite disabled: set IAP_LIVE_TESTS=1 with the dev stack running",
)

from investigation_agent_platform.application.proof.publication_commit_service import (
    CommitExpectation,
    LifecycleProjection,
    ProofPublicationService,
    RouteInput,
)
from investigation_agent_platform.domain.proof.certificate import _artifact_hash
from investigation_agent_platform.domain.proof.composition import (
    CompositionError,
    CompositionErrorCode,
    invalidate_by_evidence,
    invalidate_derivations,
    make_node,
)
from investigation_agent_platform.domain.proof.manifest import (
    ManifestCoverage,
    build_manifest,
)
from investigation_agent_platform.domain.proof.rules import RULE_REGISTRY
from investigation_agent_platform.infrastructure.persistence.models import (
    ContradictionORM,
    EvidenceORM,
    FindingORM,
    HypothesisORM,
    InvestigationConclusionORM,
    InvestigationORM,
    ProofDerivationORM,
    ProofPublicationCommitORM,
)
from investigation_agent_platform.infrastructure.persistence.proof_commit_repository import (
    evidence_fingerprint_digest,
    load_derivation_graph,
    store_derivation_nodes,
)
from investigation_agent_platform.infrastructure.persistence.rls import (
    rls_session,
)

TENANT = "proof-commit-tenant"
IMPL_DIGEST = RULE_REGISTRY["definition/v1"].implementation_digest


def _pg_url() -> str:
    url = os.environ.get("IAP_PROOF_TEST_PG_URL") or os.environ.get("IAP_DATABASE_URI", "")
    if not url:
        pytest.skip("no Postgres URL (IAP_DATABASE_URI / IAP_PROOF_TEST_PG_URL)")
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


async def _tables_present(factory: Any) -> bool:
    async with factory() as session:
        for table in ("proof_derivations", "proof_publication_commits"):
            exists = await session.execute(
                text("SELECT 1 FROM information_schema.tables WHERE table_name = :t"),
                {"t": table},
            )
            if exists.scalar() is None:
                return False
    return True


async def _clean_tenant(factory: Any) -> None:
    async with rls_session(factory, TENANT) as session:
        for model in (
            ProofPublicationCommitORM,
            ProofDerivationORM,
            ContradictionORM,
            FindingORM,
            InvestigationConclusionORM,
            HypothesisORM,
            EvidenceORM,
        ):
            await session.execute(delete(model).where(model.tenant_id == TENANT))
        await session.execute(delete(InvestigationORM).where(InvestigationORM.tenant_id == TENANT))


@pytest_asyncio.fixture()
async def live():
    engine = create_async_engine(_pg_url())
    maker = async_sessionmaker(engine, expire_on_commit=False)
    if not await _tables_present(maker):
        await engine.dispose()
        pytest.skip("proof tables missing: run alembic upgrade head on dev first")
    await _clean_tenant(maker)
    yield SimpleNamespace(engine=engine, factory=maker)
    await _clean_tenant(maker)
    await engine.dispose()


async def _investigation(live: Any) -> UUID:
    inv_id = uuid4()
    now = datetime.now(UTC)
    async with rls_session(live.factory, TENANT) as session:
        session.add(
            InvestigationORM(
                id=inv_id,
                tenant_id=TENANT,
                application_id="example-app",
                environment="test",
                title="proof commit probe",
                problem_description="c1c2",
                status="INVESTIGATING",
                phase="VERIFYING",
                created_by="live-suite",
                session_id=f"proof-{inv_id.hex[:8]}",
                created_at=now,
                updated_at=now,
            )
        )
    return inv_id


async def _hypothesis(live: Any, inv_id: UUID, statement: str = "defined(OrderService)") -> Any:
    async with rls_session(live.factory, TENANT) as session:
        row = HypothesisORM(
            tenant_id=TENANT,
            investigation_id=inv_id,
            title="probe",
            description="probe",
            statement=statement,
            status="SUPPORTED",
        )
        session.add(row)
        await session.flush()
        await session.refresh(row, attribute_names=["id", "version", "status"])
        return row


async def _hypothesis_version(live: Any, hyp_id: UUID) -> int:
    async with rls_session(live.factory, TENANT) as session:
        row = (
            (
                await session.execute(
                    select(HypothesisORM).where(
                        HypothesisORM.id == hyp_id, HypothesisORM.tenant_id == TENANT
                    )
                )
            )
            .scalars()
            .first()
        )
        assert row is not None
        return int(row.version)


async def _evidence(live: Any, inv_id: UUID, fingerprint: str, key: str) -> None:
    now = datetime.now(UTC)
    async with rls_session(live.factory, TENANT) as session:
        session.add(
            EvidenceORM(
                tenant_id=TENANT,
                investigation_id=inv_id,
                type="SOURCE_CODE",
                source_type="test",
                source_id=f"test://{key}",
                summary="probe",
                evidence_key=key,
                fingerprint=fingerprint,
                payload_json={},
                retrieved_at=now,
            )
        )


def _definition_route(
    symbol: str, window: str, premise_type: str = "RANGE_READ_FACT"
) -> RouteInput:
    artifact = {"file_path": "svc.py", "symbol": symbol, "text": window}
    digest = _artifact_hash(artifact)
    manifest = build_manifest(["row-1"], {"row-1": digest})
    proposition = {
        "canon_version": "canon/v1",
        "completeness": "PARTIAL_OK",
        "form": "EXISTENCE",
        "modality": "STATIC",
        "predicate": f"defined({symbol})",
        "quantifier": "EXISTENTIAL",
        "relation": "CALL_EDGE",
        "scope": {"configuration": "default", "execution": "n/a", "revision": "rev-live"},
        "target": {"file_path": "svc.py", "symbol": symbol},
    }
    premise = {
        "content_hash": digest,
        "evidence_id": "row-1",
        "premise_type": premise_type,
        "scope": {"revision": "rev-live"},
    }
    return RouteInput(
        proposition=proposition,
        manifest=manifest,
        premises=[premise],
        artifacts={"row-1": artifact},
        implementation_digest=IMPL_DIGEST,
    )


async def _expectation(live: Any, hyp: Any) -> CommitExpectation:
    async with rls_session(live.factory, TENANT) as session:
        digest = await evidence_fingerprint_digest(session, TENANT, hyp.investigation_id)
    return CommitExpectation(
        hypothesis_id=hyp.id, hypothesis_version=int(hyp.version), evidence_digest=digest
    )


def _lifecycle(symbol: str = "OrderService") -> LifecycleProjection:
    return LifecycleProjection(
        new_status="VERIFIED",
        finding={
            "finding_type": "ROOT_CAUSE",
            "title": f"{symbol} defined",
            "statement": f"{symbol} is defined",
            "severity": "MEDIUM",
            "confidence": 0.9,
            "evidence_ids": ["row-1"],
            "related_hypothesis_ids": [],
            "causal_chain": [],
            "remediation_steps": [],
        },
        conclusion={
            "status": "ROOT_CAUSE_ESTABLISHED",
            "root_cause": f"{symbol} is defined",
            "confidence": 0.9,
            "supporting_evidence_ids": ["row-1"],
            "supporting_hypothesis_ids": [],
            "contradicting_evidence_ids": [],
            "limitations": [],
            "recommended_next_steps": [],
            "verifier_version": "v2",
        },
    )


async def _counts(live: Any, inv_id: UUID) -> tuple[int, int, int, int]:
    async with rls_session(live.factory, TENANT) as session:
        commits = (
            await session.execute(
                select(func.count())
                .select_from(ProofPublicationCommitORM)
                .where(ProofPublicationCommitORM.investigation_id == inv_id)
            )
        ).scalar()
        nodes = (
            await session.execute(
                select(func.count())
                .select_from(ProofDerivationORM)
                .where(ProofDerivationORM.investigation_id == inv_id)
            )
        ).scalar()
        findings = (
            await session.execute(
                select(func.count())
                .select_from(FindingORM)
                .where(FindingORM.investigation_id == inv_id)
            )
        ).scalar()
        conclusions = (
            await session.execute(
                select(func.count())
                .select_from(InvestigationConclusionORM)
                .where(InvestigationConclusionORM.investigation_id == inv_id)
            )
        ).scalar()
    return int(commits or 0), int(nodes or 0), int(findings or 0), int(conclusions or 0)


@pytest.mark.asyncio
async def test_derivation_persist_reload_roundtrip(live: Any) -> None:
    inv_id = await _investigation(live)
    nodes = [
        dict(make_node("der-a", "definition/v1", ["row-1"], []).model_dump()),
        dict(make_node("der-b", "call_edge/v1", ["row-2"], ["der-a"]).model_dump()),
    ]
    async with rls_session(live.factory, TENANT) as session:
        assert await store_derivation_nodes(session, TENANT, inv_id, nodes) == 2
        graph = await load_derivation_graph(session, TENANT, inv_id)
    assert set(graph) == {"der-a", "der-b"}
    assert graph["der-b"]["depends_on"] == ["der-a"]


@pytest.mark.asyncio
async def test_k6_invalidation_over_persisted_graph(live: Any) -> None:
    inv_id = await _investigation(live)
    nodes = [
        dict(make_node("k6-a", "definition/v1", ["row-1"], []).model_dump()),
        dict(make_node("k6-b", "call_edge/v1", ["row-2"], ["k6-a"]).model_dump()),
        dict(make_node("k6-c", "call_edge/v1", ["row-3"], ["k6-b"]).model_dump()),
        dict(make_node("k6-d", "definition/v1", ["row-9"], []).model_dump()),
    ]
    async with rls_session(live.factory, TENANT) as session:
        await store_derivation_nodes(session, TENANT, inv_id, nodes)
        graph = await load_derivation_graph(session, TENANT, inv_id)
    assert invalidate_derivations({"k6-a"}, graph) == {"k6-a", "k6-b", "k6-c"}
    assert invalidate_by_evidence({"row-1"}, {"evidence.content"}, graph) == {
        "k6-a",
        "k6-b",
        "k6-c",
    }
    assert invalidate_by_evidence({"row-1"}, {"capability"}, graph) == set()


@pytest.mark.asyncio
async def test_k5_alternative_survival(live: Any) -> None:
    inv_id = await _investigation(live)
    hyp = await _hypothesis(live, inv_id)
    await _evidence(live, inv_id, "fp-k5", "k-k5")
    exp = await _expectation(live, hyp)
    svc = ProofPublicationService(live.factory)
    good = _definition_route("OrderService", "class OrderService:\n    pass\n")
    bad_artifact = dict(next(iter(good.artifacts.values())))
    bad_prop = dict(good.proposition)
    bad_prop["quantifier"] = "UNIVERSAL"
    bad_manifest = build_manifest(
        ["row-1"],
        {"row-1": _artifact_hash(bad_artifact)},
        coverage=ManifestCoverage.COMPLETE_EMPTY,
    )
    bad = RouteInput(
        proposition=bad_prop,
        manifest=bad_manifest,
        premises=list(good.premises),
        artifacts=dict(good.artifacts),
        implementation_digest=IMPL_DIGEST,
        universe="repo@rev:/:query=q",
    )
    denied = await svc.commit(TENANT, inv_id, exp, [good, bad], mode="all")
    assert denied.committed is False
    assert await _counts(live, inv_id) == (0, 0, 0, 0)
    ok = await svc.commit(
        TENANT, inv_id, exp, [good, bad], mode="any", reconciliation_adjudicated=True
    )
    assert ok.committed is True
    assert len(ok.derivation_ids) == 1
    commits, nodes, _, _ = await _counts(live, inv_id)
    assert (commits, nodes) == (1, 1)


@pytest.mark.asyncio
async def test_k12_policy_version_new_decision(live: Any) -> None:
    inv_id = await _investigation(live)
    hyp = await _hypothesis(live, inv_id)
    await _evidence(live, inv_id, "fp-k12", "k-k12")
    exp = await _expectation(live, hyp)
    svc = ProofPublicationService(live.factory)
    route = _definition_route("OrderService", "class OrderService:\n    pass\n")
    first = await svc.commit(
        TENANT, inv_id, exp, [route], gate_policy_version="gate/v1", reconciliation_adjudicated=True
    )
    assert first.committed is True
    exp2 = CommitExpectation(
        hypothesis_id=hyp.id,
        hypothesis_version=await _hypothesis_version(live, hyp.id),
        evidence_digest=exp.evidence_digest,
    )
    second = await svc.commit(
        TENANT,
        inv_id,
        exp2,
        [route],
        gate_policy_version="gate/v2",
        reconciliation_adjudicated=True,
    )
    assert second.committed is True
    assert second.commit_id != first.commit_id
    commits, _, _, _ = await _counts(live, inv_id)
    assert commits == 2


@pytest.mark.asyncio
async def test_commit_happy_path_with_projections(live: Any) -> None:
    inv_id = await _investigation(live)
    hyp = await _hypothesis(live, inv_id)
    await _evidence(live, inv_id, "fp-happy", "k-happy")
    exp = await _expectation(live, hyp)
    svc = ProofPublicationService(live.factory)
    route = _definition_route("OrderService", "class OrderService:\n    pass\n")
    out = await svc.commit(
        TENANT, inv_id, exp, [route], lifecycle=_lifecycle(), reconciliation_adjudicated=True
    )
    assert out.committed is True
    assert out.reason == "COMMITTED"
    assert await _hypothesis_version(live, hyp.id) == 2
    commits, nodes, findings, conclusions = await _counts(live, inv_id)
    assert (commits, nodes, findings, conclusions) == (1, 1, 1, 1)


@pytest.mark.asyncio
async def test_interleaved_version_bump_denied(live: Any) -> None:
    inv_id = await _investigation(live)
    hyp = await _hypothesis(live, inv_id)
    await _evidence(live, inv_id, "fp-race", "k-race")
    exp = await _expectation(live, hyp)
    async with rls_session(live.factory, TENANT) as session:
        row = await session.get(HypothesisORM, hyp.id)
        assert row is not None
        row.version = int(row.version) + 1
    svc = ProofPublicationService(live.factory)
    route = _definition_route("OrderService", "class OrderService:\n    pass\n")
    denied = await svc.commit(TENANT, inv_id, exp, [route], lifecycle=_lifecycle())
    assert denied.committed is False
    assert denied.reason == "STALE_VERSION"
    assert await _counts(live, inv_id) == (0, 0, 0, 0)
    assert await _hypothesis_version(live, hyp.id) == 2


@pytest.mark.asyncio
async def test_evidence_append_denied(live: Any) -> None:
    inv_id = await _investigation(live)
    hyp = await _hypothesis(live, inv_id)
    await _evidence(live, inv_id, "fp-e1", "k-e1")
    exp = await _expectation(live, hyp)
    await _evidence(live, inv_id, "fp-e2", "k-e2")
    svc = ProofPublicationService(live.factory)
    route = _definition_route("OrderService", "class OrderService:\n    pass\n")
    denied = await svc.commit(TENANT, inv_id, exp, [route])
    assert denied.committed is False
    assert denied.reason == "STALE_CONTEXT"
    assert await _counts(live, inv_id) == (0, 0, 0, 0)


@pytest.mark.asyncio
async def test_unadjudicated_conflict_denied_then_adjudicated_commits(live: Any) -> None:
    inv_id = await _investigation(live)
    hyp = await _hypothesis(live, inv_id)
    await _evidence(live, inv_id, "fp-c", "k-c")
    async with rls_session(live.factory, TENANT) as session:
        session.add(ContradictionORM(tenant_id=TENANT, investigation_id=inv_id, statement="clash"))
    exp = await _expectation(live, hyp)
    svc = ProofPublicationService(live.factory)
    route = _definition_route("OrderService", "class OrderService:\n    pass\n")
    denied = await svc.commit(TENANT, inv_id, exp, [route])
    assert denied.committed is False
    assert denied.reason == "CONFLICT_UNRESOLVED"
    assert await _counts(live, inv_id) == (0, 0, 0, 0)
    ok = await svc.commit(TENANT, inv_id, exp, [route], reconciliation_adjudicated=True)
    assert ok.committed is True


@pytest.mark.asyncio
async def test_forged_and_policy_denied_inputs_write_nothing(live: Any) -> None:
    inv_id = await _investigation(live)
    hyp = await _hypothesis(live, inv_id)
    await _evidence(live, inv_id, "fp-f", "k-f")
    exp = await _expectation(live, hyp)
    svc = ProofPublicationService(live.factory)
    forged = _definition_route(
        "OrderService", "class OrderService:\n    pass\n", premise_type="SEARCH_HIT"
    )
    denied = await svc.commit(TENANT, inv_id, exp, [forged])
    assert denied.committed is False
    assert denied.reason.startswith("EVALUATION_FAILED")
    route = _definition_route("OrderService", "class OrderService:\n    pass\n")
    denied = await svc.commit(
        TENANT,
        inv_id,
        exp,
        [route],
        policy_eligible=False,
        policy_reason="frozen",
        reconciliation_adjudicated=True,
    )
    assert denied.committed is False
    assert denied.reason.startswith("POLICY_DENIED")
    assert await _counts(live, inv_id) == (0, 0, 0, 0)


@pytest.mark.asyncio
async def test_idempotent_recommit(live: Any) -> None:
    inv_id = await _investigation(live)
    hyp = await _hypothesis(live, inv_id)
    await _evidence(live, inv_id, "fp-idem", "k-idem")
    exp = await _expectation(live, hyp)
    svc = ProofPublicationService(live.factory)
    route = _definition_route("OrderService", "class OrderService:\n    pass\n")
    first = await svc.commit(
        TENANT, inv_id, exp, [route], lifecycle=_lifecycle(), reconciliation_adjudicated=True
    )
    assert first.committed is True
    exp2 = CommitExpectation(
        hypothesis_id=hyp.id,
        hypothesis_version=await _hypothesis_version(live, hyp.id),
        evidence_digest=exp.evidence_digest,
    )
    second = await svc.commit(
        TENANT, inv_id, exp2, [route], lifecycle=_lifecycle(), reconciliation_adjudicated=True
    )
    assert second.committed is True
    assert second.reason == "ALREADY_COMMITTED"
    commits, nodes, findings, conclusions = await _counts(live, inv_id)
    assert (commits, nodes, findings, conclusions) == (1, 1, 1, 1)


def test_k14_duplicate_premise_rejected() -> None:
    with pytest.raises(CompositionError) as excinfo:
        make_node("dup", "definition/v1", ["row-1", "row-1"], [])
    assert excinfo.value.code == CompositionErrorCode.DUPLICATE_PREMISE.value


@pytest.mark.asyncio
async def test_rls_enforced_for_least_privilege_role(live: Any) -> None:
    inv_id = await _investigation(live)
    hyp = await _hypothesis(live, inv_id)
    await _evidence(live, inv_id, "fp-rls", "k-rls")
    exp = await _expectation(live, hyp)
    svc = ProofPublicationService(live.factory)
    route = _definition_route("OrderService", "class OrderService:\n    pass\n")
    out = await svc.commit(TENANT, inv_id, exp, [route], reconciliation_adjudicated=True)
    assert out.committed is True
    async with live.engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(text("SET ROLE iap_test1"))
            await conn.execute(text("SET LOCAL app.tenant_id = 'some-other-tenant'"))
            foreign = (await conn.execute(select(ProofDerivationORM))).scalars().all()
            assert foreign == []
            await conn.execute(text(f"SET LOCAL app.tenant_id = '{TENANT}'"))
            own = (await conn.execute(select(ProofDerivationORM))).scalars().all()
            assert len(own) >= 1
        finally:
            await conn.execute(text("RESET ROLE"))
            await trans.rollback()
