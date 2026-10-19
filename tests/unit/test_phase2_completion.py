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


def _hyp(statement, status=HypothesisStatus.PROPOSED, assessed_ids=()):
    h_id = uuid4()
    return Hypothesis(
        id=h_id,
        tenant_id="t",
        investigation_id=uuid4(),
        title=statement[:50],
        description=statement,
        statement=statement,
        status=status,
        supporting_evidence_ids=list(assessed_ids),
        assessments=[
            HypothesisEvidenceAssessment(
                hypothesis_id=h_id,
                evidence_id=eid,
                assessment=AssessmentType.SUPPORTS,
                strength=AssessmentStrength.MODERATE,
                reason="cited",
            )
            for eid in assessed_ids
        ],
    )


def _verifiable_setup():
    items = [_ev(EvidenceType.RUNTIME_LOG, "p1") for _ in range(3)]
    items += [_ev(EvidenceType.METRIC, "p2", snippet="pool saturation 99") for _ in range(3)]
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
        candidate = _hyp("Pool exhaustion causes checkout latency", HypothesisStatus.SUPPORTED)
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
        candidate = _hyp("Pool exhaustion causes checkout latency", HypothesisStatus.SUPPORTED)
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
        candidate = _hyp("Pool exhaustion causes latency", HypothesisStatus.SUPPORTED)
        policy = RootCauseVerificationPolicy(require_cross_layer_evidence=False)
        result = engine.evaluate_hypothesis(candidate, items, [], chain, policy)
        assert result.status == VerificationStatus.VERIFIED
        assert any(lim.code == LimitationCode.SINGLE_PROVIDER for lim in result.limitations)
        assert "SINGLE_PROVIDER" in result.explanation

    def test_verified_explanation_names_limitations_not_just_counts(self):
        engine = VerificationEngine()
        items, chain = _verifiable_setup()
        candidate = _hyp("Pool exhaustion causes checkout latency", HypothesisStatus.SUPPORTED)
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
