# tests/unit/test_h17_r4_contradiction_blocker.py
"""R4 (H17/H11): a linked contradiction blocks the one group it names.

An unresolved MEDIUM-or-worse contradiction whose evidence intersects
exactly one group's cited rows fails that group. LOW notes, resolved rows,
and evidence shared across groups never block.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


def _contradiction(status="UNRESOLVED", severity="MEDIUM", evidence=()):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        resolution_status=status,
        severity=SimpleNamespace(value=severity),
        statement="the handler cannot both define and lack the symbol",
        evidence_ids=list(evidence),
    )


def _groups():  # type: ignore[no-untyped-def]
    row_a, row_b = uuid4(), uuid4()
    cited = {
        ("DEFINITION", "g1"): {str(row_a)},
        ("CALL_EDGE", "g2"): {str(row_b)},
    }
    return cited, row_a, row_b


def test_r4_medium_linking_one_group_blocks_it() -> None:
    from investigation_agent_platform.application.investigation.cross_claim import (
        contradiction_blocks_group,
    )

    cited, row_a, _ = _groups()
    detail = contradiction_blocks_group(
        ("DEFINITION", "g1"), cited, [_contradiction(evidence=[row_a])]
    )
    assert detail is not None and "contradiction" in detail


def test_r4_other_group_is_not_blocked() -> None:
    from investigation_agent_platform.application.investigation.cross_claim import (
        contradiction_blocks_group,
    )

    cited, row_a, _ = _groups()
    assert (
        contradiction_blocks_group(("CALL_EDGE", "g2"), cited, [_contradiction(evidence=[row_a])])
        is None
    )


def test_r4_evidence_shared_across_groups_blocks_nothing() -> None:
    from investigation_agent_platform.application.investigation.cross_claim import (
        contradiction_blocks_group,
    )

    cited, row_a, row_b = _groups()
    both = [_contradiction(evidence=[row_a, row_b])]
    assert contradiction_blocks_group(("DEFINITION", "g1"), cited, both) is None
    assert contradiction_blocks_group(("CALL_EDGE", "g2"), cited, both) is None


def test_r4_low_note_never_blocks() -> None:
    from investigation_agent_platform.application.investigation.cross_claim import (
        contradiction_blocks_group,
    )

    cited, row_a, _ = _groups()
    assert (
        contradiction_blocks_group(
            ("DEFINITION", "g1"), cited, [_contradiction(severity="LOW", evidence=[row_a])]
        )
        is None
    )


def test_r4_resolved_row_never_blocks() -> None:
    from investigation_agent_platform.application.investigation.cross_claim import (
        contradiction_blocks_group,
    )

    cited, row_a, _ = _groups()
    assert (
        contradiction_blocks_group(
            ("DEFINITION", "g1"), cited, [_contradiction(status="RESOLVED", evidence=[row_a])]
        )
        is None
    )


def test_r4_high_blocks() -> None:
    from investigation_agent_platform.application.investigation.cross_claim import (
        contradiction_blocks_group,
    )

    cited, row_a, _ = _groups()
    assert (
        contradiction_blocks_group(
            ("DEFINITION", "g1"), cited, [_contradiction(severity="HIGH", evidence=[row_a])]
        )
        is not None
    )


@pytest.mark.asyncio
async def test_r4_wiring_withdraws_the_group_the_contradiction_names() -> None:
    """R4 wiring through enforce_group_conjunction: validators pass, but the
    named group is withdrawn. Uses the H17-T14 spy pattern for _group_verifies
    so the test pins the R4 wiring, not the validators."""
    from test_claim_binding_phase3 import _code_row, _def_hyp

    from investigation_agent_platform.application.investigation.verification import (
        Contradiction,
        ContradictionSeverity,
        InvestigationConfidence,
        RootCauseVerificationPolicy,
        VerificationEngine,
        VerificationResult,
        VerificationStatus,
    )
    from investigation_agent_platform.domain.hypothesis.models import (
        AssessmentStrength,
        AssessmentType,
        ClaimKind,
        ClaimTarget,
        HypothesisEvidenceAssessment,
    )

    inv_id = uuid4()
    read_a = _code_row(
        "GET_CODE", "src/a.py", "abc123", "40| def handleAlpha(req):", start=40, end=42
    )
    read_b = _code_row(
        "GET_CODE", "src/b.py", "abc123", "10| def handleBeta(req):", start=10, end=12
    )
    base = _def_hyp(read_a, statement="handlers are defined")
    base = base.model_copy(update={"investigation_id": inv_id, "tenant_id": "t"})
    asm_b = HypothesisEvidenceAssessment(
        hypothesis_id=base.id,
        evidence_id=read_b.evidence_id,
        assessment=AssessmentType.SUPPORTS,
        strength=AssessmentStrength.STRONG,
        reason="defines handler",
        claim_kind=ClaimKind.DEFINITION,
        claim_target=ClaimTarget(symbol="handleBeta", file_path="src/b.py"),
    )
    hyp = base.model_copy(
        update={
            "assessments": [*base.assessments, asm_b],
            "supporting_evidence_ids": [read_a.evidence_id, read_b.evidence_id],
        }
    )
    engine = VerificationEngine()
    policy = RootCauseVerificationPolicy(cross_claim_checks=True)
    result = VerificationResult(
        hypothesis_id=hyp.id,
        status=VerificationStatus.VERIFIED,
        explanation="both groups verified",
        confidence=InvestigationConfidence(
            coverage_score=1.0, reliability_score=1.0, causal_score=1.0,
            contradiction_penalty=0.0,
        ),
    )
    contradiction = Contradiction(
        statement="row A cannot define handleAlpha",
        evidence_ids=[read_a.evidence_id],
        severity=ContradictionSeverity.MEDIUM,
    )

    orig = VerificationEngine._group_verifies
    VerificationEngine._group_verifies = lambda self, *a, **k: True  # type: ignore[method-assign]
    try:
        clean = engine.enforce_group_conjunction(
            hyp, result, [read_a, read_b], [], [], policy
        )
        assert clean.status == VerificationStatus.VERIFIED
        blocked = engine.enforce_group_conjunction(
            hyp, result, [read_a, read_b], [contradiction], [], policy
        )
    finally:
        VerificationEngine._group_verifies = orig  # type: ignore[method-assign]
    assert blocked.status == VerificationStatus.INCONCLUSIVE
    details = " ".join(
        str(getattr(lim, "detail", "")) for lim in (blocked.limitations or [])
    )
    assert "contradiction" in details
    assert "src/a.py" in details or str(read_a.evidence_id)[:8] in details
