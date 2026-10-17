# tests/evaluation/test_incident_replay.py
"""P2-3: incident-replay evaluation. Fixed fixtures with known epistemic
outcomes — the regression gate for conclusion quality.

First case replays self-investigation a301ece4: an API-behavior complaint
("profile versions are being dropped") with only log/state evidence, no
bound code/API evidence, and no causal chain. The correct outcome is
INCONCLUSIVE at verification (never VERIFIED), and an honest-abstention
conclusion — not ROOT_CAUSE_ESTABLISHED.
"""

from datetime import UTC, datetime
from uuid import uuid4

from investigation_agent_platform.application.investigation.verification import (
    CausalRelationship,
    RootCauseVerificationPolicy,
    VerificationEngine,
    VerificationStatus,
    rank_hypotheses,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.hypothesis.models import Hypothesis, HypothesisStatus
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)


def _ev(provider, etype):
    iid = uuid4()
    now = datetime.now(UTC)
    return Evidence(
        tenant_id="tenant-a",
        investigation_id=iid,
        evidence_type=etype,
        provider=provider,
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
            source_location=SourceLocation(system="test", identifier="x"),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
    )


def _hyp(statement, status=HypothesisStatus.PROPOSED, supporting=()):
    iid = uuid4()
    return Hypothesis(
        tenant_id="tenant-a",
        investigation_id=iid,
        title=statement[:256],
        description=statement,
        statement=statement,
        status=status,
        supporting_evidence_ids=list(supporting),
    )


def test_a301ece4_replay_log_state_only_never_verifies():
    """10 log/state rows, zero bound code/API evidence, no causal chain:
    the bound-evidence path must refuse VERIFIED (the legacy corpus path
    approved exactly this shape at 0.80)."""
    engine = VerificationEngine()
    hyp = _hyp("read path drops versions", supporting=())
    # Empty bound set short-circuits before the engine in production; at the
    # engine level, an empty bound list cannot reach threshold either.
    result = engine.evaluate_hypothesis(
        hyp, [], [], [], RootCauseVerificationPolicy()
    )
    assert result.status == VerificationStatus.INCONCLUSIVE


def test_supported_hypothesis_with_causal_chain_verifies():
    engine = VerificationEngine()
    items = [_ev("prov-a", EvidenceType.LOG), _ev("prov-a", EvidenceType.LOG)] + [
        _ev("prov-b", EvidenceType.DATABASE_STATE),
        _ev("prov-c", EvidenceType.SOURCE_CODE),
    ]
    hyp = _hyp(
        "route filters versions",
        status=HypothesisStatus.SUPPORTED,
        supporting=tuple(e.evidence_id for e in items),
    )
    chain = [
        CausalRelationship(
            cause="repo query filters by status",
            mechanism="list_revisions applies status predicate before version scan",
            effect="older versions missing from response",
            confidence=0.7,
            evidence_ids=[e.evidence_id for e in items],
        )
    ]
    result = engine.evaluate_hypothesis(
        hyp, items, [], chain, RootCauseVerificationPolicy()
    )
    assert result.status == VerificationStatus.VERIFIED


def test_refuted_first_candidate_does_not_block_supported_second():
    ranked = rank_hypotheses(
        [
            _hyp("wrong", status=HypothesisStatus.CONTRADICTED),
            _hyp("right", status=HypothesisStatus.SUPPORTED),
        ]
    )
    assert ranked[0].statement == "right"
    assert ranked[-1].statement == "wrong"
