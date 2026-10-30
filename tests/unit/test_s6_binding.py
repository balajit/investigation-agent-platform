# tests/unit/test_s6_binding.py
"""P3 (S6 binding + S5 rival bar): no auto-pass, pinned ranges, elimination record."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from investigation_agent_platform.application.investigation.verification import (
    RootCauseVerificationPolicy,
    VerificationEngine,
    VerificationStatus,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.finding.models import EliminatedRival
from investigation_agent_platform.domain.hypothesis.models import Hypothesis, HypothesisStatus
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)


def _hyp(statement="cause is X", status=HypothesisStatus.PROPOSED, claim_kind=None):
    iid = uuid4()
    return Hypothesis(
        tenant_id="tenant-a",
        investigation_id=iid,
        title=statement[:256],
        description=statement,
        statement=statement,
        status=status,
        claim_kind=claim_kind,
    )


def _ev(etype=EvidenceType.LOG, file_path="", line_start=None, line_end=None, revision=""):
    iid = uuid4()
    now = datetime.now(UTC)
    return Evidence(
        tenant_id="tenant-a",
        investigation_id=iid,
        evidence_type=etype,
        provider="prov-a",
        source="urn:src",
        title="t",
        summary="s",
        fingerprint=f"fp-{uuid4()}",
        observed_at=now,
        retrieved_at=now,
        provenance=EvidenceProvenance(
            tenant_id="tenant-a",
            investigation_id=iid,
            provider_type="test",
            requested_provider_id="test",
            actual_provider_id="test",
            source_system="test",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="test", operation="test", normalized_query_hash="abc"
            ),
            source_location=SourceLocation(
                system="test",
                identifier="x",
                file_path=file_path,
                line_start=line_start,
                line_end=line_end,
                revision=revision,
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
    )


def _range_row(revision="abc123"):
    row = _ev(
        EvidenceType.SOURCE_CODE,
        file_path="src/A.java",
        line_start=10,
        line_end=20,
        revision=revision,
    )
    return row.model_copy(
        update={
            "provenance": row.provenance.model_copy(
                update={
                    "query_fingerprint": row.provenance.query_fingerprint.model_copy(
                        update={"operation": "GET_CODE"}
                    )
                }
            )
        }
    )


class TestUntypedNeverVerifies:
    def test_untyped_claim_is_inconclusive(self):
        hyp = _hyp("something happened somewhere", claim_kind=None)
        result = VerificationEngine().evaluate_hypothesis(
            hyp, [_ev()], [], [], RootCauseVerificationPolicy()
        )
        assert result.status == VerificationStatus.INCONCLUSIVE
        assert "Untyped" in result.explanation or "untyped" in result.explanation

    def test_garbage_kind_is_inconclusive(self):
        hyp = _hyp("something happened somewhere")
        hyp = hyp.model_copy(update={"claim_kind": "NOT_A_KIND"})
        result = VerificationEngine().evaluate_hypothesis(
            hyp, [_ev()], [], [], RootCauseVerificationPolicy()
        )
        assert result.status == VerificationStatus.INCONCLUSIVE


class TestRangeRevisionPin:
    def test_pinned_row_proves(self):
        from investigation_agent_platform.domain.hypothesis.models import ClaimTarget

        ok, _ = VerificationEngine.definition_window_proof(
            [_range_row("abc123")], ClaimTarget(file_path="src/A.java")
        )
        assert ok is True

    def test_unpinned_row_does_not_prove(self):
        from investigation_agent_platform.domain.hypothesis.models import ClaimTarget

        for bad_rev in ("", "HEAD", "UNPINNED"):
            ok, reason = VerificationEngine.definition_window_proof(
                [_range_row(bad_rev)], ClaimTarget(file_path="src/A.java")
            )
            assert ok is False, bad_rev
            assert "pinned" in reason

    def test_cited_revision_must_match(self):
        from investigation_agent_platform.domain.hypothesis.models import ClaimTarget

        ok, reason = VerificationEngine.definition_window_proof(
            [_range_row("abc123")],
            ClaimTarget(file_path="src/A.java", commit_id="def999"),
        )
        assert ok is False
        assert "def999" in reason

    def test_cited_revision_match_proves(self):
        from investigation_agent_platform.domain.hypothesis.models import ClaimTarget

        ok, _ = VerificationEngine.definition_window_proof(
            [_range_row("abc123")],
            ClaimTarget(file_path="src/A.java", commit_id="abc123"),
        )
        assert ok is True


class TestEliminationRecord:
    def _sibling(self, statement, status, refuting):
        hyp = _hyp(statement, status=status)
        return hyp.model_copy(update={"refuting_evidence_ids": list(refuting)})

    def test_rejected_with_evidence_counts(self):
        cand = _hyp("winner")
        sib = self._sibling("loser", HypothesisStatus.REJECTED, ["e1", "e2"])
        bank = [{"evidence_id": "e1"}, {"evidence_id": "e2"}]
        record = VerificationEngine.elimination_record(cand, [cand, sib], bank)
        assert len(record) == 1
        assert record[0].statement == "loser"
        assert record[0].status == "REJECTED"
        assert record[0].ruled_out_by_evidence_ids == ["e1", "e2"]

    def test_rejected_without_evidence_excluded(self):
        cand = _hyp("winner")
        sib = self._sibling("loser", HypothesisStatus.REJECTED, [])
        assert VerificationEngine.elimination_record(cand, [cand, sib], []) == []

    def test_stale_ids_excluded(self):
        # Refuting IDs that resolve against nothing banked satisfy nothing.
        cand = _hyp("winner")
        sib = self._sibling("loser", HypothesisStatus.REJECTED, ["ghost-1"])
        bank = [{"evidence_id": "e1"}]
        assert VerificationEngine.elimination_record(cand, [cand, sib], bank) == []

    def test_no_bank_means_no_record(self):
        cand = _hyp("winner")
        sib = self._sibling("loser", HypothesisStatus.REJECTED, ["e1"])
        assert VerificationEngine.elimination_record(cand, [cand, sib]) == []
        assert VerificationEngine.elimination_record(cand, [cand, sib], None) == []

    def test_live_rivals_excluded(self):
        cand = _hyp("winner")
        sibs = [
            self._sibling("live one", HypothesisStatus.UNDER_INVESTIGATION, ["e1"]),
            self._sibling("supported one", HypothesisStatus.SUPPORTED, ["e1"]),
        ]
        bank = [{"evidence_id": "e1"}]
        assert VerificationEngine.elimination_record(cand, [cand, *sibs], bank) == []

    def test_contradicted_counts(self):
        cand = _hyp("winner")
        sib = self._sibling("wrong", HypothesisStatus.CONTRADICTED, ["e9"])
        bank = [{"evidence_id": "e9"}]
        record = VerificationEngine.elimination_record(cand, [cand, sib], bank)
        assert len(record) == 1 and record[0].status == "CONTRADICTED"

    def test_record_round_trips_through_conclusion(self):
        from investigation_agent_platform.domain.finding.models import InvestigationConclusion

        rec = EliminatedRival(
            statement="loser", status="REJECTED", ruled_out_by_evidence_ids=["e1"]
        )
        concl = InvestigationConclusion(
            tenant_id="t",
            investigation_id=uuid4(),
            status="ROOT_CAUSE_LIKELY",
            root_cause="winner",
            supporting_evidence_ids=[uuid4()],
            eliminated_rivals=[rec],
        )
        assert concl.eliminated_rivals[0].ruled_out_by_evidence_ids == ["e1"]

    def test_rival_unbeaten_kind_known(self):
        from investigation_agent_platform.application.worker.activities.common import (
            _limitation_kind,
        )

        assert _limitation_kind("RIVAL_UNBEATEN: no ruled-out rival") == "RIVAL_UNBEATEN"

    def test_rival_unbeaten_directs_rival_testing(self):
        from investigation_agent_platform.application.investigation.verification import (
            LimitationCode,
            VerificationLimitation,
        )

        directive = VerificationEngine.next_check_for(
            [VerificationLimitation(code=LimitationCode.RIVAL_UNBEATEN, detail="x")],
            [],
            [],
        )
        assert directive is not None
        assert "rival" in directive.lower()

    def test_untested_rival_takes_priority_over_unbeaten(self):
        from investigation_agent_platform.application.investigation.verification import (
            LimitationCode,
            VerificationLimitation,
        )

        directive = VerificationEngine.next_check_for(
            [
                VerificationLimitation(code=LimitationCode.RIVAL_UNTESTED, detail="x"),
                VerificationLimitation(code=LimitationCode.RIVAL_UNBEATEN, detail="y"),
            ],
            ["live rival statement"],
            [],
        )
        assert directive is not None
        assert "live rival statement" in directive
