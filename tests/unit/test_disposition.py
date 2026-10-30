# tests/unit/test_disposition.py
"""§2 similar-past-investigation protocol (TO_DO_LLM_List): match, matrix, re-verify."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from investigation_agent_platform.api.dependencies import (
    InMemoryContradictionRepository,
    InMemoryEvidenceRepository,
    InMemoryFindingConclusionRepository,
    InMemoryInvestigationRepository,
)
from investigation_agent_platform.application.investigation.context_compiler import (
    classify_surfaces,
)
from investigation_agent_platform.application.investigation.disposition import (
    DispositionAction,
    SimilarMatch,
    apply_declined,
    decide,
    disposition_for_request,
    entity_tokens,
    find_similar,
    keyword_signature,
    overlap_similarity,
    reverify_conclusion,
    with_verdict,
)
from investigation_agent_platform.application.investigation.verification import (
    Contradiction,
    ContradictionSeverity,
)
from investigation_agent_platform.domain.evidence.models import EvidenceRelationshipType
from investigation_agent_platform.domain.finding.models import (
    ConclusionStatus,
    InvestigationConclusion,
)
from investigation_agent_platform.domain.investigation.models import (
    Investigation,
    InvestigationContext,
    InvestigationRequest,
    InvestigationStatus,
)


def _request(problem, app="my-app", session="sess-1"):
    return InvestigationRequest(
        problem_description=problem,
        application_id=app,
        session_id=session,
        requested_by="tester",
    )


def _context():
    now = datetime.now(UTC)
    return InvestigationContext(environment="test", time_window=(now, now))


def _inv(problem, status=InvestigationStatus.CREATED, app="my-app", fingerprint=None, age_days=0):
    now = datetime.now(UTC) - timedelta(days=age_days)
    return Investigation(
        session_id="sess-1",
        application_id=app,
        tenant_id="n-a",
        request=_request(problem, app),
        context=_context(),
        status=status,
        created_at=now,
        updated_at=now,
        code_issue_fingerprint=fingerprint,
    )


def _conclusion(inv_id, status=ConclusionStatus.ROOT_CAUSE_ESTABLISHED, evidence_ids=()):
    return InvestigationConclusion(
        tenant_id="n-a",
        investigation_id=inv_id,
        status=status,
        root_cause="db pool exhausted"
        if status == ConclusionStatus.ROOT_CAUSE_ESTABLISHED
        else None,
        supporting_evidence_ids=list(evidence_ids),
        generated_at=datetime.now(UTC),
    )


def _match(inv_id, status="RUNNING", conclusion=None, by="fingerprint", score=1.0):
    return SimilarMatch(
        investigation_id=inv_id,
        status=status,
        conclusion_status=conclusion,
        created_at=datetime.now(UTC),
        matched_by=by,
        score=score,
    )


def run(coro):
    return asyncio.run(coro)


# --- §2.1 matching primitives ---


def test_signature_extracts_paths_symbols_words():
    sig = keyword_signature(
        "GET /investigations/{id} fails in InvestigationService.execute with timeout"
    )
    assert "path:/investigations/{id}".replace("{id}", "{id}") in sig or any(
        t.startswith("path:/investigations") for t in sig
    )
    assert "sym:investigationservice.execute" in sig
    assert "word:timeout" in sig
    assert "word:the" not in sig and "word:with" not in sig


def test_overlap_empty_is_zero():
    assert overlap_similarity(frozenset(), frozenset({"a"})) == 0.0
    assert overlap_similarity(frozenset({"a"}), frozenset({"a"})) == 1.0


def test_entity_tokens_only_paths_and_symbols():
    sig = frozenset({"path:/a", "sym:x.y", "word:hello"})
    assert entity_tokens(sig) == frozenset({"path:/a", "sym:x.y"})


# --- §2.2 case matrix ---


def test_no_candidates_fresh():
    d = decide([])
    assert d.action == DispositionAction.FRESH and d.start_run is True
    assert d.primary_target_id is None and d.primary_link is None


def test_case_a_active_duplicate_no_run():
    target = uuid4()
    d = decide([_match(target, status="INVESTIGATING", by="fingerprint")])
    assert d.action == DispositionAction.DUPLICATE
    assert d.start_run is False
    assert d.primary_target_id == target and d.primary_link == "DUPLICATE_OF"


def test_case_a_prefers_active_over_terminal_success():
    active, done = uuid4(), uuid4()
    d = decide(
        [
            _match(done, status="COMPLETED", conclusion="ROOT_CAUSE_ESTABLISHED", by="fingerprint"),
            _match(active, status="CREATED", by="entity", score=0.8),
        ]
    )
    assert d.action == DispositionAction.DUPLICATE
    assert d.primary_target_id == active
    assert done in d.cluster_link_ids  # case F cluster


def test_case_g_text_alone_never_duplicates():
    target = uuid4()
    d = decide([_match(target, status="RUNNING", by="text", score=0.9)])
    assert d.action == DispositionAction.POSSIBLY_RELATED
    assert d.start_run is True
    assert d.primary_link == "POSSIBLY_RELATED"


def test_case_b_provisional_follow_up_pending_verdict():
    target = uuid4()
    d = decide(
        [_match(target, status="COMPLETED", conclusion="ROOT_CAUSE_ESTABLISHED", by="fingerprint")]
    )
    assert d.action == DispositionAction.FOLLOW_UP and d.start_run is True
    assert d.primary_link == "FOLLOW_UP_OF"


def test_case_c_abstention_seeded_fresh():
    target = uuid4()
    d = decide(
        [_match(target, status="COMPLETED", conclusion="INSUFFICIENT_EVIDENCE", by="fingerprint")]
    )
    assert d.action == DispositionAction.SEEDED_FRESH and d.start_run is True
    assert d.primary_link == "RELATES_TO"


def test_case_d_failed_retry():
    target = uuid4()
    d = decide([_match(target, status="FAILED", by="fingerprint")])
    assert d.action == DispositionAction.RETRY and d.start_run is True
    assert d.primary_link == "RETRY_OF"


def test_case_e_cancelled_seeded_fresh():
    target = uuid4()
    d = decide([_match(target, status="CANCELLED", by="fingerprint")])
    assert d.action == DispositionAction.SEEDED_FRESH
    assert d.primary_link == "RELATES_TO"


def test_case_e_declined_closes_without_link():
    target = uuid4()
    d = decide([_match(target, status="CANCELLED", by="fingerprint")])
    declined = apply_declined(d)
    assert declined.action == DispositionAction.DECLINED
    assert declined.start_run is False and declined.primary_link is None


def test_verdict_confirmed_serves_cached():
    target = uuid4()
    provisional = decide(
        [_match(target, status="COMPLETED", conclusion="ROOT_CAUSE_ESTABLISHED", by="fingerprint")]
    )
    verdict = {"verdict": "CONFIRMED", "checked": ["present:x"], "decayed": [], "skipped": []}
    d = with_verdict(provisional, verdict, {})
    assert d.action == DispositionAction.SERVE_CACHED
    assert d.start_run is False and d.primary_link == "DUPLICATE_OF"
    assert d.verdict == verdict


def test_verdict_stale_follow_up_carries_delta():
    target = uuid4()
    provisional = decide(
        [_match(target, status="COMPLETED", conclusion="ROOT_CAUSE_ESTABLISHED", by="fingerprint")]
    )
    verdict = {
        "verdict": "STALE",
        "checked": [],
        "decayed": [{"evidence_id": "x", "reason": "gone"}],
        "skipped": [],
    }
    d = with_verdict(provisional, verdict, {"prior_root_cause": "rc"})
    assert d.action == DispositionAction.FOLLOW_UP and d.start_run is True
    assert d.seed["decayed_citations"] == verdict["decayed"]
    assert d.seed["prior_root_cause"] == "rc"


def test_link_kinds_exist():
    for kind in ("DUPLICATE_OF", "FOLLOW_UP_OF", "RETRY_OF", "POSSIBLY_RELATED"):
        assert EvidenceRelationshipType(kind).value == kind


# --- §2.3 re-verification ---


def _evidence_row(eid, etype="LOG", observed_days_ago=1, snippet="log line"):
    from investigation_agent_platform.domain.evidence.models import EvidenceType
    from investigation_agent_platform.domain.provenance.models import (
        EvidenceFreshness,
        EvidenceProvenance,
        QueryFingerprint,
        SourceLocation,
    )

    iid = uuid4()
    now = datetime.now(UTC)
    observed = now - timedelta(days=observed_days_ago)
    return __import__(
        "investigation_agent_platform.domain.evidence.models", fromlist=["Evidence"]
    ).Evidence(
        evidence_id=eid,
        tenant_id="n-a",
        investigation_id=iid,
        evidence_type=EvidenceType(etype),
        provider="test",
        source="test://src",
        title="t",
        summary="s",
        content_snippet=snippet,
        observed_at=observed,
        retrieved_at=now,
        provenance=EvidenceProvenance(
            tenant_id="n-a",
            investigation_id=iid,
            provider_type="test",
            requested_provider_id="test",
            actual_provider_id="test",
            source_system="test",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="test", operation="test", normalized_query_hash="h"
            ),
            source_location=SourceLocation(system="test", identifier="x"),
        ),
        freshness=EvidenceFreshness(observed_at=observed, retrieved_at=now),
        fingerprint=f"fp-{eid}",
    )


def test_reverify_confirmed_when_all_citations_hold():
    prior = uuid4()
    eid = uuid4()
    ev_repo = InMemoryEvidenceRepository()
    run(ev_repo.save("n-a", _evidence_row(eid)))
    conc = _conclusion(prior, evidence_ids=[eid])
    verdict = run(
        reverify_conclusion(
            prior_investigation_id=prior,
            conclusion=conc,
            evidence_repo=ev_repo,
            contradiction_repo=InMemoryContradictionRepository(),
        )
    )
    assert verdict["verdict"] == "CONFIRMED"
    assert verdict["decayed"] == []


def test_reverify_stale_on_missing_evidence():
    prior = uuid4()
    conc = _conclusion(prior, evidence_ids=[uuid4()])
    verdict = run(
        reverify_conclusion(
            prior_investigation_id=prior,
            conclusion=conc,
            evidence_repo=InMemoryEvidenceRepository(),
            contradiction_repo=InMemoryContradictionRepository(),
        )
    )
    assert verdict["verdict"] == "STALE"
    assert any("no longer present" in d["reason"] for d in verdict["decayed"])


def test_reverify_stale_on_aged_out_logs():
    prior = uuid4()
    eid = uuid4()
    ev_repo = InMemoryEvidenceRepository()
    run(ev_repo.save("n-a", _evidence_row(eid, observed_days_ago=90)))
    conc = _conclusion(prior, evidence_ids=[eid])
    verdict = run(
        reverify_conclusion(
            prior_investigation_id=prior,
            conclusion=conc,
            evidence_repo=ev_repo,
            contradiction_repo=InMemoryContradictionRepository(),
        )
    )
    assert verdict["verdict"] == "STALE"
    assert any("aged out" in d["reason"] for d in verdict["decayed"])


def test_reverify_stale_on_new_critical_contradiction():
    prior = uuid4()
    contra_repo = InMemoryContradictionRepository()
    eid = uuid4()
    ev_repo = InMemoryEvidenceRepository()
    run(ev_repo.save("n-a", _evidence_row(eid)))
    conc = _conclusion(prior, evidence_ids=[eid])
    conc = conc.model_copy(update={"generated_at": datetime.now(UTC) - timedelta(hours=2)})
    contra = Contradiction(
        statement="pool metrics contradict exhaustion",
        severity=ContradictionSeverity.CRITICAL,
        created_at=datetime.now(UTC),
    )
    run(contra_repo.save("n-a", contra, prior))
    verdict = run(
        reverify_conclusion(
            prior_investigation_id=prior,
            conclusion=conc,
            evidence_repo=ev_repo,
            contradiction_repo=contra_repo,
        )
    )
    assert verdict["verdict"] == "STALE"
    assert any("CRITICAL" in d["reason"] for d in verdict["decayed"])


def test_reverify_skips_code_without_reader():
    prior = uuid4()
    eid = uuid4()
    ev_repo = InMemoryEvidenceRepository()
    run(ev_repo.save("n-a", _evidence_row(eid, etype="SOURCE_CODE", snippet="def f(): ...")))
    conc = _conclusion(prior, evidence_ids=[eid])
    verdict = run(
        reverify_conclusion(
            prior_investigation_id=prior,
            conclusion=conc,
            evidence_repo=ev_repo,
            contradiction_repo=InMemoryContradictionRepository(),
        )
    )
    assert verdict["verdict"] == "CONFIRMED"  # skipped checks don't block
    assert any("no code reader" in s["reason"] for s in verdict["skipped"])


def test_reverify_detects_changed_code():
    prior = uuid4()
    eid = uuid4()
    ev_repo = InMemoryEvidenceRepository()
    run(ev_repo.save("n-a", _evidence_row(eid, etype="SOURCE_CODE", snippet="def f(): old")))

    async def reader(ev):
        return "def f(): new"

    conc = _conclusion(prior, evidence_ids=[eid])
    verdict = run(
        reverify_conclusion(
            prior_investigation_id=prior,
            conclusion=conc,
            evidence_repo=ev_repo,
            contradiction_repo=InMemoryContradictionRepository(),
            code_reader=reader,
        )
    )
    assert verdict["verdict"] == "STALE"
    assert any("changed" in d["reason"] for d in verdict["decayed"])


# --- find_similar + disposition_for_request against in-memory repos ---


def test_find_similar_exact_fingerprint_row():
    repo = InMemoryInvestigationRepository()
    prior = _inv(
        "pool exhausted on checkout", status=InvestigationStatus.RUNNING, fingerprint="fp-1"
    )
    run(repo.create("n-a", prior))
    matches = run(
        find_similar(
            tenant_id="n-a",
            application_id="my-app",
            problem_description="pool exhausted again",
            fingerprint="fp-1",
            investigation_repo=repo,
            finding_repo=None,
            code_issue_index=None,
        )
    )
    assert any(m.matched_by == "fingerprint" and m.investigation_id == prior.id for m in matches)


def test_find_similar_near_text_and_entity():
    repo = InMemoryInvestigationRepository()
    prior = _inv("GET /investigations/{id} returns nothing after deploy")
    run(repo.create("n-a", prior))
    matches = run(
        find_similar(
            tenant_id="n-a",
            application_id="my-app",
            problem_description="GET /investigations/{id} returns nothing since this morning",
            fingerprint=None,
            investigation_repo=repo,
            finding_repo=None,
            code_issue_index=None,
        )
    )
    assert matches and matches[0].matched_by in ("entity", "text")


def test_find_similar_respects_retention_and_app():
    repo = InMemoryInvestigationRepository()
    old = _inv("pool exhausted on checkout", age_days=90)
    other_app = _inv("pool exhausted on checkout", app="other-app")
    run(repo.create("n-a", old))
    run(repo.create("n-a", other_app))
    matches = run(
        find_similar(
            tenant_id="n-a",
            application_id="my-app",
            problem_description="pool exhausted on checkout again today",
            fingerprint=None,
            investigation_repo=repo,
            finding_repo=None,
            code_issue_index=None,
        )
    )
    assert matches == []


def test_disposition_for_request_duplicate_active():
    repo = InMemoryInvestigationRepository()
    prior = _inv(
        "pool exhausted on checkout", status=InvestigationStatus.RUNNING, fingerprint="fp-9"
    )
    run(repo.create("n-a", prior))
    d = run(
        disposition_for_request(
            tenant_id="n-a",
            application_id="my-app",
            problem_description="pool exhausted on checkout",
            fingerprint="fp-9",
            investigation_repo=repo,
        )
    )
    assert d.action == DispositionAction.DUPLICATE and d.start_run is False


def test_disposition_for_request_fresh_when_nothing_similar():
    repo = InMemoryInvestigationRepository()
    d = run(
        disposition_for_request(
            tenant_id="n-a",
            application_id="my-app",
            problem_description="a completely novel cascading latency anomaly in sector seven",
            fingerprint=None,
            investigation_repo=repo,
        )
    )
    assert d.action == DispositionAction.FRESH and d.start_run is True


def test_disposition_for_request_case_b_confirmed_serves_cached():
    repo = InMemoryInvestigationRepository()
    findings = InMemoryFindingConclusionRepository()
    ev_repo = InMemoryEvidenceRepository()
    eid = uuid4()
    run(ev_repo.save("n-a", _evidence_row(eid)))
    prior = _inv(
        "pool exhausted on checkout", status=InvestigationStatus.COMPLETED, fingerprint="fp-7"
    )
    run(repo.create("n-a", prior))
    run(findings.save_conclusion("n-a", _conclusion(prior.id, evidence_ids=[eid])))
    d = run(
        disposition_for_request(
            tenant_id="n-a",
            application_id="my-app",
            problem_description="pool exhausted on checkout",
            fingerprint="fp-7",
            investigation_repo=repo,
            finding_repo=findings,
            contradiction_repo=InMemoryContradictionRepository(),
            evidence_repo=ev_repo,
        )
    )
    assert d.action == DispositionAction.SERVE_CACHED and d.start_run is False
    assert d.verdict and d.verdict["verdict"] == "CONFIRMED"


def test_disposition_for_request_case_b_stale_follows_up():
    repo = InMemoryInvestigationRepository()
    findings = InMemoryFindingConclusionRepository()
    prior = _inv(
        "pool exhausted on checkout", status=InvestigationStatus.COMPLETED, fingerprint="fp-8"
    )
    run(repo.create("n-a", prior))
    # Conclusion cites evidence that no longer exists → STALE follow-up.
    run(findings.save_conclusion("n-a", _conclusion(prior.id, evidence_ids=[uuid4()])))
    d = run(
        disposition_for_request(
            tenant_id="n-a",
            application_id="my-app",
            problem_description="pool exhausted on checkout",
            fingerprint="fp-8",
            investigation_repo=repo,
            finding_repo=findings,
            contradiction_repo=InMemoryContradictionRepository(),
            evidence_repo=InMemoryEvidenceRepository(),
        )
    )
    assert d.action == DispositionAction.FOLLOW_UP and d.start_run is True
    assert d.seed["decayed_citations"]


# --- O5 existence-check-first ---


def test_o5_missing_complaint_seeds_existence_check_first():
    plan = classify_surfaces("GET /investigations returns nothing after the deploy")
    assert "state" in plan["required_surfaces"]
    assert plan["open_questions"][0].startswith("Does the expected item exist in state")
