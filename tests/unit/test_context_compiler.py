# tests/unit/test_context_compiler.py
"""P0-1: compiled reasoning context carries code bodies and hydrated state."""

from datetime import UTC, datetime
from uuid import uuid4

from investigation_agent_platform.application.investigation.context_compiler import (
    REASONER_PROMPT_VERSION,
    compile_reasoning_context,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.investigation.models import InvestigationState
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)


def _evidence(summary="log line", snippet="", etype=EvidenceType.LOG):
    iid = uuid4()
    return Evidence(
        tenant_id="tenant-a",
        investigation_id=iid,
        evidence_type=etype,
        provider="test",
        source="test://src",
        title="t",
        summary=summary,
        content_snippet=snippet,
        observed_at=datetime.now(UTC),
        retrieved_at=datetime.now(UTC),
        provenance=EvidenceProvenance(
            tenant_id="tenant-a",
            investigation_id=iid,
            provider_type="test",
            requested_provider_id="test",
            actual_provider_id="test",
            source_system="test",
            retrieval_timestamp=datetime.now(UTC),
            query_fingerprint=QueryFingerprint(
                provider_type="test",
                operation="test",
                normalized_query_hash="abc",
            ),
            source_location=SourceLocation(system="test", identifier="x"),
        ),
        freshness=EvidenceFreshness(
            observed_at=datetime.now(UTC), retrieved_at=datetime.now(UTC)
        ),
        fingerprint=f"fp-{uuid4()}",
    )


def _state(evidence):
    from datetime import timedelta

    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationContext,
        InvestigationRequest,
    )

    now = datetime.now(UTC)
    inv = Investigation(
        session_id="sess-1",
        application_id="app",
        tenant_id="tenant-a",
        request=InvestigationRequest(
            application_id="app",
            problem_description="p",
            session_id="sess-1",
            requested_by="tester",
        ),
        context=InvestigationContext(
            environment="production",
            time_window=(now - timedelta(hours=1), now),
        ),
    )
    return InvestigationState(investigation=inv, evidence=evidence)


def test_source_code_body_reaches_compiled_context():
    body = "def handler():\n    return buggy_value"
    ctx = compile_reasoning_context(_state([_evidence(snippet=body, etype=EvidenceType.SOURCE_CODE)]))
    assert ctx["prompt_version"] == REASONER_PROMPT_VERSION
    excerpt = ctx["evidence_excerpts"][0]
    assert body in excerpt["content"]
    assert excerpt["content_truncated"] is False


def test_long_code_body_truncated_with_disclosure():
    body = "x = 1\n" * 600  # 3600 chars: fits Evidence cap, exceeds excerpt cap
    ctx = compile_reasoning_context(_state([_evidence(snippet=body, etype=EvidenceType.SOURCE_CODE)]))
    excerpt = ctx["evidence_excerpts"][0]
    assert excerpt["content_truncated"] is True
    assert len(excerpt["content"]) <= 1500


def test_long_summary_flagged_truncated():
    ctx = compile_reasoning_context(_state([_evidence(summary="s" * 2000)]))
    excerpt = ctx["evidence_excerpts"][0]
    assert excerpt["summary_truncated"] is True
    assert len(excerpt["summary"]) == 1000


def test_non_code_snippet_not_duplicated_into_content():
    ctx = compile_reasoning_context(_state([_evidence(snippet="should not leak")]))
    assert ctx["evidence_excerpts"][0]["content"] == ""


def _tier_evidence(snippet="def f(): ...", relevance=1.0, age_seconds=0):
    from datetime import timedelta

    base = _evidence(snippet=snippet, etype=EvidenceType.SOURCE_CODE)
    when = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return base.model_copy(
        update={"relevance": relevance, "retrieved_at": when, "observed_at": when}
    )


def _hypothesis_supporting(*evidence_ids):
    from uuid import uuid4

    from investigation_agent_platform.domain.hypothesis.models import Hypothesis

    return Hypothesis(
        tenant_id="tenant-a",
        investigation_id=uuid4(),
        title="t",
        description="d",
        statement="h",
        supporting_evidence_ids=list(evidence_ids),
    )


def test_bound_evidence_always_tier1():
    from investigation_agent_platform.application.investigation.context_compiler import (
        compile_evidence_excerpts,
    )

    low = _tier_evidence(snippet="def f(): bound", relevance=0.1, age_seconds=3600)
    excerpts = compile_evidence_excerpts([low], {str(low.evidence_id)})
    assert excerpts[0]["tier"] == 1
    assert excerpts[0]["content"] == "def f(): bound"


def test_high_relevance_tier1_without_binding():
    from investigation_agent_platform.application.investigation.context_compiler import (
        compile_evidence_excerpts,
    )

    ev = _tier_evidence(snippet="def f(): hot", relevance=0.9, age_seconds=3600)
    excerpts = compile_evidence_excerpts([ev])
    assert excerpts[0]["tier"] == 1
    assert excerpts[0]["content"] == "def f(): hot"


def test_low_relevance_old_unbound_is_tier2_summary_only():
    from investigation_agent_platform.application.investigation.context_compiler import (
        _TIER2_MARKER,
        compile_evidence_excerpts,
    )

    # Push past the recency window with newer items so this one is old on all axes.
    old = _tier_evidence(snippet="def f(): stale", relevance=0.1, age_seconds=7200)
    newer = [_tier_evidence(snippet=f"def g{i}(): x", relevance=0.1, age_seconds=i) for i in range(6)]
    excerpts = compile_evidence_excerpts([old, *newer])
    by_id = {e["evidence_id"]: e for e in excerpts}
    stale = by_id[str(old.evidence_id)]
    assert stale["tier"] == 2
    assert stale["content"] == _TIER2_MARKER
    assert stale["summary"] != ""  # summary + provenance retained


def test_recency_grants_tier1():
    from investigation_agent_platform.application.investigation.context_compiler import (
        compile_evidence_excerpts,
    )

    items = [_tier_evidence(snippet=f"def g{i}(): x", relevance=0.1, age_seconds=(5 - i) * 10) for i in range(6)]
    excerpts = compile_evidence_excerpts(items)
    tiers = [e["tier"] for e in excerpts]
    assert tiers.count(1) == 5  # 5 most recent (default window)
    assert tiers.count(2) == 1  # oldest falls out


def test_relevance_floor_env_override(monkeypatch):
    import os

    from investigation_agent_platform.application.investigation.context_compiler import (
        compile_evidence_excerpts,
    )

    os.environ["IAP_COMPILER_FULL_EXCERPT_RELEVANCE"] = "0.99"
    try:
        ev = _tier_evidence(snippet="def f(): warm", relevance=0.9, age_seconds=3600)
        fillers = [_tier_evidence(snippet=f"def h{i}(): y", relevance=0.99, age_seconds=0) for i in range(6)]
        excerpts = compile_evidence_excerpts([ev, *fillers])
        by_id = {e["evidence_id"]: e for e in excerpts}
        assert by_id[str(ev.evidence_id)]["tier"] == 2
    finally:
        del os.environ["IAP_COMPILER_FULL_EXCERPT_RELEVANCE"]


def test_compiled_context_wires_hypothesis_bound_set():
    low = _tier_evidence(snippet="def f(): cited", relevance=0.05, age_seconds=3600)
    state = _state([low])
    state = state.model_copy(
        update={"active_hypotheses": [_hypothesis_supporting(low.evidence_id)]}
    )
    ctx = compile_reasoning_context(state)
    assert ctx["evidence_excerpts"][0]["tier"] == 1
    assert ctx["evidence_excerpts"][0]["content"] == "def f(): cited"
