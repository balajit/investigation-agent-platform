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
        freshness=EvidenceFreshness(observed_at=datetime.now(UTC), retrieved_at=datetime.now(UTC)),
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
    ctx = compile_reasoning_context(
        _state([_evidence(snippet=body, etype=EvidenceType.SOURCE_CODE)])
    )
    assert ctx["prompt_version"] == REASONER_PROMPT_VERSION
    excerpt = ctx["evidence_excerpts"][0]
    assert body in excerpt["content"]
    assert excerpt["content_truncated"] is False


def test_long_code_body_truncated_with_disclosure():
    body = "x = 1\n" * 680  # 4080 chars: fits Evidence cap (4096), exceeds packet cap (4000)
    ctx = compile_reasoning_context(
        _state([_evidence(snippet=body, etype=EvidenceType.SOURCE_CODE)])
    )
    excerpt = ctx["evidence_excerpts"][0]
    assert excerpt["content_truncated"] is True
    assert len(excerpt["content"]) <= 4000
    assert excerpt["next_window"] is not None
    assert excerpt["next_window"]["start_line"] > 1


def test_long_summary_flagged_truncated():
    ctx = compile_reasoning_context(_state([_evidence(summary="s" * 2000)]))
    excerpt = ctx["evidence_excerpts"][0]
    assert excerpt["summary_truncated"] is True
    assert len(excerpt["summary"]) == 1000


def test_non_code_snippet_not_duplicated_into_content():
    ctx = compile_reasoning_context(_state([_evidence(snippet="should not leak")]))
    assert ctx["evidence_excerpts"][0]["content"] == ""


def _story_evidence(**overrides):
    attrs = {
        "story_comp": "ACTVTY",
        "story_func": "execute_action_activity",
        "story_stage": "execute",
        "story_step": "dispatch",
        "story_outcome": "completed",
        "story_event": "execute_action_activity completed",
        "story_elapsed_ms": 42,
        "story_inputs": '{"action": "SEARCH_CODE"}',
        "story_outputs": '{"banked": 0}',
    }
    attrs.update(overrides)
    base = _evidence(summary="[ACTVTY] execute_action_activity · execute/dispatch · completed")
    return base.model_copy(update={"attributes": attrs})


def test_story_block_reaches_log_excerpt():
    ctx = compile_reasoning_context(_state([_story_evidence()]))
    excerpt = ctx["evidence_excerpts"][0]
    block = excerpt["story"]
    assert block["comp"] == "ACTVTY"
    assert block["func"] == "execute_action_activity"
    assert block["stage"] == "execute"
    assert block["outcome"] == "completed"
    assert block["elapsed_ms"] == 42
    assert block["inputs"] == '{"action": "SEARCH_CODE"}'
    assert block["outputs"] == '{"banked": 0}'


def test_story_block_absent_without_envelope():
    ctx = compile_reasoning_context(_state([_evidence()]))
    assert ctx["evidence_excerpts"][0]["story"] is None


def test_story_block_inputs_capped():
    ctx = compile_reasoning_context(_state([_story_evidence(story_inputs="x" * 2000)]))
    assert len(ctx["evidence_excerpts"][0]["story"]["inputs"]) == 500


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
    newer = [
        _tier_evidence(snippet=f"def g{i}(): x", relevance=0.1, age_seconds=i) for i in range(6)
    ]
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

    items = [
        _tier_evidence(snippet=f"def g{i}(): x", relevance=0.1, age_seconds=(5 - i) * 10)
        for i in range(6)
    ]
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
        fillers = [
            _tier_evidence(snippet=f"def h{i}(): y", relevance=0.99, age_seconds=0)
            for i in range(6)
        ]
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


def _code_evidence(summary="code", snippet="x = 1", retrieved=None):

    ev = _evidence(summary=summary, snippet=snippet, etype=EvidenceType.SOURCE_CODE)
    if retrieved is not None:
        fresh = ev.freshness.model_copy(update={"retrieved_at": retrieved})
        ev = ev.model_copy(update={"retrieved_at": retrieved, "freshness": fresh})
    return ev


