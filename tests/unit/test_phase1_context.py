# tests/unit/test_phase1_context.py
"""Phase 1: code packets, deterministic ordering, visible-ID envelope, prompt sections."""

import asyncio
import random
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from investigation_agent_platform.application.investigation.context_compiler import (
    CODE_PACKET_VERSION,
    compile_evidence_excerpts,
    compile_reasoning_context,
    enforce_context_budget,
)
from investigation_agent_platform.application.investigation.reasoning import (
    ReasoningCoordinator,
    build_system_prompt,
)
from investigation_agent_platform.application.worker.activities import (
    select_reasoning_evidence,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.investigation.models import (
    Investigation,
    InvestigationContext,
    InvestigationRequest,
    InvestigationState,
)
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)
from investigation_agent_platform.ports.observability.telemetry import LLMCallMetadata
from investigation_agent_platform.ports.reasoning.llm_gateway import LLMGatewayResponse


def _ev(
    *,
    etype=EvidenceType.LOG,
    snippet="",
    summary="s",
    retrieved=None,
    file_path=None,
    line_start=None,
    line_end=None,
    attributes=None,
):
    iid = uuid4()
    when = retrieved or datetime.now(UTC)
    return Evidence(
        tenant_id="tenant-a",
        investigation_id=iid,
        evidence_type=etype,
        provider="test",
        source="test://src",
        title="t",
        summary=summary,
        content_snippet=snippet,
        attributes=attributes or {},
        observed_at=when,
        retrieved_at=when,
        provenance=EvidenceProvenance(
            tenant_id="tenant-a",
            investigation_id=iid,
            provider_type="test",
            requested_provider_id="test",
            actual_provider_id="test",
            source_system="test",
            retrieval_timestamp=when,
            query_fingerprint=QueryFingerprint(
                provider_type="test", operation="test", normalized_query_hash="abc"
            ),
            source_location=SourceLocation(
                system="Git",
                identifier="r/f",
                file_path=file_path,
                line_start=line_start,
                line_end=line_end,
                revision="deadbeef" if file_path else None,
            ),
        ),
        freshness=EvidenceFreshness(observed_at=when, retrieved_at=when),
        fingerprint=f"fp-{uuid4()}",
    )


def _range_read(start, end, marker_line=None):
    lines = []
    for n in range(start, end + 1):
        text = "if decisive_branch:" if n == marker_line else f"stmt_{n}"
        lines.append(f"{n}| {text}")
    return _ev(
        etype=EvidenceType.SOURCE_CODE,
        snippet="\n".join(lines),
        file_path="src/svc/handler.py",
        line_start=start,
        line_end=end,
        attributes={"read_mode": "range"},
    )


def _head_read():
    return _ev(
        etype=EvidenceType.SOURCE_CODE,
        snippet="\n".join(f"head_{n}" for n in range(1, 120)),
        file_path="src/svc/handler.py",
        line_start=1,
        line_end=119,
        attributes={"read_mode": "path_only", "discovery_only": True},
    )


def _state(evidence, problem="p"):
    now = datetime.now(UTC)
    inv = Investigation(
        session_id="sess-1",
        application_id="app",
        tenant_id="tenant-a",
        request=InvestigationRequest(
            application_id="app",
            problem_description=problem,
            session_id="sess-1",
            requested_by="tester",
        ),
        context=InvestigationContext(
            environment="production", time_window=(now - timedelta(hours=1), now)
        ),
    )
    return InvestigationState(investigation=inv, evidence=evidence)


