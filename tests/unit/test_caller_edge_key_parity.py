# tests/unit/test_caller_edge_key_parity.py
"""Caller-edge obligation identity + proven-matching goldens.

Pins the chief-architect hardening: single canonical key with pipe-escaping
and identical path normalization on verify/workflow sides, and proven-callers
matching class stem as well as method fragment.
"""

from datetime import UTC, datetime
from uuid import uuid4

from investigation_agent_platform.application.investigation.verification import (
    VerificationEngine,
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


def _code_row(path, snippet, revision="abc123"):
    iid = uuid4()
    now = datetime.now(UTC)
    location = SourceLocation(
        system="git",
        identifier="x",
        file_path=path,
        line_start=10,
        line_end=20,
        revision=revision,
    )
    return Evidence(
        tenant_id="t",
        investigation_id=iid,
        evidence_type=EvidenceType.SOURCE_CODE,
        provider="test",
        source="test://src",
        title="t",
        summary="s",
        content_snippet=snippet,
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
                provider_type="test", operation="GET_SOURCE", normalized_query_hash="h"
            ),
            source_location=location,
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=f"fp-{uuid4()}",
    )


def _hyp(statement):
    return Hypothesis(
        tenant_id="t",
        investigation_id=uuid4(),
        title=statement[:256],
        description=statement,
        statement=statement,
        status=HypothesisStatus.PROPOSED,
    )


def test_canonical_key_escapes_pipes_and_normalizes_paths():
    key_plain = VerificationEngine.canonical_caller_edge_key(
        "OrderServiceHandler.handle", "OrderQualifyHelper", "src/a.py", "abc123"
    )
    key_slashed = VerificationEngine.canonical_caller_edge_key(
        "OrderServiceHandler.handle", "OrderQualifyHelper", "./src/a.py", "abc123"
    )
    key_backslash = VerificationEngine.canonical_caller_edge_key(
        "OrderServiceHandler.handle", "OrderQualifyHelper", "src\\a.py", "abc123"
    )
    assert key_plain == key_slashed == key_backslash

    key_pipe = VerificationEngine.canonical_caller_edge_key(
        "A|B", "C", "src/a.py", "abc"
    )
    assert "|" in key_pipe  # separators remain
    assert "A|B" not in key_pipe  # component pipe is escaped
    assert "A%7CB" in key_pipe

    target = ClaimTarget(
        symbol="OrderServiceHandler.handle",
        counterpart="OrderQualifyHelper",
        file_path="./src/a.py",
        commit_id="abc123",
    )
    assert VerificationEngine.caller_edge_key(target) == key_plain


def test_workflow_key_matches_canonical_helper():
    # Mirrors the workflow forcing normalization (workflows.py caller-edge
    # branch) without importing the Temporal workflow module.
    def _workflow_key(symbol, counterpart, file_path, commit_id):
        def _escape(value):
            return str(value or "").strip().replace("|", "%7C")

        normalized = (
            str(file_path or "").replace("\\", "/").strip().strip("/").lstrip("./")
        )
        return "|".join(
            (
                _escape(symbol),
                _escape(counterpart),
                normalized.replace("|", "%7C"),
                _escape(commit_id),
            )
        )

    cases = [
        ("OrderServiceHandler.handle", "OrderQualifyHelper", "src/X.java", "abc123"),
        ("OrderServiceHandler.handle", "OrderServiceRestProxy", "./src/X.java", ""),
        ("S", "C|D", "src\\Y.java", "rev1"),
    ]
    for symbol, counterpart, path, commit in cases:
        assert _workflow_key(symbol, counterpart, path, commit) == (
            VerificationEngine.canonical_caller_edge_key(
                symbol, counterpart, path, commit
            )
        )


def test_method_qualified_counterpart_proves_class_stem():
    iid = uuid4()
    _ = iid
    helper = _code_row(
        "src/OrderQualifyHelper.java",
        "10| public String qualify() {\n11| return handler.handle(orderId);",
    )
    proxy = _code_row(
        "src/OrderServiceRestProxy.java",
        "10| public String submit() {\n11| return handler.handle(orderId);",
    )
    statement = (
        "OrderQualifyHelper and OrderServiceRestProxy call OrderServiceHandler.handle"
    )
    hyp = _hyp(statement)
    assessments = [
        HypothesisEvidenceAssessment(
            hypothesis_id=hyp.id,
            evidence_id=helper.evidence_id,
            assessment=AssessmentType.SUPPORTS,
            strength=AssessmentStrength.STRONG,
            reason="helper call",
            claim_kind=ClaimKind.CALL_EDGE,
            claim_target=ClaimTarget(
                symbol="OrderServiceHandler.handle",
                counterpart="OrderQualifyHelper",
                file_path="src/OrderQualifyHelper.java",
                commit_id="abc123",
            ),
        ),
        HypothesisEvidenceAssessment(
            hypothesis_id=hyp.id,
            evidence_id=proxy.evidence_id,
            assessment=AssessmentType.SUPPORTS,
            strength=AssessmentStrength.STRONG,
            reason="proxy call via method-qualified counterpart",
            claim_kind=ClaimKind.CALL_EDGE,
            claim_target=ClaimTarget(
                symbol="OrderServiceHandler.handle",
                # Method-qualified form must still prove the class stem.
                counterpart="OrderServiceRestProxy.submit",
                file_path="src/OrderServiceRestProxy.java",
                commit_id="abc123",
            ),
        ),
    ]
    hyp = hyp.model_copy(
        update={
            "assessments": assessments,
            "supporting_evidence_ids": [helper.evidence_id, proxy.evidence_id],
        }
    )
    ok, reason = VerificationEngine().check_claim_kind(hyp, [helper, proxy])
    assert ok is True, reason