class TestContextShrink:
    def test_estimate_tokens(self):
        from investigation_agent_platform.application.investigation.context_compiler import (
            estimate_tokens,
        )

        assert estimate_tokens("") == 0
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("abcde") == 2

    def test_newest_survives_cap(self):
        from datetime import timedelta

        from investigation_agent_platform.application.investigation.context_compiler import (
            compile_evidence_excerpts,
        )

        now = datetime.now(UTC)
        items = [
            _code_evidence(summary=f"row-{i}", retrieved=now - timedelta(minutes=i))
            for i in range(120)
        ]
        excerpts = compile_evidence_excerpts(items)
        assert len(excerpts) <= 100
        summaries = [e["summary"] for e in excerpts]
        assert "row-0" in summaries  # newest kept
        assert "row-119" not in summaries  # oldest shed

    def test_bound_never_demoted(self):
        from investigation_agent_platform.application.investigation.context_compiler import (
            enforce_context_budget,
        )

        old = {
            "evidence_id": "bound-1",
            "tier": 1,
            "content": "x" * 5000,
            "summary": "s",
            "retrieved_at": "2020-01-01",
        }
        new = {
            "evidence_id": "new-1",
            "tier": 1,
            "content": "y" * 100,
            "summary": "s",
            "retrieved_at": "2026-01-01",
        }
        ctx = {
            "plan": {},
            "hypotheses": [],
            "timeline": [],
            "pending_questions": [],
            "findings": [],
            "evidence_excerpts": [old, new],
        }
        out = enforce_context_budget(ctx, bound={"bound-1"}, budget=200)
        by_id = {e["evidence_id"]: e for e in out["evidence_excerpts"]}
        assert by_id["bound-1"]["tier"] == 1
        assert by_id["bound-1"]["content"] == "x" * 5000
        # Unbound row demoted to the retrieval marker; bound content intact.
        assert by_id["new-1"]["tier"] == 2
        assert out["purged"]["excerpts_demoted"] == 1

    def test_budget_drops_oldest_first(self):
        from investigation_agent_platform.application.investigation.context_compiler import (
            enforce_context_budget,
        )

        def _row(eid, year, size=2000):
            return {
                "evidence_id": eid,
                "tier": 1,
                "content": "z" * size,
                "summary": "s",
                "retrieved_at": f"{year}-06-01",
            }

        ctx = {
            "plan": {},
            "hypotheses": [],
            "timeline": [],
            "pending_questions": [],
            "findings": [],
            "evidence_excerpts": [_row("old", "2020"), _row("mid", "2023"), _row("new", "2026")],
        }
        out = enforce_context_budget(ctx, budget=1100)
        by_id = {e["evidence_id"]: e for e in out["evidence_excerpts"]}
        assert by_id["old"]["tier"] == 2  # oldest demoted first
        assert out["purged"]["excerpts_demoted"] >= 1

    def test_terminal_hypotheses_compact(self):
        from investigation_agent_platform.application.investigation.context_compiler import (
            compile_hypotheses,
        )
        from investigation_agent_platform.domain.hypothesis.models import (
            Hypothesis,
            HypothesisStatus,
        )

        def _hyp(statement, status):
            iid = uuid4()
            return Hypothesis(
                tenant_id="t",
                investigation_id=iid,
                title=statement[:50],
                description=statement,
                statement=statement,
                status=status,
            )

        hyps = [
            _hyp("rejected claim with a long tail " + "x" * 500, HypothesisStatus.REJECTED),
            _hyp("live claim", HypothesisStatus.UNDER_INVESTIGATION),
        ]
        out = compile_hypotheses(hyps)
        assert out[0].get("terminal") is True
        assert len(out[0]["statement"]) <= 200
        assert "supporting_evidence_ids" not in out[0]
        assert out[1].get("terminal", False) is False

    def test_compiled_context_carries_purge_ledger(self):
        ctx = compile_reasoning_context(_state([_evidence()]))
        assert "purged" in ctx
        assert "estimated_tokens" in ctx
        assert ctx["estimated_tokens"] > 0

    def test_enforce_deterministic(self):
        from investigation_agent_platform.application.investigation.context_compiler import (
            enforce_context_budget,
        )

        def _ctx():
            return {
                "plan": {"a": 1},
                "hypotheses": [],
                "timeline": [{"event": f"e{i}"} for i in range(10)],
                "pending_questions": [f"q{i}" for i in range(10)],
                "findings": [],
                "evidence_excerpts": [],
            }

        first = enforce_context_budget(_ctx(), budget=300)
        second = enforce_context_budget(_ctx(), budget=300)
        assert first["purged"] == second["purged"]
        assert first["estimated_tokens"] == second["estimated_tokens"]


def test_case_state_pinned_block_from_persisted_fields():
    from investigation_agent_platform.application.investigation.context_compiler import (
        enforce_context_budget,
    )

    rows = [
        _evidence(summary="a", etype=EvidenceType.SOURCE_CODE),
        _evidence(summary="b", etype=EvidenceType.SOURCE_CODE),
        _evidence(summary="c", etype=EvidenceType.LOG),
    ]
    ctx = compile_reasoning_context(_state(rows))
    block = ctx["case_state"]
    assert block["banked_total"] == 3
    assert block["banked_by_type"]["SOURCE_CODE"] == 2
    assert block["banked_by_type"]["LOG"] == 1
    assert block["lifecycle_state"] != ""
    # Pinned: survives even a tiny budget that sheds everything else.
    squeezed = enforce_context_budget(dict(ctx), budget=50)
    assert squeezed["case_state"] == block
