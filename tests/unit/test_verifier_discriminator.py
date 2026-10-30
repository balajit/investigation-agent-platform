"""Offline discriminator for unified and historical caller-edge semantics."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

from investigation_agent_platform.application.investigation.verification import (
    VerificationEngine,
)
from investigation_agent_platform.domain.evidence.models import EvidenceType
from investigation_agent_platform.domain.hypothesis.models import (
    AssessmentStrength,
    AssessmentType,
    ClaimKind,
    ClaimTarget,
    Hypothesis,
    HypothesisEvidenceAssessment,
    HypothesisStatus,
)

ENGINE = VerificationEngine()
PATH = "src/OrderQualifyHelper.java"
REVISION = "rev-current"


def _row(
    evidence_id: UUID,
    investigation_id: UUID,
    *,
    revision: str,
    line_start: int,
    line_end: int,
    tenant_id: str = "tenant-a",
    operation: str = "GET_CODE",
    snippet: str = "return handler.handle(orderId);",
) -> SimpleNamespace:
    now = SimpleNamespace()
    return SimpleNamespace(
        evidence_id=evidence_id,
        tenant_id=tenant_id,
        investigation_id=investigation_id,
        evidence_type=EvidenceType.SOURCE_CODE,
        content_snippet=snippet,
        source="sample-repository",
        attributes=(
            {"read_mode": "range"}
            if operation in ("GET_SOURCE", "GET_CODE") and line_start is not None
            else {}
        ),
        provenance=SimpleNamespace(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            source_location=SimpleNamespace(
                file_path=PATH,
                revision=revision,
                line_start=line_start,
                line_end=line_end,
                identifier=f"sample-repository/{PATH}",
            ),
            query_fingerprint=SimpleNamespace(operation=operation),
            retrieval_timestamp=now,
        ),
    )


def _case(
    *,
    anchor_revision: str = REVISION,
    showing_revision: str = REVISION,
    showing_start: int = 10,
    showing_end: int = 12,
    showing_tenant: str = "tenant-a",
) -> tuple[Hypothesis, list[SimpleNamespace]]:
    investigation_id = uuid4()
    anchor_id = uuid4()
    target = ClaimTarget(
        symbol="OrderServiceHandler.handle",
        counterpart="OrderQualifyHelper",
        file_path=PATH,
        line_start=10,
        line_end=12,
        commit_id=REVISION,
    )
    assessment = HypothesisEvidenceAssessment(
        hypothesis_id=uuid4(),
        evidence_id=anchor_id,
        assessment=AssessmentType.SUPPORTS,
        strength=AssessmentStrength.MODERATE,
        reason="caller edge candidate",
        claim_kind=ClaimKind.CALL_EDGE,
        claim_target=target,
    )
    hypothesis = Hypothesis(
        id=assessment.hypothesis_id,
        tenant_id="tenant-a",
        investigation_id=investigation_id,
        title="caller edge",
        description="caller edge",
        statement="OrderQualifyHelper calls OrderServiceHandler.handle",
        status=HypothesisStatus.UNDER_INVESTIGATION,
        supporting_evidence_ids=[anchor_id],
        assessments=[assessment],
    )
    return hypothesis, [
        _row(anchor_id, investigation_id, revision=anchor_revision, line_start=1, line_end=2),
        _row(
            uuid4(),
            investigation_id,
            revision=showing_revision,
            line_start=showing_start,
            line_end=showing_end,
            tenant_id=showing_tenant,
        ),
    ]


def _historical_caller_edge(
    hypothesis: Hypothesis, evidence_items: list[SimpleNamespace]
) -> tuple[bool, str]:
    """Comparator for pre-remediation caller proof: file-local, not exact-bound."""
    assessment = next(
        item for item in hypothesis.assessments if item.claim_kind == ClaimKind.CALL_EDGE
    )
    cited = next(item for item in evidence_items if item.evidence_id == assessment.evidence_id)
    rows = [cited]
    target = assessment.claim_target
    if target is not None:
        rows.extend(
            item
            for item in evidence_items
            if item is not cited
            and VerificationEngine._paths_match(
                str(target.file_path or ""), VerificationEngine._item_file(item)
            )
        )
    return VerificationEngine.call_site_proof(
        rows,
        target,
        str(getattr(hypothesis, "statement", "") or ""),
        legacy_extraction=False,
    )


def test_unified_vs_historical_caller_edge_discriminator() -> None:
    cases = {
        "complete": _case(),
        "wrong_revision": _case(showing_revision="rev-old"),
        "wrong_range": _case(showing_start=13, showing_end=15),
        "foreign_binding": _case(showing_tenant="tenant-b"),
    }
    outcomes = {
        name: {
            "unified": ENGINE.check_claim_kind(hypothesis, rows),
            "historical": _historical_caller_edge(hypothesis, rows),
        }
        for name, (hypothesis, rows) in cases.items()
    }

    assert outcomes["complete"]["unified"] == (True, "")
    assert outcomes["complete"]["historical"] == (True, "")
    for name in ("wrong_revision", "wrong_range", "foreign_binding"):
        assert outcomes[name]["unified"][0] is False
        assert outcomes[name]["historical"] == (True, "")


def test_discriminator_does_not_restore_historical_routing() -> None:
    hypothesis, rows = _case(showing_revision="rev-old")
    assert ENGINE.check_claim_kind(hypothesis, rows)[0] is False
    assert _historical_caller_edge(hypothesis, rows)[0] is True
