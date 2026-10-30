"""DB-backed L*-flow integration: Postgres round-trip -> S2 -> grader.

Executes the exact flow the running loop applies, against real Postgres
(``iap_test`` by default, ``IAP_TEST_FLOW_PG_DSN`` to override) through the
production SQLAlchemy repositories — not the in-memory fakes, which would
bypass every seam under test:

    Investigation create
      -> Evidence.save_batch (ON CONFLICT dedup, JSONB payload write)
      -> Evidence.find_by_investigation_id (RLS read + payload_json
         -> Evidence.model_validate rehydration: the objects S2 sees)
      -> classify_banked_sites -> compile_dormant_templates
      -> Hypothesis.save / FindingConclusion.save_conclusion
      -> FindingConclusion.get_conclusion (ORM -> domain rehydration)
      -> score_supported (the single battery rubric, on rehydrated dumps)

Each test asserts the persistence step actually landed (insert counts,
rehydrated identity) before grading, so a green run proves the banked
corpus that S2 and the grader see is the corpus Postgres returns — UUID
keys, enum values, JSONB normalization, and revision strings included.
No model calls, no Temporal, no telemetry. Opt-in only
(``IAP_RUN_FLOW_DB_TESTS=1``): the suite writes to real Postgres and must
never run by accident. Tenant is unique per run (``s2-flow-<rand>``) so
concurrent or interrupted runs cannot collide; teardown hard-deletes each
test investigation with a tenant-scoped DELETE (FK CASCADE clears
children) because the repository's ``delete()`` is a soft-delete by
design.

Requires explicit opt-in with ``IAP_RUN_FLOW_DB_TESTS=1`` and a migrated,
disposable database: ``alembic -x url=<dsn> upgrade head``. The shared
PostgreSQL guard rejects remote and protected loopback endpoints unless the
sanctioned ``IAP_PG_GUARD_OVERRIDE`` is explicitly set. The suite uses a
unique tenant per process and hard-deletes each investigation during teardown.
It skips when the opt-in is absent or the database is unreachable.
"""

from __future__ import annotations

import os
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from investigation_agent_platform.application.investigation.context_compiler import (
    MAX_DORMANT_TEMPLATES,
    compile_dormant_templates,
    external_classifier_for_roots,
)
from investigation_agent_platform.application.investigation.validator import (
    _is_within_roots,
)
from investigation_agent_platform.domain.common.exceptions import ConcurrencyError
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.evidence.qualification import QUALIFIER_VERSION
from investigation_agent_platform.domain.finding.models import (
    ConclusionStatus,
    InvestigationConclusion,
    classify_terminal_handoff,
)
from investigation_agent_platform.domain.hypothesis.models import Hypothesis
from investigation_agent_platform.domain.hypothesis.templates import (
    classify_banked_sites,
)
from investigation_agent_platform.domain.investigation.models import (
    Investigation,
    InvestigationAnchors,
    InvestigationContext,
    InvestigationRequest,
)
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
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
from investigation_agent_platform.infrastructure.persistence.pg_guard import require_disposable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import tranche_b_battery as battery

TENANT = f"s2-flow-{uuid4().hex[:8]}"
DSN = os.environ.get(
    "IAP_TEST_FLOW_PG_DSN",
    "postgresql+asyncpg://iap:iap@localhost:5452/iap_test",
)
_CASES = {c["id"]: c for c in battery.load_cases()}

#: Opt-in gate (repo convention, cf. onboarding DB tests): the suite
#: writes to real Postgres, so it never runs by accident. Set
#: IAP_RUN_FLOW_DB_TESTS=1 to run it.
_RUN_DB_TESTS = os.environ.get("IAP_RUN_FLOW_DB_TESTS", "0") == "1"

REV = "54d73240d6b5b03b899bcaf0e0c4e1cdc31be9fd"
FILTER = "service/src/main/java/com/sample/orders/filter/SampleOrderFilter.java"
IMPL = "service/src/main/java/com/sample/orders/service/OrderServiceImpl.java"
HELPER = "service/src/main/java/com/sample/orders/helper/OrderQualifyHelper.java"
MAPPER = "service/src/main/java/com/sample/orders/legacy/LegacyCodeMapper.java"
ROOTS = ["service/src/main/java"]


