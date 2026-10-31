# tests/unit/test_h15_band_tier.py
"""H15: tolerant band, explicit two-step ESTABLISHED, zero-step CAUSAL rule (LV_H)."""

from fractions import Fraction
from types import SimpleNamespace
from uuid import uuid4

from investigation_agent_platform.application.investigation.verification import (
    InvestigationConfidence,
    RootCauseVerificationPolicy,
    VerificationEngine,
    VerificationStatus,
    band_at_least,
)
from investigation_agent_platform.application.worker.activities.inputs import (
    _policy_for_loop_version,
)
from investigation_agent_platform.domain.evidence.models import EvidenceType


def _conf(cov, rel, causal, pen):  # type: ignore[no-untyped-def]
    return InvestigationConfidence(
        coverage_score=cov, reliability_score=rel, causal_score=causal, contradiction_penalty=pen,
    )


def test_one_perfect_step_meets_the_floor() -> None:
    score = _conf(1, 1, 0.5, 0).overall_confidence
    assert score != 0.9  # the float accident this item fixes
    assert band_at_least(score, 0.90, tolerant=True) is True
    assert band_at_least(score, 0.90, tolerant=False) is False


def test_half_shared_perfect_claim_stays_verified() -> None:
    score = _conf(1, 1, 0.5, 0.5).overall_confidence
    assert score < 0.80  # computes 0.7999999999999999
    assert band_at_least(score, 0.80, tolerant=True) is True
    assert band_at_least(score, 0.80, tolerant=False) is False


def test_epsilon_is_not_a_loophole() -> None:
    assert band_at_least(0.7999, 0.80, tolerant=True) is False
    assert band_at_least(0.80, 0.80, tolerant=True) is True


def _hyp(kind="CAUSAL"):  # type: ignore[no-untyped-def]
    return SimpleNamespace(id=uuid4(), claim_kind=kind)


def _evidence(provider, kind, n):  # type: ignore[no-untyped-def]
    return [
        SimpleNamespace(
            evidence_id=uuid4(), provider=provider, evidence_type=kind,
            source=f"src://{provider}/{i}", provenance=SimpleNamespace(),
        )
        for i in range(n)
    ]


def test_zero_step_causal_is_inconclusive() -> None:
    engine = VerificationEngine()
    items = _evidence("FILE_LOG", EvidenceType.LOG, 4) + _evidence("POSTGRES", EvidenceType.DATABASE_STATE, 4)
    policy = RootCauseVerificationPolicy(tolerant_band=True, require_causal_step_for_causal_claims=True)
    result = engine.evaluate_hypothesis(_hyp("CAUSAL"), items, [], [], policy)
    assert result.status == VerificationStatus.INCONCLUSIVE
    assert "no causal step" in result.explanation


def test_zero_step_behavior_untouched() -> None:
    engine = VerificationEngine()
    items = _evidence("FILE_LOG", EvidenceType.LOG, 4) + _evidence("POSTGRES", EvidenceType.DATABASE_STATE, 4)
    policy = RootCauseVerificationPolicy(tolerant_band=True, require_causal_step_for_causal_claims=True)
    flagged = engine.evaluate_hypothesis(_hyp("BEHAVIOR"), items, [], [], policy)
    plain = engine.evaluate_hypothesis(_hyp("BEHAVIOR"), items, [], [], RootCauseVerificationPolicy())
    assert (flagged.status, flagged.explanation) == (plain.status, plain.explanation)


def test_zero_step_on_older_pin_still_verifies() -> None:
    engine = VerificationEngine()
    items = _evidence("FILE_LOG", EvidenceType.LOG, 4) + _evidence("POSTGRES", EvidenceType.DATABASE_STATE, 4)
    policy = _policy_for_loop_version(RootCauseVerificationPolicy(), {"loop_version": "lv7"})
    assert policy.tolerant_band is False
    result = engine.evaluate_hypothesis(_hyp("CAUSAL"), items, [], [], policy)
    assert result.status == VerificationStatus.VERIFIED


def test_policy_routing() -> None:
    base = RootCauseVerificationPolicy()
    for meta, tolerant in ({"loop_version": "lv7"}, False), ({"loop_version": "lv10"}, True), ({}, True), (None, True):
        out = _policy_for_loop_version(base, meta)
        assert out.tolerant_band is tolerant, meta
        assert out.require_causal_step_for_causal_claims is tolerant, meta
    for meta in ({"loop_version": "lv99"}, {"loop_version": 42}, "junk"):
        out = _policy_for_loop_version(base, meta)
        assert out.tolerant_band is False, meta


def test_sweep_regression() -> None:
    covs = [Fraction(i, 4) for i in range(5)]
    rels = {Fraction(k, d) for d in range(1, 9) for k in range(d + 1)}
    fixed = 0
    for cov in covs:
        for rel in rels:
            for pen in rels:
                for causal in (Fraction(0), Fraction(1, 2), Fraction(1)):
                    exact = Fraction(35, 100) * cov + Fraction(25, 100) * rel + Fraction(20, 100) * causal + Fraction(20, 100) * (1 - pen)
                    score = 0.35 * float(cov) + 0.25 * float(rel) + 0.20 * float(causal) + 0.20 * (1.0 - float(pen))
                    for floor in (Fraction(4, 5), Fraction(9, 10)):
                        exact_hit = exact >= floor
                        assert band_at_least(score, float(floor), tolerant=True) == bool(exact_hit), (cov, rel, pen, causal, floor)
                        if exact_hit and not score >= float(floor):
                            fixed += 1
    assert fixed >= 6  # the documented lattice points the band repairs
