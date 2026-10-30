# tests/unit/test_rival_overlap.py
"""Phase 2a: rival-shared evidence penalizes confidence deterministically."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from investigation_agent_platform.application.investigation.verification import (
    LimitationCode,
    RootCauseVerificationPolicy,
    VerificationEngine,
    VerificationStatus,
)
from investigation_agent_platform.domain.evidence.models import EvidenceType


def _hyp(supporting=(), status="PROPOSED"):
    return SimpleNamespace(
        id=uuid4(),
        statement="candidate story",
        status=status,
        supporting_evidence_ids=list(supporting),
        assessments=[{"x": 1}],
        # P3 (S6 shape branch): heuristic scoring adjudicates typed
        # claims only; these scoring tests exercise causal machinery.
        claim_kind="CAUSAL",
    )


def _evidence(provider: str, kind: EvidenceType, n: int):
    texts = [
        "null default flows into handler causing NPE",
        "null default flows into handler causing NPE",
        "handler crash restarts worker causing retry storm",
        "handler crash restarts worker causing retry storm",
    ]
    rows = []
    for i in range(n):
        rows.append(
            SimpleNamespace(
                evidence_id=uuid4(),
                provider=provider,
                evidence_type=kind,
                source=f"src://{provider}/{i}",
                provenance=SimpleNamespace(),
                title=texts[i % len(texts)] if provider == "FILE_LOG" else f"row {i}",
                summary="",
                content_snippet=texts[i % len(texts)],
            )
        )
    return rows


def _chain(*ids):
    return [
        SimpleNamespace(
            cause="null default",
            mechanism="flows into handler",
            effect="NPE",
            evidence_ids=list(ids[:2]),
        ),
        SimpleNamespace(
            cause="handler crash", mechanism="restarts", effect="retry storm",
            evidence_ids=list(ids[2:4]),
        ),
    ]


class TestRivalEvidenceOverlap:
    def test_no_siblings_no_share(self) -> None:
        ids = [uuid4(), uuid4()]
        assert VerificationEngine.rival_evidence_overlap(_hyp(ids), None, ids) == (0.0, [])

    def test_fraction_math(self) -> None:
        ids = [uuid4() for _ in range(4)]
        sib = _hyp(ids[:2], status="UNDER_INVESTIGATION")
        fraction, statements = VerificationEngine.rival_evidence_overlap(_hyp(ids), [sib], ids)
        assert fraction == 0.5
        assert statements == ["candidate story"]

    def test_self_excluded_by_id(self) -> None:
        me = _hyp()
        ids = [uuid4(), uuid4()]
        me.supporting_evidence_ids = ids
        assert VerificationEngine.rival_evidence_overlap(me, [me], ids) == (0.0, [])

    def test_rejected_and_contradicted_excluded(self) -> None:
        ids = [uuid4()]
        sibs = [_hyp(ids, status="REJECTED"), _hyp(ids, status="CONTRADICTED")]
        assert VerificationEngine.rival_evidence_overlap(_hyp(ids), sibs, ids) == (0.0, [])

    def test_proposed_rival_citations_are_not_proof(self) -> None:
        # Formulate-time SUPPORTS citations are claims, not evaluated
        # support: a PROPOSED rival sharing rows is priced by the
        # RIVAL_UNTESTED demote, never by the shared-evidence penalty.
        ids = [uuid4(), uuid4()]
        sib = _hyp(ids, status="PROPOSED")
        assert VerificationEngine.rival_evidence_overlap(_hyp(ids), [sib], ids) == (0.0, [])

    def test_garbage_ids_tolerated(self) -> None:
        assert VerificationEngine.rival_evidence_overlap(_hyp(), [{"id": 1}], ["nope"]) == (0.0, [])


class TestRivalPenaltyWiring:
    def _verifiable(self):
        return _evidence("FILE_LOG", EvidenceType.LOG, 4) + _evidence(
            "POSTGRES", EvidenceType.DATABASE_STATE, 4
        )

    def test_full_share_blocks_established(self) -> None:
        engine = VerificationEngine()
        items = self._verifiable()
        ids = [e.evidence_id for e in items]
        sib = _hyp(ids, status="UNDER_INVESTIGATION")
        result = engine.evaluate_hypothesis(
            _hyp(ids), items, [], _chain(*ids), RootCauseVerificationPolicy(), [sib]
        )
        # penalty 1.0 removes the 0.20 contradiction-free term: overall
        # lands exactly on the 0.80 threshold (still VERIFIED) but the
        # conclusion can never be established on fully shared rows.
        assert result.status == VerificationStatus.VERIFIED
        assert result.tier == "likely"
        assert result.confidence.contradiction_penalty == 1.0
        codes = [lim.code for lim in result.limitations]
        assert LimitationCode.RIVAL_SHARED_EVIDENCE in codes

    def test_partial_share_demotes_to_likely(self) -> None:
        engine = VerificationEngine()
        items = self._verifiable()
        ids = [e.evidence_id for e in items]
        sib = _hyp(ids[:2], status="UNDER_INVESTIGATION")
        result = engine.evaluate_hypothesis(
            _hyp(ids), items, [], _chain(*ids), RootCauseVerificationPolicy(), [sib]
        )
        assert result.status == VerificationStatus.VERIFIED
        assert result.tier == "likely"
        codes = [lim.code for lim in result.limitations]
        assert LimitationCode.RIVAL_SHARED_EVIDENCE in codes

    def test_no_share_keeps_established(self) -> None:
        engine = VerificationEngine()
        items = self._verifiable()
        ids = [e.evidence_id for e in items]
        sib = _hyp([uuid4()], status="UNDER_INVESTIGATION")
        result = engine.evaluate_hypothesis(
            _hyp(ids), items, [], _chain(*ids), RootCauseVerificationPolicy(), [sib]
        )
        assert result.status == VerificationStatus.VERIFIED
        assert result.tier == "established"
        codes = [lim.code for lim in result.limitations]
        assert LimitationCode.RIVAL_SHARED_EVIDENCE not in codes