class TestCodePackets:
    def test_decisive_branch_at_line_480_is_in_compiled_context(self):
        rng = _range_read(470, 490, marker_line=480)
        head = _head_read()
        ctx = compile_reasoning_context(_state([head, rng]))
        by_id = {e["evidence_id"]: e for e in ctx["evidence_excerpts"]}
        packet = by_id[str(rng.evidence_id)]
        assert "decisive_branch" in packet["content"]
        assert packet["window"]["start_line"] == 470
        assert packet["window"]["end_line"] == 490
        assert packet["window"]["why"] == "range_read"
        assert packet["read_mode"] == "range"
        assert packet["discovery_only"] is False
        assert packet["citable_evidence_id"] == str(rng.evidence_id)
        assert packet["target"]["file_path"] == "src/svc/handler.py"
        assert packet["packet_version"] == CODE_PACKET_VERSION

    def test_path_only_head_is_labeled_discovery_and_lacks_branch(self):
        head = _head_read()
        ctx = compile_reasoning_context(_state([head]))
        packet = ctx["evidence_excerpts"][0]
        assert packet["discovery_only"] is True
        assert packet["window"]["why"] == "discovery"
        assert "decisive_branch" not in packet["content"]

    def test_code_fact_resolution_is_exposed_with_its_source_packet(self):
        ev = _range_read(10, 12)
        location = ev.provenance.source_location.model_copy(
            update={
                "file_path": "src/TransactionProcessor.java",
                "identifier": "repo/src/TransactionProcessor.java",
            }
        )
        ev = ev.model_copy(
            update={"provenance": ev.provenance.model_copy(update={"source_location": location})}
        )
        resolution = {
            "status": "RESOLVED",
            "repository": "repo",
            "revision": "abc123",
            "declaring_type": "TransactionProcessor",
            "field_name": "retryCount",
            "field_type": "int",
            "field_category": "INSTANCE_FIELD",
            "value_semantics": "EXPLICIT_INITIALIZER",
            "raw_initializer": "3",
            "resolved_value": "3",
            "source_evidence_ids": [str(ev.evidence_id)],
            "declaration_location": {
                "system": "Git",
                "identifier": "repo/src/TransactionProcessor.java",
                "file_path": "src/TransactionProcessor.java",
                "line_start": 10,
                "line_end": 12,
                "revision": "abc123",
            },
            "limitation_codes": [],
        }
        ev = ev.model_copy(
            update={"attributes": {"read_mode": "range", "code_fact_resolution": resolution}}
        )
        packet = compile_reasoning_context(_state([ev]))["evidence_excerpts"][0]
        assert packet["code_fact_resolution"]["resolved_value"] == "3"
        assert packet["code_fact_resolution"]["source_evidence_ids"] == [str(ev.evidence_id)]

    def test_truncated_packet_discloses_next_window(self):
        lines = [f"{n}| {'y' * 20}" for n in range(100, 260)]
        ev = _ev(
            etype=EvidenceType.SOURCE_CODE,
            snippet="\n".join(lines)[:4090],
            file_path="a.py",
            line_start=100,
            line_end=259,
            attributes={"read_mode": "range"},
        )
        ctx = compile_reasoning_context(_state([ev]))
        packet = ctx["evidence_excerpts"][0]
        assert packet["content_truncated"] is True
        nxt = packet["next_window"]
        assert nxt is not None and nxt["start_line"] > 100
        assert nxt["start_line"] == int(packet["content"].splitlines()[-1].split("|")[0]) + 1

    def test_bound_code_survives_flood_of_newer_logs(self):
        old = datetime.now(UTC) - timedelta(days=1)
        bound = _range_read(10, 20, marker_line=15)
        object.__setattr__(bound, "retrieved_at", old)
        logs = [_ev(summary=f"log-{i}", snippet=f"log {i}") for i in range(150)]
        excerpts = compile_evidence_excerpts([*logs, bound], {str(bound.evidence_id)})
        by_id = {e["evidence_id"]: e for e in excerpts}
        assert "decisive_branch" in by_id[str(bound.evidence_id)]["content"]
        assert by_id[str(bound.evidence_id)]["claim_priority"] == 0

    def test_unbound_code_not_crowded_out_by_newer_logs(self):
        old = datetime.now(UTC) - timedelta(days=1)
        code = _range_read(10, 20, marker_line=15)
        object.__setattr__(code, "retrieved_at", old)
        logs = [_ev(summary=f"log-{i}", snippet=f"log {i}") for i in range(150)]
        excerpts = compile_evidence_excerpts([*logs, code])
        assert str(code.evidence_id) in {e["evidence_id"] for e in excerpts}

    def test_ordering_is_deterministic_under_shuffle(self):
        items = [_ev(summary=f"r{i}", snippet=f"x{i}") for i in range(30)]
        items += [_range_read(1, 5), _head_read()]
        first = [e["evidence_id"] for e in compile_evidence_excerpts(items)]
        shuffled = list(items)
        random.Random(7).shuffle(shuffled)
        second = [e["evidence_id"] for e in compile_evidence_excerpts(shuffled)]
        assert first == second

    def test_budget_shrinks_unbound_packet_and_discloses(self):
        big = _range_read(1, 300)
        packet = compile_evidence_excerpts([big])[0]
        assert len(packet["content"]) > 1500
        ctx = {
            "plan": {},
            "hypotheses": [],
            "timeline": [],
            "pending_questions": [],
            "findings": [],
            "evidence_excerpts": [packet],
        }
        out = enforce_context_budget(ctx, budget=600)
        shrunk = out["evidence_excerpts"][0]
        assert out["purged"]["code_packets_shrunk"] == 1
        assert shrunk["content_truncated"] is True
        assert shrunk["next_window"] is not None

    def test_budget_never_shrinks_bound_packet(self):
        big = _range_read(1, 300)
        packet = compile_evidence_excerpts([big], {str(big.evidence_id)})[0]
        original = packet["content"]
        ctx = {
            "plan": {},
            "hypotheses": [],
            "timeline": [],
            "pending_questions": [],
            "findings": [],
            "evidence_excerpts": [packet],
        }
        out = enforce_context_budget(ctx, bound={str(big.evidence_id)}, budget=100)
        assert out["evidence_excerpts"][0]["content"] == original


