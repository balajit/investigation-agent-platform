# tests/unit/test_h12_h1_decisions.py
"""H12 (declined rival bar) and H1 (default cbv1 pin): decision pins."""

from types import SimpleNamespace
from uuid import uuid4


def test_prose_against_evidence_writes_no_refuting_ids() -> None:
    from investigation_agent_platform.application.investigation.verification import (
        VerificationEngine,
        VerificationStatus,
        apply_verification_outcome,
    )
    from investigation_agent_platform.domain.hypothesis.models import HypothesisStatus

    VerificationEngine()
    hyp = SimpleNamespace(id=uuid4(), status=HypothesisStatus.PROPOSED)
    result = SimpleNamespace(
        status=VerificationStatus.VERIFIED, confidence=SimpleNamespace(overall_confidence=0.9, coverage_score=0.5),
        corroborating_evidence_ids=[], against_evidence=["pool is exhausted"],
        limitations=[],
    )
    out = apply_verification_outcome(hyp, result)
    assert getattr(out, "refuting_evidence_ids", []) in ([], ())
    # Negative control: parsing prose with a loose regex into ids would write some.
    assert "pool is exhausted" not in [str(e) for e in (getattr(out, "refuting_evidence_ids", None) or [])]


def test_causal_verdict_at_bar_publishes_likely() -> None:
    from test_verification_scoring import _evidence, _hyp

    from investigation_agent_platform.application.investigation.verification import (
        RootCauseVerificationPolicy,
        VerificationEngine,
        VerificationStatus,
    )
    from investigation_agent_platform.domain.evidence.models import EvidenceType

    engine = VerificationEngine()
    items = _evidence("FILE_LOG", EvidenceType.LOG, 4) + _evidence("POSTGRES", EvidenceType.DATABASE_STATE, 4)
    candidate = _hyp()
    sibling = _hyp()
    result = engine.evaluate_hypothesis(candidate, items, [], [], RootCauseVerificationPolicy(), [sibling])
    assert result.status == VerificationStatus.VERIFIED
    assert result.tier == "likely"


def test_default_is_cbv1() -> None:
    from investigation_agent_platform.application.investigation.verification import (
        CLAIM_BINDING_CURRENT,
    )

    assert CLAIM_BINDING_CURRENT == "cbv1"


def test_all_writers_stamp_the_default() -> None:
    import inspect

    from investigation_agent_platform.api import dev_seed
    from investigation_agent_platform.application.investigation import services

    for module in (services, dev_seed):
        src = inspect.getsource(module)
        assert '"cbv1"' not in src, module.__name__
    assert "CLAIM_BINDING_CURRENT" in inspect.getsource(services)
    assert "CLAIM_BINDING_CURRENT" in inspect.getsource(dev_seed)
