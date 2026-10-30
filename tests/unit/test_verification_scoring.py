# tests/unit/test_verification_scoring.py
"""VerificationEngine cross-layer scoring: independent providers required.

Regression for self-investigation d16f044f, which published a VERIFIED
single-capability conclusion (ten lines from one log file counted as
cross-layer via a source-count escape hatch) without ever checking state
or code. Distinct source URIs from one provider are volume, not
corroboration.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from investigation_agent_platform.application.investigation.verification import (
    RootCauseVerificationPolicy,
    VerificationEngine,
    VerificationStatus,
)
from investigation_agent_platform.domain.evidence.models import EvidenceType


def _hyp():  # type: ignore[no-untyped-def]
    # P3 (S6 shape branch): heuristic scoring adjudicates typed claims
    # only; these scoring tests exercise causal machinery.
    return SimpleNamespace(id=uuid4(), claim_kind="CAUSAL")


def _evidence(provider: str, kind: EvidenceType, n: int):  # type: ignore[no-untyped-def]
    return [
        SimpleNamespace(
            evidence_id=uuid4(),
            provider=provider,
            evidence_type=kind,
            source=f"src://{provider}/{i}",
            provenance=SimpleNamespace(),
        )
        for i in range(n)
    ]


class TestCrossLayerScoring:
    def test_single_provider_cannot_verify(self) -> None:
        engine = VerificationEngine()
        result = engine.evaluate_hypothesis(
            _hyp(),
            _evidence("FILE_LOG", EvidenceType.LOG, 10),
            [],
            [],
            RootCauseVerificationPolicy(),
        )
        assert result.status != VerificationStatus.VERIFIED
        assert result.status == VerificationStatus.INCONCLUSIVE

    def test_two_independent_layers_verify(self) -> None:
        engine = VerificationEngine()
        items = _evidence("FILE_LOG", EvidenceType.LOG, 4) + _evidence(
            "POSTGRES", EvidenceType.DATABASE_STATE, 4
        )
        result = engine.evaluate_hypothesis(_hyp(), items, [], [], RootCauseVerificationPolicy())
        assert result.status == VerificationStatus.VERIFIED

    def test_policy_opt_out_restores_leniency(self) -> None:
        engine = VerificationEngine()
        policy = RootCauseVerificationPolicy(require_cross_layer_evidence=False)
        result = engine.evaluate_hypothesis(
            _hyp(), _evidence("FILE_LOG", EvidenceType.LOG, 10), [], [], policy
        )
        assert result.status == VerificationStatus.VERIFIED

    def test_two_sources_one_provider_insufficient(self) -> None:
        # Ten distinct URIs from one provider must not corroborate (the
        # removed escape hatch counted these as cross-layer).
        engine = VerificationEngine()
        result = engine.evaluate_hypothesis(
            _hyp(),
            _evidence("FILE_LOG", EvidenceType.LOG, 10),
            [],
            [],
            RootCauseVerificationPolicy(),
        )
        assert "coverage=0.50" in result.explanation
