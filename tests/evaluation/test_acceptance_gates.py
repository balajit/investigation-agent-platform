"""P2-3 acceptance gates: release thresholds as fail-closed tests.

- Zero unsupported VERIFIED on negative fixtures (insufficient evidence,
  contradicted, conceding, definition-without-range-read).
- Citation validity 100%: every VERIFIED result cites only resolvable IDs.
- Band integrity: VERIFIED carries established/likely tier + reason fields.
"""

from datetime import UTC, datetime
from uuid import uuid4

from investigation_agent_platform.application.investigation.verification import (
    CausalRelationship,
    Contradiction,
    ContradictionSeverity,
    RootCauseVerificationPolicy,
    VerificationEngine,
    VerificationStatus,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.hypothesis.models import Hypothesis, HypothesisStatus
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)


def _ev(provider, etype, operation="SEARCH_CODE", path="src/a.py"):
    iid = uuid4()
    now = datetime.now(UTC)
    return Evidence(
        tenant_id="t",
        investigation_id=iid,
        evidence_type=etype,
        provider=provider,
        source="test://src",
        title="t",
        summary="s",
        content_snippet="snippet",
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
                provider_type="test", operation=operation, normalized_query_hash="h"
            ),
            source_location=SourceLocation(
                system="git", identifier="x", file_path=path,
                line_start=1, line_end=5, revision="abc123",
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=f"fp-{uuid4()}",
    )


def _hyp(statement, supporting=()):
    return Hypothesis(
        tenant_id="t",
        investigation_id=uuid4(),
        title="t",
        description="d",
        statement=statement,
        status=HypothesisStatus.UNDER_INVESTIGATION,
        supporting_evidence_ids=list(supporting),
    )


def _chain(ids):
    return [
        CausalRelationship(
            cause="c", mechanism="m", effect="e", confidence=0.7,
            evidence_ids=list(ids),
        )
    ]


def test_gate_zero_unsupported_verified_on_negatives():
    engine = VerificationEngine()
    policy = RootCauseVerificationPolicy()
    items = [_ev("a", EvidenceType.LOG), _ev("b", EvidenceType.DATABASE_STATE)]
    # Insufficient: no bound evidence.
    assert engine.evaluate_hypothesis(
        _hyp("x causes y"), [], [], [], policy
    ).status == VerificationStatus.INCONCLUSIVE
    # Concession.
    assert engine.evaluate_hypothesis(
        _hyp("could not determine x"), items, [], _chain([i.evidence_id for i in items]), policy
    ).status == VerificationStatus.INCONCLUSIVE
    # Definition without range read.
    assert engine.evaluate_hypothesis(
        _hyp("widget is defined in src/a.py"), items, [], _chain([i.evidence_id for i in items]), policy
    ).status == VerificationStatus.INCONCLUSIVE
    # Contradicted.
    contradiction = Contradiction(
        tenant_id="t",
        investigation_id=uuid4(),
        statement="nope",
        severity=ContradictionSeverity.HIGH,
        resolution_status="UNRESOLVED",
    )
    assert engine.evaluate_hypothesis(
        _hyp("x causes y"), items, [contradiction], _chain([i.evidence_id for i in items]),
        policy,
    ) is not None
    result = engine.evaluate_hypothesis(
        _hyp("x causes y"), items, [contradiction], _chain([i.evidence_id for i in items]),
        policy,
    )
    # Contradiction tested separately below; this path must not VERIFY either.
    assert result.status in (VerificationStatus.INCONCLUSIVE, VerificationStatus.CONTRADICTED)
    _ = contradiction


def test_gate_citation_validity_and_band():
    engine = VerificationEngine()
    policy = RootCauseVerificationPolicy()
    items = [
        _ev("a", EvidenceType.SOURCE_CODE, operation="GET_SOURCE"),
        _ev("b", EvidenceType.SOURCE_CODE, operation="GET_SOURCE"),
        _ev("c", EvidenceType.CALL_GRAPH, operation="SEARCH_CODE"),
        _ev("d", EvidenceType.LOG, operation="SEARCH_LOGS"),
    ]
    ids = [e.evidence_id for e in items]
    hyp = _hyp("widget is implemented in src/a.py", supporting=tuple(ids))
    result = engine.evaluate_hypothesis(hyp, items, [], _chain(ids), policy)
    assert result.status == VerificationStatus.VERIFIED
    known = set(ids)
    assert all(eid in known for eid in result.corroborating_evidence_ids)
    assert result.tier in ("established", "likely")
    if result.tier == "likely":
        assert result.best_reason != ""