def _db_reachable() -> bool:
    try:
        parsed = urlparse(DSN)
        host = parsed.hostname or "localhost"
        port = parsed.port or 5432
        with socket.create_connection((host, port), timeout=2):
            pass
        return True
    except OSError:
        return False


_DB_OK = _db_reachable()

if not _RUN_DB_TESTS:
    pytestmark = pytest.mark.skipif(
        True, reason="opt-in: set IAP_RUN_FLOW_DB_TESTS=1 to run the DB flow suite"
    )
elif not _DB_OK:
    pytestmark = pytest.mark.skipif(
        True, reason=f"flow DB unreachable at {DSN} (migrate iap_test first)"
    )
else:
    # Fail closed on protected endpoints: sanctioned runs against the
    # migrated iap_test target set IAP_PG_GUARD_OVERRIDE per invocation
    # (never exported), exactly as the guard message prescribes.
    require_disposable(DSN, purpose="S2 DB flow integration tests")
    pytestmark = []


def _make_repos():
    """Fresh engine per test: pytest-asyncio STRICT gives each test its own
    event loop, and asyncpg connections cannot cross loops. Mirrors the
    per-test engine pattern in test_proof_publication_live.py."""
    engine = create_async_engine(DSN, pool_size=2, max_overflow=2, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return SimpleNamespace(
        engine=engine,
        investigations=SqlAlchemyInvestigationRepository(factory),
        evidence=SqlAlchemyEvidenceRepository(factory),
        hypotheses=SqlAlchemyHypothesisRepository(factory),
        conclusions=SqlAlchemyFindingConclusionRepository(factory),
    )


def _row(
    snippet="",
    file_path=None,
    line_start=None,
    line_end=None,
    revision=REV,
    etype=EvidenceType.SOURCE_CODE,
    attrs=None,
    summary="banked row",
    investigation_id=None,
):
    iid = investigation_id or uuid4()
    now = datetime.now(UTC)
    return Evidence(
        tenant_id=TENANT,
        investigation_id=iid,
        evidence_type=etype,
        provider="code-git",
        source=f"git://tenant-repo/{file_path or 'unknown'}@{revision}",
        title=f"Source: {file_path}" if file_path else summary,
        summary=summary,
        content_snippet=snippet,
        attributes=dict(attrs or {}),
        observed_at=now,
        retrieved_at=now,
        provenance=EvidenceProvenance(
            tenant_id=TENANT,
            investigation_id=iid,
            provider_type="code-git",
            requested_provider_id="code-git",
            actual_provider_id="code-git",
            source_system="git",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="code-git",
                operation="get_code",
                normalized_query_hash="flow",
            ),
            source_location=SourceLocation(
                system="git",
                identifier=file_path or "",
                file_path=file_path or "",
                line_start=line_start,
                line_end=line_end,
                revision=revision,
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=f"fp-{uuid4()}",
    )


def _range_row(
    snippet, file_path, line_start, line_end, revision=REV, attrs=None, investigation_id=None
):
    attributes = {"read_mode": "range"}
    attributes.update(attrs or {})
    return _row(
        snippet,
        file_path=file_path,
        line_start=line_start,
        line_end=line_end,
        revision=revision,
        attrs=attributes,
        investigation_id=investigation_id,
    )


def _anchors(**kw):
    args = {"spread_budget": 50}
    args.update(kw)
    return InvestigationAnchors(**args)


def _investigation():
    now = datetime.now(UTC)
    return Investigation(
        session_id=f"flow-{uuid4().hex[:8]}",
        application_id="spring-sample-service",
        tenant_id=TENANT,
        request=InvestigationRequest(
            application_id="spring-sample-service",
            problem_description="s2 flow battery",
            session_id=f"flow-{uuid4().hex[:8]}",
            requested_by="flow-test",
        ),
        context=InvestigationContext(
            environment="production",
            time_window=(now, now),
        ),
        metadata={},
    )


async def _hard_delete_investigation(repos, inv_id):
    """Tenant-scoped hard delete; FK CASCADE clears children. The
    repository's delete() is a soft-delete (CANCELLED transition) by
    design, so teardown mirrors scripts/clean_battery.py instead."""
    async with repos.engine.connect() as conn:
        await conn.execute(
            text("DELETE FROM investigations WHERE tenant_id = :t AND id = :i"),
            {"t": TENANT, "i": inv_id},
        )
        await conn.commit()


@pytest_asyncio.fixture
async def flow_inv():
    repos = _make_repos()
    inv = _investigation()
    try:
        await repos.investigations.create(TENANT, inv)
        yield SimpleNamespace(repos=repos, inv=inv)
    finally:
        try:
            if inv is not None:
                await _hard_delete_investigation(repos, inv.id)
        finally:
            await repos.engine.dispose()


async def _bank(repos, rows, inv):
    """Stamp + persist rows and re-read them the way the worker hydrates state.

    Rows pass through the production bank-time seam first
    (``_with_s4_qualification``, the same call execute_action_activity
    applies before persistence), so the suite proves stamp persistence
    and qualifier-version survival through JSONB — not just classifier
    behavior on hand-built attributes. The window covers the rows'
    observed range; readiness math itself stays with the S4 unit suites.
    """
    from datetime import timedelta

    from investigation_agent_platform.application.worker.activities.execution import (
        _with_s4_qualification,
    )

    if rows:
        observed = [r.observed_at for r in rows]
        window = (min(observed) - timedelta(hours=1), max(observed) + timedelta(hours=1))
        rows = [_with_s4_qualification(r, window) for r in rows]
        for row in rows:
            object.__setattr__(row, "investigation_id", inv.id)
            object.__setattr__(row.provenance, "investigation_id", inv.id)
    outcome = await repos.evidence.save_batch(TENANT, rows, investigation_id=inv.id)
    assert outcome["inserted"] == len(rows), outcome
    found = await repos.evidence.find_by_investigation_id(TENANT, inv.id)
    assert len(found) == len(rows)
    assert {str(r.evidence_id) for r in found} == {str(r.evidence_id) for r in rows}
    for row in found:
        assert row.attributes.get("s4_qualifier_version") == QUALIFIER_VERSION
    return found


def _ev_dumps(rows):
    return [r.model_dump(mode="json") for r in rows]


async def _conclude(
    ctx,
    status,
    root="",
    supporting=(),
    hyps=(),
    limitations=(),
    handoff=None,
):
    conclusion = InvestigationConclusion(
        tenant_id=TENANT,
        investigation_id=ctx.inv.id,
        status=status,
        root_cause=root or None,
        supporting_evidence_ids=list(supporting),
        supporting_hypothesis_ids=list(hyps),
        limitations=list(limitations),
        handoff=handoff,
    )
    await ctx.repos.conclusions.save_conclusion(TENANT, conclusion)
    saved = await ctx.repos.conclusions.get_conclusion(TENANT, ctx.inv.id)
    assert saved is not None
    assert saved.status == status
    assert [str(e) for e in saved.supporting_evidence_ids] == [str(e) for e in supporting]
    return saved.model_dump(mode="json")


@pytest.mark.asyncio
async def test_s4_bank_stamp_round_trips_qualified_verdict(flow_inv):
    row = _range_row(
        "10| if feature_flag_enabled:\n11|     return new_path(order)",
        FILTER,
        10,
        11,
        investigation_id=flow_inv.inv.id,
    )
    found = await _bank(flow_inv.repos, [row], flow_inv.inv)
    saved = found[0]
    assert saved.attributes["s4_qualified"] is True
    assert saved.attributes["s4_layer"] == "code"
    # The stamp must carry an explanation, but the test pins the contract
    # (qualified + layer), never the human wording of the reason.
    assert saved.attributes["s4_reason"]
    assert saved.attributes["s4_qualifier_version"] == QUALIFIER_VERSION


@pytest.mark.asyncio
async def test_s7_handoff_round_trips_through_conclusion_repository(flow_inv):
    handoff = classify_terminal_handoff(
        status=ConclusionStatus.INSUFFICIENT_EVIDENCE,
        reason="budget_exceeded",
        next_check="GET_CODE the pinned caller range, then re-verify.",
    )
    dumped = await _conclude(
        flow_inv,
        ConclusionStatus.INSUFFICIENT_EVIDENCE,
        limitations=["Investigation ended: budget_exceeded."],
        handoff=handoff,
    )
    assert dumped["handoff"]["version"] == handoff.version
    assert dumped["handoff"]["state"] == "BUDGET_DEFERRED"
    assert dumped["handoff"]["next_fetch"] == handoff.next_fetch
    assert dumped["handoff"]["resume_pointer"] == handoff.resume_pointer


def _classify(rows):
    return classify_banked_sites(rows, is_external=external_classifier_for_roots(ROOTS))


@pytest.mark.asyncio
async def test_L1_definition_lookup_seeds_no_templates_but_grades_pass(flow_inv):
    rows = [_range_row("72| public class SampleOrderFilter", FILTER, 72, 80)]
    found = await _bank(flow_inv.repos, rows, flow_inv.inv)
    assert _classify(found) == []
    section = compile_dormant_templates(
        found, anchors=_anchors(), is_external=external_classifier_for_roots(ROOTS)
    )
    assert section["templates"] == []

    dumps = _ev_dumps(found)
    verdict, _ = battery.score_supported(
        _CASES["L1"],
        await _conclude(
            flow_inv,
            ConclusionStatus.ANSWERED,
            root=f"SampleOrderFilter is implemented in {FILTER}",
            supporting=[r.evidence_id for r in found],
        ),
        {},
        {"items": dumps},
        {"items": []},
    )
    assert verdict == "PASS"


@pytest.mark.asyncio
async def test_L2_caller_graph_rows_seed_no_templates(flow_inv):
    rows = [
        _row(
            "OrderServiceHandler <- OrderQualifyHelper.handle",
            file_path=HELPER,
            etype=EvidenceType.CALL_GRAPH,
            summary="call edge",
        ),
        _range_row("10| result = handler.handle(order)", HELPER, 10, 12),
    ]
    found = await _bank(flow_inv.repos, rows, flow_inv.inv)
    assert _classify(found) == []
    section = compile_dormant_templates(
        found, anchors=_anchors(), is_external=external_classifier_for_roots(ROOTS)
    )
    assert section["templates"] == []

    call_site = next(r for r in found if r.evidence_type == EvidenceType.SOURCE_CODE)
    dumps = _ev_dumps([call_site])
    verdict, _ = battery.score_supported(
        _CASES["L2"],
        await _conclude(
            flow_inv,
            ConclusionStatus.ANSWERED,
            root="OrderServiceHandler.handle is called by OrderQualifyHelper",
            supporting=[call_site.evidence_id],
        ),
        {},
        {"items": dumps},
        {"items": []},
    )
    assert verdict == "PASS"


@pytest.mark.asyncio
async def test_L3_history_rows_seed_no_templates(flow_inv):
    rows = [
        _row(
            "abc1234 fix null guard in isEligible",
            file_path=IMPL,
            etype=EvidenceType.COMMIT_HISTORY,
            summary="fix null guard in isEligible",
        )
    ]
    found = await _bank(flow_inv.repos, rows, flow_inv.inv)
    assert _classify(found) == []

    verdict, _ = battery.score_supported(
        _CASES["L3"],
        await _conclude(
            flow_inv,
            ConclusionStatus.ANSWERED,
            root=f"{IMPL} changed recently: fix null guard in isEligible",
            supporting=[r.evidence_id for r in found],
        ),
        {},
        {"items": _ev_dumps(found)},
        {"items": []},
    )
    assert verdict == "PASS"


@pytest.mark.asyncio
async def test_L4_corrected_path_templates_flag_gate_and_grades_pass(flow_inv):
    rows = [
        _range_row(
            "10| if feature_flag_enabled:\n11|     return SampleOrderFilter.new_path(order)",
            FILTER,
            10,
            11,
        )
    ]
    found = await _bank(flow_inv.repos, rows, flow_inv.inv)
    paths = _classify(found)
    assert len(paths) == 1
    section = compile_dormant_templates(
        found, anchors=_anchors(), is_external=external_classifier_for_roots(ROOTS)
    )
    assert [t["kind"] for t in section["templates"]] == ["FLAG_GATED"]
    assert section["templates"][0]["discriminating_fetch"] is not None
    assert section["templates"][0]["handoff"] is None

    verdict, _ = battery.score_supported(
        _CASES["L4"],
        await _conclude(
            flow_inv,
            ConclusionStatus.ANSWERED,
            root=f"SampleOrderFilter is implemented in {FILTER}",
            supporting=[r.evidence_id for r in found],
        ),
        {},
        {"items": _ev_dumps(found)},
        {"items": []},
    )
    assert verdict == "PASS"


@pytest.mark.asyncio
async def test_L5_branch_chain_templates_data_gated_and_values_grade(flow_inv):
    rows = [
        _range_row(
            "10| switch (orderType) {\n11| case STANDARD:\n12|     return normalize(order);",
            MAPPER,
            10,
            12,
        )
    ]
    found = await _bank(flow_inv.repos, rows, flow_inv.inv)
    assert len(_classify(found)) == 1
    section = compile_dormant_templates(
        found, anchors=_anchors(), is_external=external_classifier_for_roots(ROOTS)
    )
    assert [t["kind"] for t in section["templates"]] == ["DATA_GATED"]

    verdict, _ = battery.score_supported(
        _CASES["L5"],
        await _conclude(
            flow_inv,
            ConclusionStatus.ANSWERED,
            root="null orderType falls back to STANDARD via LegacyCodeMapper.normalize",
            supporting=[r.evidence_id for r in found],
        ),
        {},
        {"items": _ev_dumps(found)},
        {"items": []},
    )
    assert verdict == "PASS"


@pytest.mark.asyncio
async def test_L8_unanswerable_banks_nothing_and_abstains(flow_inv):
    found = await _bank(flow_inv.repos, [], flow_inv.inv)
    assert found == []
    section = compile_dormant_templates(
        found, anchors=_anchors(), is_external=external_classifier_for_roots(ROOTS)
    )
    assert section["templates"] == []

    verdict, _ = battery.score_supported(
        _CASES["L8"],
        await _conclude(
            flow_inv,
            ConclusionStatus.INSUFFICIENT_EVIDENCE,
            root="no quantum module in this repo",
        ),
        {},
        {"items": []},
        {"items": []},
    )
    assert verdict == "PASS"


@pytest.mark.asyncio
async def test_L9_duplicate_symbol_yields_one_path_per_file(flow_inv):
    rows = [
        _range_row(
            "20| switch (kind) {\n21| case X: return isEligible(order);",
            IMPL,
            20,
            21,
        ),
        _range_row(
            "30| switch (kind) {\n31| case X: return isEligible(order);",
            IMPL.replace("service/", "service/legacy/"),
            30,
            31,
        ),
    ]
    found = await _bank(flow_inv.repos, rows, flow_inv.inv)
    assert len(_classify(found)) == 2

    hyps = [
        Hypothesis(
            tenant_id=TENANT,
            investigation_id=flow_inv.inv.id,
            title=f"rival {i}",
            description="duplicate symbol rival",
            statement=f"isEligible defined in {r.provenance.source_location.file_path}",
            supporting_evidence_ids=[r.evidence_id],
        )
        for i, r in enumerate(found)
    ]
    for hyp in hyps:
        await flow_inv.repos.hypotheses.save(TENANT, hyp, flow_inv.inv.id, 0)
    saved_hyps = await flow_inv.repos.hypotheses.find_by_investigation_id(TENANT, flow_inv.inv.id)
    assert len(saved_hyps) == 2

    verdict, _ = battery.score_supported(
        _CASES["L9"],
        await _conclude(
            flow_inv,
            ConclusionStatus.ANSWERED,
            root=f"OrderServiceImpl.isEligible is defined in {IMPL}",
            supporting=[found[0].evidence_id],
            hyps=[h.id for h in saved_hyps],
        ),
        {},
        {"items": _ev_dumps(found)},
        {"items": [h.model_dump(mode="json") for h in saved_hyps]},
    )
    assert verdict == "PASS"


@pytest.mark.asyncio
async def test_L10_head_row_excluded_pinned_row_flows(flow_inv):
    rows = [
        _range_row(
            "10| if feature_flag_enabled:\n11|     return SampleOrderFilter.new_path(order)",
            FILTER,
            10,
            11,
            revision="HEAD",
        ),
        _range_row(
            "10| if feature_flag_enabled:\n11|     return SampleOrderFilter.new_path(order)",
            FILTER,
            10,
            11,
            revision="abc123",
        ),
    ]
    found = await _bank(flow_inv.repos, rows, flow_inv.inv)
    paths = _classify(found)
    assert len(paths) == 1
    assert paths[0].revision == "abc123"

    pinned = next(r for r in found if r.provenance.source_location.revision == "abc123")
    verdict, _ = battery.score_supported(
        _CASES["L10"],
        await _conclude(
            flow_inv,
            ConclusionStatus.ANSWERED,
            root=f"SampleOrderFilter is implemented in {FILTER}",
            supporting=[pinned.evidence_id],
        ),
        {},
        {"items": _ev_dumps([pinned])},
        {"items": []},
    )
    assert verdict == "PASS"


@pytest.mark.asyncio
async def test_L11_failure_rows_never_support_a_conclusion(flow_inv):
    rows = [
        _range_row("72| public class SampleOrderFilter", FILTER, 72, 80),
        _row(
            "code index timed out",
            file_path=FILTER,
            etype=EvidenceType.SOURCE_CODE,
            attrs={"miss_reason": "timeout"},
            summary="provider timeout",
        ),
    ]
    found = await _bank(flow_inv.repos, rows, flow_inv.inv)
    failed = next(r for r in found if r.attributes.get("miss_reason") == "timeout")
    failed_dump = failed.model_dump(mode="json")
    assert failed_dump["attributes"]["miss_reason"] == "timeout"

    verdict, _ = battery.score_supported(
        _CASES["L11"],
        await _conclude(
            flow_inv,
            ConclusionStatus.INSUFFICIENT_EVIDENCE,
            root="code index timed out",
        ),
        {},
        {"items": [failed_dump]},
        {"items": []},
    )
    assert verdict == "PASS"

    verdict, detail = battery.score_supported(
        _CASES["L11"],
        await _conclude(
            flow_inv,
            ConclusionStatus.INSUFFICIENT_EVIDENCE,
            root="code index timed out",
            supporting=[failed.evidence_id],
        ),
        {},
        {"items": [failed_dump]},
        {"items": []},
    )
    assert verdict == "PARTIAL", detail
    assert "timeout" in detail


@pytest.mark.asyncio
async def test_L12_absence_proof_plus_write_path_grades_pass(flow_inv):
    rows = [
        _row(
            "",
            file_path="audit_log/order-9",
            etype=EvidenceType.SOURCE_CODE,
            attrs={"match_count": 0, "search_scope": "audit_log/order-9"},
            summary="empty search over refund audit scope",
        ),
        _range_row(
            "50| ledger.append(entry)",
            "service/src/main/java/com/sample/orders/RefundWriter.java",
            50,
            52,
        ),
    ]
    found = await _bank(flow_inv.repos, rows, flow_inv.inv)
    proof = next(r for r in found if r.attributes.get("match_count") == 0)
    assert _classify([proof]) == []

    verdict, _ = battery.score_supported(
        _CASES["L12"],
        await _conclude(
            flow_inv,
            ConclusionStatus.ANSWERED,
            root=(
                "No audit row was written for the refund in scope "
                "audit_log/order-9; write path checked."
            ),
            supporting=[r.evidence_id for r in found],
        ),
        {},
        {"items": _ev_dumps(found)},
        {"items": []},
    )
    assert verdict == "PASS"


@pytest.mark.asyncio
async def test_L13_not_checkable_skips_retrieval(flow_inv):
    found = await _bank(flow_inv.repos, [], flow_inv.inv)
    assert found == []

    verdict, _ = battery.score_supported(
        _CASES["L13"],
        await _conclude(
            flow_inv,
            ConclusionStatus.INSUFFICIENT_EVIDENCE,
            root="beauty has no observable",
            limitations=["NOT_CHECKABLE_SKIPPED"],
        ),
        {},
        {"items": []},
        {"items": []},
    )
    assert verdict == "PASS"


@pytest.mark.asyncio
async def test_dispatch_parity_empty_roots_render_handoff_not_fetch(flow_inv):
    rows = [
        _range_row(
            "10| if feature_flag_enabled:\n11|     return SampleOrderFilter.new_path(order)",
            "service/svc/app.py",
            10,
            11,
        )
    ]
    found = await _bank(flow_inv.repos, rows, flow_inv.inv)
    classifier = external_classifier_for_roots([])
    assert classifier("service/svc/app.py") is True
    assert _is_within_roots("service/svc/app.py", []) is False
    section = compile_dormant_templates(found, anchors=_anchors(), is_external=classifier)
    assert len(section["templates"]) == 1
    assert section["templates"][0]["discriminating_fetch"] is None
    assert section["templates"][0]["handoff"] is not None


@pytest.mark.asyncio
async def test_template_cap_stable_across_bank_order(flow_inv):
    """P4 lock at DB level: Postgres returns rows in an order the test does
    not control; two investigations banking the same ten rows must still
    render identical template sections."""
    snippets = [
        (
            "10| if feature_flag_enabled:\n11|     return SampleOrderFilter.new_path(order)",
            f"service/svc/mod{i}.py",
        )
        for i in range(10)
    ]
    first_rows = [_range_row(snippet, path, 10, 11) for snippet, path in snippets]
    first_found = await _bank(flow_inv.repos, first_rows, flow_inv.inv)
    first_section = compile_dormant_templates(
        first_found,
        anchors=_anchors(),
        is_external=external_classifier_for_roots(ROOTS),
    )

    inv2 = _investigation()
    await flow_inv.repos.investigations.create(TENANT, inv2)
    try:
        second_rows = [
            _range_row(snippet, path, 10, 11, investigation_id=inv2.id)
            for snippet, path in reversed(snippets)
        ]
        second_found = await _bank(flow_inv.repos, second_rows, inv2)
        second_section = compile_dormant_templates(
            second_found,
            anchors=_anchors(),
            is_external=external_classifier_for_roots(ROOTS),
        )
    finally:
        await _hard_delete_investigation(flow_inv.repos, inv2.id)

    assert MAX_DORMANT_TEMPLATES == 8
    assert len(first_section["templates"]) == MAX_DORMANT_TEMPLATES
    assert [t["suspect_path"] for t in first_section["templates"]] == [
        t["suspect_path"] for t in second_section["templates"]
    ]
    assert [t["kind"] for t in first_section["templates"]] == [
        t["kind"] for t in second_section["templates"]
    ]


@pytest.mark.asyncio
async def test_tenant_isolation_enforced_at_repository_layer(flow_inv):
    """Tenant scoping is a claimed seam of the flow: rows banked under one
    tenant must be invisible to another through the same repositories
    (per-tenant WHERE predicates plus RLS session scoping)."""
    rows = [_range_row("72| public class SampleOrderFilter", FILTER, 72, 80)]
    found = await _bank(flow_inv.repos, rows, flow_inv.inv)
    assert len(found) == 1
    await _conclude(
        flow_inv,
        ConclusionStatus.ANSWERED,
        root=f"SampleOrderFilter is implemented in {FILTER}",
        supporting=[r.evidence_id for r in found],
    )
    other = f"{TENANT}-other"
    assert await flow_inv.repos.evidence.find_by_investigation_id(other, flow_inv.inv.id) == []
    assert await flow_inv.repos.conclusions.get_conclusion(other, flow_inv.inv.id) is None


@pytest.mark.asyncio
async def test_conclusion_rewrite_guard_rejects_overwrite(flow_inv):
    """save_conclusion's core contract through the DB: overwriting a terminal
    verdict with a different status is a lost race and must fail loudly,
    while an idempotent same-status retry is allowed."""
    rows = [_range_row("72| public class SampleOrderFilter", FILTER, 72, 80)]
    found = await _bank(flow_inv.repos, rows, flow_inv.inv)
    supporting = [r.evidence_id for r in found]
    await _conclude(
        flow_inv,
        ConclusionStatus.ANSWERED,
        root=f"SampleOrderFilter is implemented in {FILTER}",
        supporting=supporting,
    )
    with pytest.raises(ConcurrencyError):
        await flow_inv.repos.conclusions.save_conclusion(
            TENANT,
            InvestigationConclusion(
                tenant_id=TENANT,
                investigation_id=flow_inv.inv.id,
                status=ConclusionStatus.INSUFFICIENT_EVIDENCE,
                root_cause="a different verdict",
            ),
        )
    await flow_inv.repos.conclusions.save_conclusion(
        TENANT,
        InvestigationConclusion(
            tenant_id=TENANT,
            investigation_id=flow_inv.inv.id,
            status=ConclusionStatus.ANSWERED,
            root_cause=f"SampleOrderFilter is implemented in {FILTER}",
            supporting_evidence_ids=supporting,
        ),
    )
    saved = await flow_inv.repos.conclusions.get_conclusion(TENANT, flow_inv.inv.id)
    assert saved is not None
    assert saved.status == ConclusionStatus.ANSWERED


@pytest.mark.asyncio
async def test_revision_drift_visible_across_banked_rows(flow_inv):
    """The live battery flags revision_drift from the database; the flow
    must surface every banked revision, never silently collapse them."""
    rows = [
        _range_row(
            "10| if feature_flag_enabled:\n11|     return SampleOrderFilter.new_path(order)",
            FILTER,
            10,
            11,
            revision="aaa111",
        ),
        _range_row(
            "10| if feature_flag_enabled:\n11|     return SampleOrderFilter.new_path(order)",
            FILTER,
            10,
            11,
            revision="bbb222",
        ),
    ]
    found = await _bank(flow_inv.repos, rows, flow_inv.inv)
    assert {r.provenance.source_location.revision for r in found} == {"aaa111", "bbb222"}
    assert len(_classify(found)) == 2


@pytest.mark.asyncio
async def test_hypothesis_guarded_update_bumps_version(flow_inv):
    """Hypothesis save contract through the DB: insert-at-1, conditional
    update N→N+1, stale writes raise ConcurrencyError."""
    hyp = Hypothesis(
        tenant_id=TENANT,
        investigation_id=flow_inv.inv.id,
        title="rival",
        description="duplicate symbol rival",
        statement="isEligible defined here v1",
    )
    await flow_inv.repos.hypotheses.save(TENANT, hyp, flow_inv.inv.id, 0)
    (saved,) = await flow_inv.repos.hypotheses.find_by_investigation_id(TENANT, flow_inv.inv.id)
    assert saved.version == 1
    await flow_inv.repos.hypotheses.save(
        TENANT,
        hyp.model_copy(update={"statement": "isEligible defined here v2"}),
        flow_inv.inv.id,
        1,
    )
    (saved2,) = await flow_inv.repos.hypotheses.find_by_investigation_id(TENANT, flow_inv.inv.id)
    assert saved2.version == 2
    assert saved2.statement == "isEligible defined here v2"
    with pytest.raises(ConcurrencyError):
        await flow_inv.repos.hypotheses.save(
            TENANT,
            hyp.model_copy(update={"statement": "stale write"}),
            flow_inv.inv.id,
            1,
        )
