# tests/unit/test_history_absence.py
"""HISTORY absence proof (D1, claim-proof gaps).

File-level negative history findings ("no recent change to F") verify via
a scoped empty search; commit-naming targets, incomplete searches, and
cbv1 rows fall through exactly as before.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from investigation_agent_platform.application.investigation.verification import (
    LimitationCode,
    VerificationEngine,
    VerificationStatus,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.hypothesis.models import (
    AssessmentStrength,
    AssessmentType,
    ClaimKind,
    ClaimTarget,
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

TIP = "54d73240d6b5b03b899bcaf0e0c4e1cdc31be9fd"
FILE = "service/src/main/java/com/sample/orders/service/OrderServiceImpl.java"


def _row(
    *,
    path: str = FILE,
    revision: str | None = TIP,
    match_count: int = 0,
    discovery_only: bool = False,
    etype: EvidenceType = EvidenceType.COMMIT_HISTORY,
) -> Evidence:
    iid = uuid4()
    now = datetime.now(UTC)
    return Evidence(
        tenant_id="t",
        investigation_id=iid,
        evidence_type=etype,
        provider="git",
        source="test://git",
        title="t",
        summary="s",
        content_snippet="s",
        observed_at=now,
        retrieved_at=now,
        attributes={"match_count": match_count, **({"discovery_only": True} if discovery_only else {})},
        provenance=EvidenceProvenance(
            tenant_id="t",
            investigation_id=iid,
            provider_type="GIT",
            requested_provider_id="git",
            actual_provider_id="git",
            source_system="Git",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="GIT", operation="GET_CODE_HISTORY", normalized_query_hash="h"
            ),
            source_location=SourceLocation(
                system="Git", identifier="x", file_path=path, revision=revision
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=f"fp-{uuid4()}",
    )


def _target(**kw) -> ClaimTarget:
    return ClaimTarget(file_path=FILE, **kw)


class TestHistoryAbsenceOutcome:
    def test_inspected_empty(self) -> None:
        outcome, _, rows = VerificationEngine.history_absence_outcome(
            [_row()], _target()
        )
        assert outcome == "INSPECTED_EMPTY"
        assert len(rows) == 1

    def test_no_rows(self) -> None:
        outcome, _, rows = VerificationEngine.history_absence_outcome([], _target())
        assert outcome == "NO_SEARCH"
        assert rows == []

    def test_wrong_file(self) -> None:
        outcome, _, _ = VerificationEngine.history_absence_outcome(
            [_row(path="other/X.java")], _target()
        )
        assert outcome == "NO_SEARCH"

    def test_discovery_only(self) -> None:
        outcome, _, _ = VerificationEngine.history_absence_outcome(
            [_row(discovery_only=True)], _target()
        )
        assert outcome == "SEARCH_INCOMPLETE"

    def test_revision_less_row(self) -> None:
        outcome, _, _ = VerificationEngine.history_absence_outcome(
            [_row(revision=None)], _target()
        )
        assert outcome == "SEARCH_INCOMPLETE"

    def test_positive_row_blocks_absence(self) -> None:
        positive = _row(match_count=0)
        positive = positive.model_copy(
            update={"attributes": dict(positive.attributes, match_count=3)}
        )
        outcome, _, _ = VerificationEngine.history_absence_outcome(
            [_row(), positive], _target()
        )
        assert outcome == "SEARCH_INCOMPLETE"

    def test_commit_naming_target_deferred(self) -> None:
        outcome, _, _ = VerificationEngine.history_absence_outcome(
            [_row()], _target(commit_id="abc123")
        )
        assert outcome == "SEARCH_INCOMPLETE"

    def test_no_target(self) -> None:
        outcome, _, _ = VerificationEngine.history_absence_outcome([_row()], None)
        assert outcome == "NO_SEARCH"

    def test_non_history_rows_ignored(self) -> None:
        outcome, _, _ = VerificationEngine.history_absence_outcome(
            [_row(etype=EvidenceType.SOURCE_CODE)], _target()
        )
        assert outcome == "NO_SEARCH"


def _hyp(target: ClaimTarget, *cited_ids) -> Hypothesis:
    iid = uuid4()
    hyp = Hypothesis(
        tenant_id="t",
        investigation_id=iid,
        title="no recent change",
        description="no recent change",
        statement="No recent change to OrderServiceImpl.",
        status=HypothesisStatus.PROPOSED,
        supporting_evidence_ids=list(cited_ids),
    )
    asm = HypothesisEvidenceAssessment(
        hypothesis_id=hyp.id,
        evidence_id=cited_ids[0] if cited_ids else uuid4(),
        assessment=AssessmentType.SUPPORTS,
        strength=AssessmentStrength.MODERATE,
        reason="negative recent change",
        claim_kind=ClaimKind.HISTORY,
        claim_target=target,
    )
    return hyp.model_copy(update={"assessments": [asm]})


class TestValidateHistoryAbsence:
    def test_inspected_empty_verifies_under_cbv2(self) -> None:
        row = _row()
        hyp = _hyp(_target(), row.evidence_id)
        engine = VerificationEngine(claim_binding_version="cbv2")
        result = engine.validate_history_claim(hyp, [row], [])
        assert result is not None
        assert result.status == VerificationStatus.VERIFIED
        assert result.corroborating_evidence_ids == [row.evidence_id]
        assert result.explanation.startswith("HISTORY_ABSENCE_PROVED:")
        assert TIP[:8] in result.explanation
        assert LimitationCode.HISTORY_ABSENCE_PROVED in [
            getattr(lim, "code", None) for lim in result.limitations
        ]

    def test_cbv1_falls_through(self) -> None:
        row = _row()
        hyp = _hyp(_target(), row.evidence_id)
        engine = VerificationEngine(claim_binding_version="cbv1")
        assert engine.validate_history_claim(hyp, [row], []) is None

    def test_commit_naming_target_skips_absence(self) -> None:
        row = _row()
        hyp = _hyp(_target(commit_id="abc123"), row.evidence_id)
        engine = VerificationEngine(claim_binding_version="cbv2")
        assert engine.validate_history_claim(hyp, [row], []) is None

    def test_no_rows_falls_through(self) -> None:
        hyp = _hyp(_target(), uuid4())
        engine = VerificationEngine(claim_binding_version="cbv2")
        assert engine.validate_history_claim(hyp, [], []) is None

    def test_concession_still_refuses(self) -> None:
        row = _row()
        hyp = _hyp(_target(), row.evidence_id)
        hyp = hyp.model_copy(update={"statement": "Could not verify any recent change"})
        engine = VerificationEngine(claim_binding_version="cbv2")
        assert engine.concedes_claim(hyp.statement) is True
        assert engine.validate_history_claim(hyp, [row], []) is None

    def test_positive_path_untouched(self) -> None:
        positive = _row(match_count=3)
        positive = positive.model_copy(
            update={
                "summary": "Fix filter edge case",
                "content_snippet": "Fix filter edge case",
                "provenance": positive.provenance.model_copy(
                    update={
                        "source_location": positive.provenance.source_location.model_copy(
                            update={"revision": "abc123def"}
                        )
                    }
                ),
            }
        )
        hyp = _hyp(
            ClaimTarget(
                file_path=FILE, commit_id="abc123", commit_subject="Fix filter edge case"
            ),
            positive.evidence_id,
        )
        engine = VerificationEngine(claim_binding_version="cbv2")
        result = engine.validate_history_claim(hyp, [positive], [])
        assert result is not None
        assert result.status == VerificationStatus.VERIFIED
        assert "HISTORY_ABSENCE_PROVED" not in result.explanation
