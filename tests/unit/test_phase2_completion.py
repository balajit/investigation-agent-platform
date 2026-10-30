# tests/unit/test_phase2_completion.py
"""Phase 2.3/2.4: rival contrast, typed limitations, conclusion mapping."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from investigation_agent_platform.application.investigation.verification import (
    CausalRelationship,
    LimitationCode,
    RootCauseVerificationPolicy,
    VerificationEngine,
    VerificationLimitation,
    VerificationStatus,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.finding.models import InvestigationConclusion
from investigation_agent_platform.domain.hypothesis.models import (
    AssessmentStrength,
    AssessmentType,
    ClaimKind,
    Hypothesis,
    HypothesisEvidenceAssessment,
    HypothesisStatus,
)
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)


def _ev(etype=EvidenceType.RUNTIME_LOG, provider="p1", snippet="pool exhausted"):
    iid = uuid4()
    now = datetime.now(UTC)
    return Evidence(
        tenant_id="t",
        investigation_id=iid,
        evidence_type=etype,
        provider=provider,
        source="test://src",
        title="t",
        summary=snippet,
        content_snippet=snippet,
        observed_at=now,
        retrieved_at=now,
        provenance=EvidenceProvenance(
            tenant_id="t",
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
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=f"fp-{uuid4()}",
    )


def _hyp(statement, status=HypothesisStatus.PROPOSED, assessed_ids=(), claim_kind=None):
    from investigation_agent_platform.domain.hypothesis.models import ClaimTarget

    h_id = uuid4()
    assessments = []
    for eid in list(assessed_ids):
        kwargs = {}
        if claim_kind is not None:
            kwargs = {"claim_kind": claim_kind, "claim_target": ClaimTarget()}
        assessments.append(
            HypothesisEvidenceAssessment(
                hypothesis_id=h_id,
                evidence_id=eid,
                assessment=AssessmentType.SUPPORTS,
                strength=AssessmentStrength.MODERATE,
                reason="cited",
                **kwargs,
            )
        )
    return Hypothesis(
        id=h_id,
        tenant_id="t",
        investigation_id=uuid4(),
        title=statement[:50],
        description=statement,
        statement=statement,
        status=status,
        supporting_evidence_ids=list(assessed_ids),
        assessments=assessments,
    )


def _verifiable_setup():
    items = [_ev(EvidenceType.RUNTIME_LOG, "p1") for _ in range(3)]
    items += [
        _ev(EvidenceType.METRIC, "p2", snippet="pool saturation 99, checkout latency exceeded deadline")
        for _ in range(3)
    ]
    chain = [
        CausalRelationship(
            cause="pool exhaustion",
            effect="checkout latency",
            mechanism="threads block on pool",
            confidence=0.9,
            evidence_ids=[e.evidence_id for e in items[:2]],
        ),
        CausalRelationship(
            cause="checkout latency",
            effect="timeout",
            mechanism="deadline exceeded",
            confidence=0.9,
            evidence_ids=[e.evidence_id for e in items[2:4]],
        ),
    ]
    return items, chain


class TestRivalContrast:
    def test_untested_rival_demotes_to_likely(self):
        engine = VerificationEngine()
        items, chain = _verifiable_setup()
        candidate = _hyp("Pool exhaustion causes checkout latency", HypothesisStatus.SUPPORTED, assessed_ids=[items[1].evidence_id], claim_kind="CAUSAL")
        rival = _hyp("A slow downstream causes checkout latency")
        result = engine.evaluate_hypothesis(
            candidate, items, [], chain, RootCauseVerificationPolicy(),
            sibling_hypotheses=[rival],
        )
        assert result.status == VerificationStatus.VERIFIED
        assert result.tier == "likely"
        assert any(lim.code == LimitationCode.RIVAL_UNTESTED for lim in result.limitations)
        assert "RIVAL_UNTESTED" in result.explanation

    def test_tested_rivals_keep_established(self):
        engine = VerificationEngine()
        items, chain = _verifiable_setup()
        candidate = _hyp("Pool exhaustion causes checkout latency", HypothesisStatus.SUPPORTED, assessed_ids=[items[1].evidence_id], claim_kind="CAUSAL")
        assessed = _hyp("Other cause", assessed_ids=[items[0].evidence_id])
        rejected = _hyp("Bad cause", status=HypothesisStatus.REJECTED)
        result = engine.evaluate_hypothesis(
            candidate, items, [], chain, RootCauseVerificationPolicy(),
            sibling_hypotheses=[assessed, rejected],
        )
        assert result.status == VerificationStatus.VERIFIED
        assert result.tier == "established"
        assert not any(lim.code == LimitationCode.RIVAL_UNTESTED for lim in result.limitations)

    def test_candidate_excluded_from_rivals_by_id(self):
        engine = VerificationEngine()
        items, chain = _verifiable_setup()
        candidate = _hyp("Pool exhaustion causes checkout latency", HypothesisStatus.SUPPORTED)
        result = engine.evaluate_hypothesis(
            candidate, items, [], chain, RootCauseVerificationPolicy(),
            sibling_hypotheses=[candidate],
        )
        assert result.tier == "established"

    def test_single_provider_limitation_recorded(self):
        engine = VerificationEngine()
        items = [_ev(EvidenceType.RUNTIME_LOG, "p1") for _ in range(3)]
        items += [_ev(EvidenceType.TRACE, "p1", snippet="span pool wait") for _ in range(3)]
        chain = [
            CausalRelationship(
                cause="a", effect="b", mechanism="m", confidence=0.9,
                evidence_ids=[e.evidence_id for e in items[:2]],
            ),
            CausalRelationship(
                cause="b", effect="c", mechanism="n", confidence=0.9,
                evidence_ids=[e.evidence_id for e in items[2:4]],
            ),
        ]
        candidate = _hyp("Pool exhaustion causes latency", HypothesisStatus.SUPPORTED, assessed_ids=[items[1].evidence_id], claim_kind="CAUSAL")
        policy = RootCauseVerificationPolicy(require_cross_layer_evidence=False)
        result = engine.evaluate_hypothesis(candidate, items, [], chain, policy)
        assert result.status == VerificationStatus.VERIFIED
        assert any(lim.code == LimitationCode.SINGLE_PROVIDER for lim in result.limitations)
        assert "SINGLE_PROVIDER" in result.explanation

    def test_verified_explanation_names_limitations_not_just_counts(self):
        engine = VerificationEngine()
        items, chain = _verifiable_setup()
        candidate = _hyp("Pool exhaustion causes checkout latency", HypothesisStatus.SUPPORTED, assessed_ids=[items[1].evidence_id], claim_kind="CAUSAL")
        result = engine.evaluate_hypothesis(
            candidate, items, [], chain, RootCauseVerificationPolicy(),
            sibling_hypotheses=[_hyp("Untested rival")],
        )
        assert "Limitations:" in result.explanation
        assert "RIVAL_UNTESTED" in result.explanation


def _code_row(snippet, file_path="src/svc/pool.py"):
    return SimpleNamespace(
        title="Source: pool",
        summary="Pool implementation with exhaustion guard",
        content_snippet=snippet,
        source=f"git://r/{file_path}@abc",
        provenance=SimpleNamespace(
            source_location=SimpleNamespace(file_path=file_path)
        ),
    )


class TestConclusionMapping:
    def test_prose_only_causal_claim_is_unmapped(self):
        rows = [_code_row("10| def healthy():\n11|     return True", "src/other.py")]
        unmapped = VerificationEngine.map_conclusion_sentences(
            "The cache layer is slow because of lock contention.", rows, []
        )
        assert unmapped == ["The cache layer is slow because of lock contention"]

    def test_cited_claim_maps(self):
        rows = [
            _code_row(
                "10| def acquire():\n11|     # pool exhaustion guard\n12|     raise PoolExhausted",
                "src/svc/pool.py",
            )
        ]
        unmapped = VerificationEngine.map_conclusion_sentences(
            "Pool exhaustion causes checkout latency. The guard is implemented in src/svc/pool.py.",
            rows,
            ["pool exhaustion threads block on pool checkout latency"],
        )
        assert unmapped == []

    def test_unmapped_demotes_to_likely(self):
        engine = VerificationEngine()
        items, chain = _verifiable_setup()
        candidate = _hyp("Pool exhaustion causes checkout latency", HypothesisStatus.SUPPORTED)
        result = engine.evaluate_hypothesis(
            candidate, items, [], chain, RootCauseVerificationPolicy()
        )
        assert result.tier == "established"
        demoted = VerificationEngine.apply_conclusion_mapping(
            result, ["The cache layer is slow because of lock contention"]
        )
        assert demoted.tier == "likely"
        assert any(lim.code == LimitationCode.CONCLUSION_UNMAPPED for lim in demoted.limitations)

    def test_mapped_leaves_tier_alone(self):
        engine = VerificationEngine()
        items, chain = _verifiable_setup()
        candidate = _hyp("Pool exhaustion causes checkout latency", HypothesisStatus.SUPPORTED)
        result = engine.evaluate_hypothesis(
            candidate, items, [], chain, RootCauseVerificationPolicy()
        )
        assert VerificationEngine.apply_conclusion_mapping(result, []).tier == "established"

    def test_next_check_priority(self):
        untested = ["A slow downstream causes it"]
        unmapped = ["The cache layer is slow"]
        rival_lim = VerificationLimitation(code=LimitationCode.RIVAL_UNTESTED, detail="d")
        unmapped_lim = VerificationLimitation(code=LimitationCode.CONCLUSION_UNMAPPED, detail="d")
        single_lim = VerificationLimitation(code=LimitationCode.SINGLE_PROVIDER, detail="d")
        assert VerificationEngine.next_check_for([], []) is None
        assert "untested rival" in (
            VerificationEngine.next_check_for([rival_lim], untested, unmapped) or ""
        )
        assert "each conclusion sentence" in (
            VerificationEngine.next_check_for([unmapped_lim], [], unmapped) or ""
        )
        assert "independent evidence provider" in (
            VerificationEngine.next_check_for([single_lim], [], []) or ""
        )

    def test_conclusion_persists_next_check(self):
        conclusion = InvestigationConclusion(
            tenant_id="t",
            investigation_id=uuid4(),
            status="ROOT_CAUSE_LIKELY",
            root_cause="Pool exhaustion causes latency.",
            supporting_evidence_ids=[uuid4()],
            limitations=["RIVAL_UNTESTED: 1 rival never evaluated"],
            next_check="Evaluate the untested rival hypothesis against evidence, then re-verify.",
        )
        assert conclusion.next_check is not None
        assert "re-verify" in conclusion.next_check


def _chain_step(cause, mechanism, effect, evidence_ids):
    return CausalRelationship(
        cause=cause,
        effect=effect,
        mechanism=mechanism,
        confidence=0.9,
        evidence_ids=list(evidence_ids),
    )


class TestCausalStepGrounding:
    def test_step_citing_nothing_is_inconclusive(self):
        engine = VerificationEngine()
        items, _ = _verifiable_setup()
        candidate = _hyp("Pool exhaustion causes checkout latency", HypothesisStatus.SUPPORTED, assessed_ids=[items[1].evidence_id], claim_kind="CAUSAL")
        chain = [_chain_step("pool exhaustion", "threads block", "checkout latency", [])]
        result = engine.evaluate_hypothesis(
            candidate, items, [], chain, RootCauseVerificationPolicy()
        )
        assert result.status == VerificationStatus.INCONCLUSIVE
        assert "cites no evidence" in result.explanation

    def test_step_citing_unknown_id_is_inconclusive(self):
        engine = VerificationEngine()
        items, _ = _verifiable_setup()
        candidate = _hyp("Pool exhaustion causes checkout latency", HypothesisStatus.SUPPORTED, assessed_ids=[items[1].evidence_id], claim_kind="CAUSAL")
        chain = [_chain_step("pool exhaustion", "threads block", "checkout latency", [uuid4()])]
        result = engine.evaluate_hypothesis(
            candidate, items, [], chain, RootCauseVerificationPolicy()
        )
        assert result.status == VerificationStatus.INCONCLUSIVE
        assert "unknown id" in result.explanation

    def test_step_citing_only_docs_is_inconclusive(self):
        engine = VerificationEngine()
        doc = _ev(EvidenceType.DOCUMENTATION, "p1", snippet="pooling runbook guidance")
        items, _ = _verifiable_setup()
        candidate = _hyp("Pool exhaustion causes checkout latency", HypothesisStatus.SUPPORTED, assessed_ids=[items[1].evidence_id], claim_kind="CAUSAL")
        chain = [_chain_step("pool exhaustion", "threads block", "checkout latency", [doc.evidence_id])]
        result = engine.evaluate_hypothesis(
            candidate, [doc, *items], [], chain, RootCauseVerificationPolicy()
        )
        assert result.status == VerificationStatus.INCONCLUSIVE
        assert "orientation/docs" in result.explanation

    def test_undemonstrated_relation_demotes_to_likely(self):
        engine = VerificationEngine()
        code = [
            _ev(EvidenceType.SOURCE_CODE, "p1", snippet="def healthy():\n    return True"),
            _ev(EvidenceType.SOURCE_CODE, "p2", snippet="def ready():\n    return True"),
        ]
        logs = [_ev(EvidenceType.RUNTIME_LOG, "p1", snippet="ok") for _ in range(4)]
        items = code + logs
        candidate = _hyp("Pool exhaustion causes checkout latency", HypothesisStatus.SUPPORTED, assessed_ids=[items[1].evidence_id], claim_kind="CAUSAL")
        chain = [
            _chain_step(
                "pool exhaustion",
                "threads block on pool",
                "checkout latency",
                [e.evidence_id for e in code],
            ),
            _chain_step(
                "checkout latency",
                "deadline exceeded",
                "timeout",
                [e.evidence_id for e in logs[:2]],
            ),
        ]
        result = engine.evaluate_hypothesis(
            candidate, items, [], chain, RootCauseVerificationPolicy()
        )
        assert result.status == VerificationStatus.VERIFIED
        assert result.tier == "likely"
        assert any(lim.code == LimitationCode.RELATION_UNDEMONSTRATED for lim in result.limitations)
        assert "RELATION_UNDEMONSTRATED" in result.explanation

    def test_demonstrated_relation_stays_established(self):
        engine = VerificationEngine()
        code = _ev(
            EvidenceType.SOURCE_CODE,
            "p1",
            snippet="if pool_exhaustion:\n    threads block on pool",
        )
        metric = _ev(
            EvidenceType.METRIC,
            "p2",
            snippet="checkout latency exceeded deadline, raising timeout",
        )
        filler = [_ev(EvidenceType.RUNTIME_LOG, "p1", snippet="ok") for _ in range(2)]
        filler += [_ev(EvidenceType.RUNTIME_LOG, "p2", snippet="ok") for _ in range(2)]
        items = [code, metric, *filler]
        candidate = _hyp("Pool exhaustion causes checkout latency", HypothesisStatus.SUPPORTED, assessed_ids=[items[1].evidence_id], claim_kind="CAUSAL")
        chain = [
            _chain_step(
                "pool exhaustion",
                "threads block on pool",
                "checkout latency",
                [code.evidence_id],
            ),
            _chain_step(
                "checkout latency",
                "deadline exceeded",
                "timeout",
                [metric.evidence_id],
            ),
        ]
        result = engine.evaluate_hypothesis(
            candidate, items, [], chain, RootCauseVerificationPolicy()
        )
        assert result.status == VerificationStatus.VERIFIED
        assert result.tier == "established"
        assert not any(
            lim.code == LimitationCode.RELATION_UNDEMONSTRATED for lim in result.limitations
        )

    def test_next_check_relation_priority(self):
        relation_lim = VerificationLimitation(code=LimitationCode.RELATION_UNDEMONSTRATED, detail="d")
        unmapped_lim = VerificationLimitation(code=LimitationCode.CONCLUSION_UNMAPPED, detail="d")
        single_lim = VerificationLimitation(code=LimitationCode.SINGLE_PROVIDER, detail="d")
        assert "causal step" in (
            VerificationEngine.next_check_for([relation_lim], [], []) or ""
        )
        # Relation outranks sentence mapping and single-provider corroboration.
        assert "causal step" in (
            VerificationEngine.next_check_for(
                [unmapped_lim, single_lim, relation_lim], [], ["slow cache"]
            )
            or ""
        )
        # An untested rival still outranks the relation check.
        rival_lim = VerificationLimitation(code=LimitationCode.RIVAL_UNTESTED, detail="d")
        assert "untested rival" in (
            VerificationEngine.next_check_for(
                [rival_lim, relation_lim], ["slow downstream"], []
            )
            or ""
        )


class TestAbstentionNextCheck:
    def test_all_rejected_directs_new_hypothesis(self):
        out = VerificationEngine.next_check_for_abstention(
            "refuted", ["all hypotheses rejected or contradicted"]
        )
        assert out is not None and "consistent with the refuting evidence" in out

    def test_refuting_items_direct_resolve(self):
        out = VerificationEngine.next_check_for_abstention(
            "refuted", ["3 refuting evidence item(s) bound"]
        )
        assert out is not None and "3 refuting evidence item(s) bound" in out

    def test_bare_refuted_falls_back(self):
        out = VerificationEngine.next_check_for_abstention("refuted", ["stale"])
        assert out == "Resolve the refuting evidence, then re-verify."

    def test_no_bound_evidence(self):
        out = VerificationEngine.next_check_for_abstention(
            "insufficient", ["no bound supporting evidence"]
        )
        assert out is not None and "Cite and bind supporting evidence" in out

    def test_no_causal_chain(self):
        out = VerificationEngine.next_check_for_abstention(
            "insufficient", ["no validated causal chain"]
        )
        assert out is not None and "cited evidence for each link" in out

    def test_gate_blocker_passthrough_truncates(self):
        blocker = "x" * 400
        out = VerificationEngine.next_check_for_abstention("insufficient", [blocker])
        assert out == f"Resolve '{'x' * 300}', then re-verify."

    def test_refuting_beats_no_bound_evidence(self):
        out = VerificationEngine.next_check_for_abstention(
            "refuted",
            ["no bound supporting evidence", "2 refuting evidence item(s) bound"],
        )
        assert out is not None and "2 refuting evidence item(s) bound" in out

    def test_empty_reasons_yields_none(self):
        assert VerificationEngine.next_check_for_abstention("insufficient", []) is None
        assert VerificationEngine.next_check_for_abstention("insufficient", None) is None

    def test_never_raises_on_garbage(self):
        out = VerificationEngine.next_check_for_abstention(None, [None, 123, {}])
        assert out is None or isinstance(out, str)

    def test_abstention_statuses_accept_next_check(self):
        for status in ("INSUFFICIENT_EVIDENCE", "NO_ROOT_CAUSE_FOUND"):
            conclusion = InvestigationConclusion(
                tenant_id="t",
                investigation_id=uuid4(),
                status=status,
                root_cause=None,
                supporting_evidence_ids=[],
                limitations=["no bound supporting evidence"],
                next_check="Cite and bind supporting evidence, then re-verify.",
            )
            assert conclusion.next_check is not None


def _range_row(snippet="10| if retryCount > 0:\n11|     reserve()"):
    iid = uuid4()
    now = datetime.now(UTC)
    return Evidence(
        tenant_id="t",
        investigation_id=iid,
        evidence_type=EvidenceType.SOURCE_CODE,
        provider="test",
        source="test://pool.py",
        title="t",
        summary="range",
        content_snippet=snippet,
        observed_at=now,
        retrieved_at=now,
        provenance=EvidenceProvenance(
            tenant_id="t",
            investigation_id=iid,
            provider_type="test",
            requested_provider_id="test",
            actual_provider_id="test",
            source_system="test",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="test", operation="GET_SOURCE", normalized_query_hash="h"
            ),
            source_location=SourceLocation(
                system="test", identifier="x", file_path="pool.py",
                line_start=10, line_end=11, revision="abc",
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=f"fp-{uuid4()}",
        attributes={"read_mode": "range"},
    )


class TestBehaviorNoRuntimeDemotion:
    def _chain(self, ids):
        return [
            CausalRelationship(
                cause="retry re-enters reserve",
                effect="client timeout",
                mechanism="consumed reservation id blocks",
                confidence=0.9,
                evidence_ids=list(ids),
            )
        ]

    def test_code_only_rival_tested_demotes_to_likely(self):
        engine = VerificationEngine()
        code = _range_row()
        items = [code] + [_ev(EvidenceType.DOCUMENTATION, "p1", snippet="runbook note") for _ in range(5)]
        candidate = _hyp(
            "Retry re-enters reserve with a consumed id",
            HypothesisStatus.SUPPORTED,
            assessed_ids=[code.evidence_id],
            claim_kind=ClaimKind.BEHAVIOR,
        )
        rival = _hyp("Downstream latency", assessed_ids=[items[1].evidence_id])
        result = engine.evaluate_hypothesis(
            candidate, items, [], self._chain([code.evidence_id]),
            RootCauseVerificationPolicy(), sibling_hypotheses=[rival],
        )
        assert result.status == VerificationStatus.VERIFIED
        assert result.tier == "likely"
        assert any(lim.code == LimitationCode.NO_RUNTIME_OBSERVATION for lim in result.limitations)
        assert "NO_RUNTIME_OBSERVATION" in result.explanation

    def test_code_only_without_rival_stays_inconclusive(self):
        engine = VerificationEngine()
        code = _range_row()
        items = [code] + [_ev(EvidenceType.DOCUMENTATION, "p1", snippet="runbook note") for _ in range(5)]
        candidate = _hyp(
            "Retry re-enters reserve with a consumed id",
            HypothesisStatus.SUPPORTED,
            assessed_ids=[code.evidence_id],
            claim_kind=ClaimKind.BEHAVIOR,
        )
        result = engine.evaluate_hypothesis(
            candidate, items, [], self._chain([code.evidence_id]),
            RootCauseVerificationPolicy(),
        )
        assert result.status == VerificationStatus.INCONCLUSIVE

    def test_head_only_with_rival_stays_inconclusive(self):
        engine = VerificationEngine()
        head = _range_row(snippet="head without numbering")
        object.__setattr__(
            head, "provenance",
            EvidenceProvenance(
                tenant_id="t", investigation_id=uuid4(), provider_type="test",
                requested_provider_id="test", actual_provider_id="test",
                source_system="test", retrieval_timestamp=datetime.now(UTC),
                query_fingerprint=QueryFingerprint(
                    provider_type="test", operation="GET_SOURCE", normalized_query_hash="h"),
                source_location=SourceLocation(system="test", identifier="x"),
            ),
        )
        items = [head] + [_ev(EvidenceType.DOCUMENTATION, "p1", snippet="runbook note") for _ in range(5)]
        candidate = _hyp(
            "Retry re-enters reserve",
            HypothesisStatus.SUPPORTED,
            assessed_ids=[head.evidence_id],
            claim_kind=ClaimKind.BEHAVIOR,
        )
        rival = _hyp("Downstream latency", assessed_ids=[items[1].evidence_id])
        result = engine.evaluate_hypothesis(
            candidate, items, [], self._chain([head.evidence_id]),
            RootCauseVerificationPolicy(), sibling_hypotheses=[rival],
        )
        assert result.status == VerificationStatus.INCONCLUSIVE

    def test_next_check_names_runtime_observation(self):
        lim = VerificationLimitation(code=LimitationCode.NO_RUNTIME_OBSERVATION, detail="d")
        out = VerificationEngine.next_check_for([lim], [], [])
        assert out is not None and "runtime" in out