def _capture(problem="p", evidence=None):
    safety = MagicMock()
    safety.validate_prompt_safety = AsyncMock(return_value=True)
    gateway = MagicMock()
    gateway.complete = AsyncMock(
        return_value=LLMGatewayResponse(
            content="{}",
            parsed={"observations": [], "conclusion_readiness": 0.0},
            metadata=LLMCallMetadata(
                model_name="t",
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
                estimated_cost_usd=0.0,
                latency_ms=1.0,
            ),
        )
    )
    coord = ReasoningCoordinator(prompt_safety_policy=safety, llm_gateway=gateway)
    state = _state(evidence or [], problem)
    asyncio.run(coord.reason("tenant-a", state))
    return gateway.complete.call_args.args[-1]


class TestVisibleEnvelope:
    def test_envelope_lists_only_visible_ids(self):
        shown = [_ev(summary=f"r{i}", snippet=f"x{i}") for i in range(5)]
        request = _capture(evidence=shown)
        envelope_ids = {str(i) for i in request.envelope.evidence_ids}
        assert envelope_ids == {str(e.evidence_id) for e in shown}

    def test_envelope_excludes_rows_beyond_visible_cap(self):
        many = [_ev(summary=f"r{i}", snippet=f"x{i}") for i in range(150)]
        request = _capture(evidence=many)
        assert len(request.envelope.evidence_ids) <= 100
        assert "evidence_records" not in request.prompt
        assert "visible_evidence_ids" in request.prompt

    def test_cited_ids_are_always_visible(self):
        rng = _range_read(1, 10)
        request = _capture(evidence=[rng])
        assert str(rng.evidence_id) in {str(i) for i in request.envelope.evidence_ids}
        assert str(rng.evidence_id) in request.prompt


class TestPromptSections:
    def test_reasoner_request_pins_low_temperature(self):
        from investigation_agent_platform.application.investigation.reasoning import (
            REASONING_TEMPERATURE,
        )

        assert REASONING_TEMPERATURE == 0.1
        request = _capture()
        assert request.temperature == 0.1

    def test_sections_are_named_and_versioned(self):
        prompt = build_system_prompt()
        assert prompt.startswith("INVESTIGATION REASONER v3-claim-grounded-011")
        for header in (
            "## output-contract",
            "## action-authority",
            "## evidence-hierarchy",
            "## code-reading",
            "## method-and-stopping",
            "## triage-weighting",
        ):
            assert header in prompt

    def test_capability_authority_stated_once(self):
        assert build_system_prompt().count("allowed_capabilities") == 1

    def test_claim_rules_present(self):
        prompt = build_system_prompt()
        assert "candidate location, not proof" in prompt
        assert "next_window" in prompt
        assert "cite the range-read evidence ID" in prompt
        assert "path-only read is discovery" in prompt

    def test_transient_is_weighting_not_prohibition(self):
        prompt = build_system_prompt()
        assert "over deep code reads" not in prompt
        assert "weighting hint, not a prohibition" in prompt
        assert "does NOT forbid code reads" in prompt
        assert "GET_CODE" in prompt

    def test_walk_stack_parameter_shape_documented(self):
        prompt = build_system_prompt()
        assert '"frames": [{"file"' in prompt
        assert "before any other gathering" in prompt
        assert "unresolved frames are reported" in prompt

    def test_no_stale_evidence_records_reference(self):
        assert "evidence_records" not in build_system_prompt()


class TestHydrationSelection:
    def test_cited_and_code_rows_survive_the_cap(self):
        old = datetime.now(UTC) - timedelta(days=2)
        cited = _ev(summary="cited log", snippet="c")
        object.__setattr__(cited, "retrieved_at", old)
        code = _range_read(1, 5)
        object.__setattr__(code, "retrieved_at", old)
        logs = [_ev(summary=f"l{i}") for i in range(300)]
        hyp = SimpleNamespace(supporting_evidence_ids=[cited.evidence_id], refuting_evidence_ids=[])
        out = select_reasoning_evidence([*logs, cited, code], [hyp], limit=200)
        ids = {e.evidence_id for e in out}
        assert len(out) == 200
        assert cited.evidence_id in ids and code.evidence_id in ids

    def test_selection_is_order_independent(self):
        items = [_ev(summary=f"r{i}") for i in range(60)]
        shuffled = list(items)
        random.Random(3).shuffle(shuffled)
        a = [e.evidence_id for e in select_reasoning_evidence(items, [], limit=40)]
        b = [e.evidence_id for e in select_reasoning_evidence(shuffled, [], limit=40)]
        assert a == b
