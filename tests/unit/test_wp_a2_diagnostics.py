"""WP-A2 diagnostics (Stage A, additive-only): G2 gate tests.

Covers the G2 exit criteria: stable reason-code vocabulary, tolerant
readers for versioned assessment fields (legacy fixtures still load),
and no verdict change from diagnostics (ambiguous stays exactly as
evaluated — no new false accepts).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from investigation_agent_platform.application.investigation.verification import (
    Demotion,
    DiagnosticReasonCode,
    EvaporatedId,
    InvestigationConfidence,
    LimitationCode,
    MissingObligation,
    RootCauseVerificationPolicy,
    VerificationDiagnostics,
    VerificationEngine,
    VerificationLimitation,
    VerificationResult,
    VerificationStatus,
    collect_verification_diagnostics,
)
from investigation_agent_platform.domain.hypothesis.models import (
    AssessmentStrength,
    AssessmentType,
    ClaimKind,
    Hypothesis,
    HypothesisEvidenceAssessment,
)


def _hyp(**over: Any) -> Hypothesis:
    base: dict[str, Any] = {
        "tenant_id": "tenant-a",
        "investigation_id": uuid4(),
        "title": "t",
        "statement": "The cache causes the outage",
        "description": "d",
    }
    base.update(over)
    return Hypothesis(**base)


def _conf() -> InvestigationConfidence:
    return InvestigationConfidence(
        coverage_score=0.6,
        reliability_score=0.7,
        causal_score=0.5,
        contradiction_penalty=0.0,
    )


def _result(status: VerificationStatus, **over: Any) -> VerificationResult:
    base: dict[str, Any] = {
        "hypothesis_id": uuid4(),
        "status": status,
        "corroborating_evidence_ids": [],
        "explanation": "e",
        "confidence": _conf(),
    }
    base.update(over)
    return VerificationResult(**base)


def test_reason_code_vocabulary_is_frozen() -> None:
    assert sorted(c.value for c in DiagnosticReasonCode) == [
        "CITED_ID_EVAPORATED",
        "OBLIGATION_MISSING",
        "TIER_DEMOTED",
    ]


def test_legacy_assessment_row_loads_without_fabricated_defaults() -> None:
    hid, eid = uuid4(), uuid4()
    legacy = {
        "hypothesis_id": str(hid),
        "evidence_id": str(eid),
        "assessment": "SUPPORTS",
        "strength": "WEAK",
        "reason": "r",
    }
    asm = HypothesisEvidenceAssessment.model_validate(legacy)
    assert asm.schema_version is None
    assert asm.reason_code is None
    assert asm.assessment == AssessmentType.SUPPORTS


def test_new_assessment_fields_round_trip_and_tolerate_future_codes() -> None:
    asm = HypothesisEvidenceAssessment(
        hypothesis_id=uuid4(),
        evidence_id=uuid4(),
        assessment=AssessmentType.SUPPORTS,
        strength=AssessmentStrength.STRONG,
        reason="r",
        schema_version="v1",
        reason_code="CITED_ID_EVAPORATED",
    )
    assert HypothesisEvidenceAssessment.model_validate(asm.model_dump()).reason_code == (
        "CITED_ID_EVAPORATED"
    )
    # Free-form str: an unknown future code still validates (reader tolerance).
    future = dict(asm.model_dump(), reason_code="SOME_FUTURE_CODE")
    assert HypothesisEvidenceAssessment.model_validate(future).reason_code == (
        "SOME_FUTURE_CODE"
    )


def test_collector_records_demotions_with_causes() -> None:
    hid = uuid4()
    hyp = _hyp()
    result = _result(
        VerificationStatus.VERIFIED,
        hypothesis_id=hid,
        tier="likely",
        limitations=[
            VerificationLimitation(code=LimitationCode.SINGLE_PROVIDER, detail="one provider")
        ],
    )
    diag = collect_verification_diagnostics(hyp, [], result)
    assert diag is not None
    assert len(diag.demotions) == 1
    dem = diag.demotions[0]
    assert dem.from_tier == "established"
    assert dem.to_tier == "likely"
    assert dem.reason_code == DiagnosticReasonCode.TIER_DEMOTED
    assert dem.cause_codes == ["SINGLE_PROVIDER"]
    assert "TIER_DEMOTED" in diag.reason_codes
    assert diag.schema_version == "v1"


def test_collector_returns_none_when_clean() -> None:
    eid = uuid4()
    hyp = _hyp(supporting_evidence_ids=[eid])
    # bound below via namespace evidence carrying the same id
    evidence = [SimpleNamespace(evidence_id=eid)]
    result = _result(VerificationStatus.SUPPORTED, corroborating_evidence_ids=[eid])
    assert collect_verification_diagnostics(hyp, evidence, result) is None


def test_engine_reports_evaporated_ids_and_missing_obligation() -> None:
    ghost = uuid4()
    hyp = _hyp(
        supporting_evidence_ids=[ghost],
        assessments=[
            HypothesisEvidenceAssessment(
                hypothesis_id=uuid4(),
                evidence_id=ghost,
                assessment=AssessmentType.SUPPORTS,
                strength=AssessmentStrength.WEAK,
                reason="r",
            )
        ],
    )
    result = VerificationEngine().evaluate_hypothesis(
        hyp, [], [], [], RootCauseVerificationPolicy()
    )
    assert result.status == VerificationStatus.INCONCLUSIVE
    assert result.diagnostics is not None
    assert [e.cited_id for e in result.diagnostics.evaporated_ids] == [str(ghost)]
    assert result.diagnostics.evaporated_ids[0].reason_code == (
        DiagnosticReasonCode.CITED_ID_EVAPORATED
    )
    assert len(result.diagnostics.missing_obligations) == 1
    missing = result.diagnostics.missing_obligations[0]
    assert missing.claim == "The cache causes the outage"
    assert missing.next_action == "Bind supporting evidence to the claim, then re-verify."
    assert set(result.diagnostics.reason_codes) == {
        "CITED_ID_EVAPORATED",
        "OBLIGATION_MISSING",
    }


def test_missing_obligation_directive_follows_claim_kind() -> None:
    ghost = uuid4()
    hyp = _hyp(
        supporting_evidence_ids=[ghost],
        assessments=[
            HypothesisEvidenceAssessment(
                hypothesis_id=uuid4(),
                evidence_id=ghost,
                assessment=AssessmentType.SUPPORTS,
                strength=AssessmentStrength.WEAK,
                reason="r",
                claim_kind=ClaimKind.CALL_EDGE,
            )
        ],
    )
    result = VerificationEngine().evaluate_hypothesis(
        hyp, [], [], [], RootCauseVerificationPolicy()
    )
    assert result.diagnostics is not None
    assert len(result.diagnostics.missing_obligations) == 1
    missing = result.diagnostics.missing_obligations[0]
    assert missing.claim_kind == "CALL_EDGE"
    assert missing.next_action == (
        "Bind the range-read row showing the call expression, then re-verify."
    )


def test_diagnostics_always_attached_and_verdict_stable() -> None:
    ghost = uuid4()
    hyp = _hyp(supporting_evidence_ids=[ghost])
    engine = VerificationEngine()
    policy = RootCauseVerificationPolicy()
    first = engine.evaluate_hypothesis(hyp, [], [], [], policy)
    second = engine.evaluate_hypothesis(hyp, [], [], [], policy)
    # No flag, no opt-out: diagnostics always attached, verdicts identical.
    assert first.diagnostics is not None
    for field in (
        "status",
        "tier",
        "explanation",
        "corroborating_evidence_ids",
        "best_reason",
        "against_evidence",
    ):
        assert getattr(first, field) == getattr(second, field)
    assert first.confidence.overall_confidence == second.confidence.overall_confidence
    # Ambiguous stays ambiguous: diagnostics never promote.
    assert first.status == VerificationStatus.INCONCLUSIVE


def test_diagnostic_models_hold_bounds() -> None:
    assert EvaporatedId(cited_id="x").reason_code == DiagnosticReasonCode.CITED_ID_EVAPORATED
    assert Demotion(hypothesis_id=uuid4()).to_tier == "likely"
    assert MissingObligation(claim="c").reason_code == DiagnosticReasonCode.OBLIGATION_MISSING
    assert VerificationDiagnostics().schema_version == "v1"
