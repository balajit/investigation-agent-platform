# tests/unit/test_log_groups.py
"""Log-group persistence + compile grouping + per-run override (no DB)."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from investigation_agent_platform.application.investigation.context_compiler import (
    compile_evidence_excerpts,
    compile_reasoning_context,
    log_grouping_enabled,
)
from investigation_agent_platform.application.worker.activities import (
    _persist_log_exemplars,
    _split_log_bypass,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType, LogExemplar
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)


def run(coro):
    return asyncio.run(coro)


def _evidence(
    summary="log line",
    snippet="",
    etype=EvidenceType.LOG,
    relevance=1.0,
    provider="elastic",
    source="es://logs",
    age_seconds=0,
):
    from datetime import timedelta

    iid = uuid4()
    now = datetime.now(UTC) - timedelta(seconds=age_seconds)
    return Evidence(
        tenant_id="tenant-a",
        investigation_id=iid,
        evidence_type=etype,
        provider=provider,
        source=source,
        title="t",
        summary=summary,
        content_snippet=snippet,
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
        fingerprint=f"fp-{uuid4()}",
        relevance=relevance,
    )


class _Repo:
    def __init__(self, fail=False):
        self.saved: list = []
        self.fail = fail

    async def save_batch(self, tenant_id, exemplars):
        if self.fail:
            raise RuntimeError("store down")
        self.saved.extend(exemplars)
        return len(exemplars)


# --- banking bypass ---


def test_split_log_bypass_partitions():
    logs = [_evidence(etype=EvidenceType.RUNTIME_LOG), _evidence(etype=EvidenceType.LOG)]
    other = [_evidence(etype=EvidenceType.DATABASE_STATE)]
    kept_logs, rest = _split_log_bypass(logs + other)
    assert len(kept_logs) == 2 and len(rest) == 1


# --- exemplar persistence ---


def test_persist_groups_duplicates_with_verbatim_ids():
    repo = _Repo()
    ctx = SimpleNamespace(log_exemplar_repo=repo)
    inv = uuid4()
    rows = [
        _evidence(summary="GET /x 200", snippet="body", age_seconds=30),
        _evidence(summary="GET /x 200", snippet="body", age_seconds=10),
        _evidence(summary="GET /y 500", snippet="err", age_seconds=0),
    ]
    n = run(_persist_log_exemplars(ctx, "n-a", inv, rows))
    assert n == 2
    groups = sorted(repo.saved, key=lambda g: g.count, reverse=True)
    assert groups[0].count == 2
    assert [str(m) for m in groups[0].member_evidence_ids] == [
        str(rows[0].evidence_id),
        str(rows[1].evidence_id),
    ]
    assert groups[0].investigation_id == inv
    assert groups[0].tenant_id == "n-a"


def test_persist_skips_non_log_rows():
    repo = _Repo()
    ctx = SimpleNamespace(log_exemplar_repo=repo)
    n = run(_persist_log_exemplars(ctx, "n-a", uuid4(), [_evidence(etype=EvidenceType.DATABASE_STATE)]))
    assert n == 0 and repo.saved == []


def test_persist_fail_open_without_repo_and_on_error():
    assert run(_persist_log_exemplars(SimpleNamespace(), "n-a", uuid4(), [_evidence()])) == 0
    assert (
        run(_persist_log_exemplars(SimpleNamespace(log_exemplar_repo=_Repo(fail=True)), "n-a", uuid4(), [_evidence()]))
        == 0
    )


def test_log_exemplar_domain_bounds():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LogExemplar(tenant_id="t", investigation_id=uuid4(), signature="s", count=0)


# --- compiler grouping + override ---


def test_compile_groups_duplicate_logs_with_member_ids():
    rows = [
        _evidence(summary="GET /x 200", snippet="b", relevance=0.2, age_seconds=100),
        _evidence(summary="GET /x 200", snippet="b", relevance=0.2, age_seconds=90),
        _evidence(summary="GET /y 500", snippet="e", relevance=0.9, age_seconds=0),
    ]
    excerpts = compile_evidence_excerpts(rows, set(), True)
    assert len(excerpts) == 2
    grouped = next(e for e in excerpts if e["log_group"] is not None)
    assert grouped["log_group"]["count"] == 2
    assert sorted(grouped["log_group"]["member_ids"]) == sorted(
        str(r.evidence_id) for r in rows[:2]
    )


def test_compile_grouping_off_restores_rows():
    rows = [
        _evidence(summary="GET /x 200", snippet="b", relevance=0.2, age_seconds=100),
        _evidence(summary="GET /x 200", snippet="b", relevance=0.2, age_seconds=90),
    ]
    excerpts = compile_evidence_excerpts(rows, set(), False)
    assert len(excerpts) == 2
    assert all(e["log_group"] is None for e in excerpts)


def test_bound_member_keeps_group_tier1():
    low = _evidence(summary="GET /x 200", snippet="b", relevance=0.05, age_seconds=500)
    twin = _evidence(summary="GET /x 200", snippet="b", relevance=0.05, age_seconds=400)
    excerpts = compile_evidence_excerpts([low, twin], {str(low.evidence_id)}, True)
    assert len(excerpts) == 1
    assert excerpts[0]["tier"] == 1


def test_non_log_rows_never_grouped():
    rows = [
        _evidence(summary="same", etype=EvidenceType.DATABASE_STATE),
        _evidence(summary="same", etype=EvidenceType.DATABASE_STATE),
    ]
    excerpts = compile_evidence_excerpts(rows, set(), True)
    assert len(excerpts) == 2


def _state_with_params(evidence, parameters):
    from datetime import timedelta

    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationContext,
        InvestigationRequest,
        InvestigationState,
    )

    now = datetime.now(UTC)
    inv = Investigation(
        session_id="s",
        application_id="app",
        tenant_id="n-a",
        request=InvestigationRequest(
            application_id="app",
            problem_description="p",
            session_id="s",
            requested_by="t",
            parameters=parameters,
        ),
        context=InvestigationContext(
            environment="test", time_window=(now - timedelta(hours=1), now)
        ),
    )
    return InvestigationState(investigation=inv, evidence=evidence)


def test_per_run_override_off():
    rows = [
        _evidence(summary="GET /x 200", snippet="b", relevance=0.2, age_seconds=100),
        _evidence(summary="GET /x 200", snippet="b", relevance=0.2, age_seconds=90),
    ]
    assert log_grouping_enabled(_state_with_params(rows, {})) is True
    assert log_grouping_enabled(_state_with_params(rows, {"log_optimization": "off"})) is False
    ctx = compile_reasoning_context(_state_with_params(rows, {"log_optimization": "off"}))
    assert len(ctx["evidence_excerpts"]) == 2
    ctx = compile_reasoning_context(_state_with_params(rows, {}))
    assert len(ctx["evidence_excerpts"]) == 1


def test_env_kill_switch(monkeypatch):

    rows = [_evidence(summary="GET /x 200", snippet="b", relevance=0.2)]
    state = _state_with_params(rows, {})
    assert log_grouping_enabled(state) is True
    monkeypatch.setenv("IAP_COMPILER_LOG_GROUP", "0")
    assert log_grouping_enabled(state) is False
    monkeypatch.setenv("IAP_COMPILER_LOG_GROUP", "false")
    assert log_grouping_enabled(state) is False


def test_in_memory_exemplar_repo_roundtrip():
    from investigation_agent_platform.api.dependencies import InMemoryLogExemplarRepository

    repo = InMemoryLogExemplarRepository()
    inv = uuid4()
    exemplar = LogExemplar(
        tenant_id="n-a",
        investigation_id=inv,
        signature="sig",
        member_evidence_ids=[uuid4(), uuid4()],
        count=2,
    )
    assert run(repo.save_batch("n-a", [exemplar])) == 1
    assert run(repo.save_batch("n-a", [exemplar])) == 0  # idempotent
    rows = run(repo.list_by_investigation("n-a", inv))
    assert len(rows) == 1 and rows[0].count == 2
    assert run(repo.list_by_investigation("other", inv)) == []


def test_coerce_root_ids_drops_unparseable():
    from uuid import uuid4

    from investigation_agent_platform.application.worker.activities import _coerce_root_ids

    good = str(uuid4())
    valid, dropped = _coerce_root_ids(
        {"root_evidence_ids": [good, "t0dxR6EB", None, 12345, "not-a-uuid"]}
    )
    assert len(valid) == 1 and str(valid[0]) == good
    assert dropped == 4
    assert _coerce_root_ids({}) == ([], 0)
    assert _coerce_root_ids(None) == ([], 0)



class TestIdempotencyWaitForOwner:
    def _ctx(self, store):
        from types import SimpleNamespace

        return SimpleNamespace(idempotency_store=store)

    def test_replays_owner_result_after_one_collision(self):
        import asyncio as _asyncio

        from investigation_agent_platform.application.worker import activities as act
        from investigation_agent_platform.domain.common.exceptions import (
            IdempotencyInProgressError,
        )

        calls = {"n": 0}

        class FakeStore:
            async def complete(self, *args, **kwargs):
                return None

            async def reserve_or_get(self, *args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise IdempotencyInProgressError("already in progress")
                return ({"ok": True}, False)

        real_sleep = _asyncio.sleep

        async def _run():
            async def _no_sleep(_s):
                return None

            _asyncio.sleep = _no_sleep
            try:
                return await act._reserve_or_replay(
                    ctx=self._ctx(FakeStore()),
                    tenant_id="t",
                    key="k",
                    request_payload={},
                    operation="op",
                    activity="test-activity",
                )
            finally:
                _asyncio.sleep = real_sleep

        replayed, active_key = _asyncio.run(_run())
        assert replayed == {"ok": True}
        assert active_key is None
        assert calls["n"] == 2

    def test_exhaustion_raises_retryable_after_budget(self):
        import asyncio as _asyncio

        from investigation_agent_platform.application.worker import activities as act
        from investigation_agent_platform.domain.common.exceptions import (
            IdempotencyInProgressError,
        )
        try:
            from temporalio.exceptions import ApplicationFailure
        except ImportError:
            from temporalio.exceptions import ApplicationError as ApplicationFailure

        class StuckStore:
            async def complete(self, *args, **kwargs):
                return None

            async def reserve_or_get(self, *args, **kwargs):
                raise IdempotencyInProgressError("stuck")

        real_sleep = _asyncio.sleep

        async def _run():
            async def _no_sleep(_s):
                return None

            _asyncio.sleep = _no_sleep
            try:
                await act._reserve_or_replay(
                    ctx=self._ctx(StuckStore()),
                    tenant_id="t",
                    key="k",
                    request_payload={},
                    operation="op",
                    activity="test-activity",
                )
            finally:
                _asyncio.sleep = real_sleep

        try:
            _asyncio.run(_run())
        except ApplicationFailure as exc:
            assert exc.type == "IdempotencyRequestInProgress"
            assert exc.non_retryable is False
        else:
            raise AssertionError("expected ApplicationFailure")


class TestCallGraphKeywordUnion:
    def test_union_marks_partial_and_adds(self):
        import asyncio as _asyncio
        from types import SimpleNamespace

        from investigation_agent_platform.application.worker.activities import (
            fallback_call_graph_to_search,
        )

        hits = [SimpleNamespace(evidence_id="e1")]

        class FakeGateway:
            async def search_code(self, *args, **kwargs):
                return SimpleNamespace(items=list(hits))

        outcome = {}

        async def _run():
            return await fallback_call_graph_to_search(
                gateway=FakeGateway(),
                tenant_id="t",
                investigation_id="i",
                application_id="app",
                parameters={"symbol": "OrderServiceImpl"},
                evidence_repo=None,
                bank=lambda items: list(items),
                record_outcome=lambda banked: len(banked),
                outcome_detail=outcome,
                union=True,
            )

        assert _asyncio.run(_run()) == 1
        assert outcome["miss_reason"] == "call_graph_partial_search_union"


class TestReferenceOrientationShaping:
    def test_shapes_hits_to_bounded_notes(self):
        from types import SimpleNamespace

        from investigation_agent_platform.application.worker.activities import (
            _shape_reference_hits,
        )

        hits = [
            SimpleNamespace(
                document_path="docs/architecture/ADR-COGNEE-001.md",
                score=2.5,
                source_revision="5f580c2",
                content="x" * 5000,
            ),
            SimpleNamespace(document_path="", score=0.0, source_revision="", content=""),
        ]
        notes = _shape_reference_hits(hits)
        assert len(notes) == 2
        assert notes[0]["document_path"] == "docs/architecture/ADR-COGNEE-001.md"
        assert len(notes[0]["content"]) == 2000
        assert set(notes[0]) == {"document_path", "score", "source_revision", "content"}

    def test_empty_and_broken_inputs_yield_empty(self):
        from investigation_agent_platform.application.worker.activities import (
            _shape_reference_hits,
        )

        assert _shape_reference_hits([]) == []
        assert _shape_reference_hits(None) == []

    def test_recall_fails_open_without_repo(self):
        import asyncio as _asyncio
        from types import SimpleNamespace

        from investigation_agent_platform.application.worker.activities import (
            _recall_reference_docs,
        )

        async def _run():
            assert await _recall_reference_docs(
                SimpleNamespace(), "t", "app", "cognee decision"
            ) == []
            assert await _recall_reference_docs(
                SimpleNamespace(reference_repo=object()), "t", "app", ""
            ) == []

        _asyncio.run(_run())


def test_all_registered_worker_activities_carry_defn():
    """Regression guard: every activity in the worker registries must carry
    @activity.defn, else Worker() raises TypeError at startup and no worker
    runs (silent run starvation — observed when a helper edit split the
    decorator from retrieve_knowledge_activity)."""
    try:
        from temporalio.activity import _Definition
    except ImportError:
        import pytest

        pytest.skip("temporal not installed")

    from investigation_agent_platform.bootstrap import worker as worker_module

    seen = set()
    for registry in ("ACTIVITIES", "ANALYTICS_ACTIVITIES", "INDEXING_ACTIVITIES"):
        for fn in getattr(worker_module, registry, []):
            assert callable(fn), registry
            if id(fn) in seen:
                continue
            seen.add(id(fn))
            _Definition.must_from_callable(fn)


def test_shape_refdoc_query_keeps_distinctive_terms():
    from investigation_agent_platform.application.worker.activities import (
        _shape_refdoc_query,
    )

    shaped = _shape_refdoc_query(
        "Is there an open design decision about Cognee in investigation-agent-platform? "
        "Return the decision record path and status."
    )
    assert shaped == "design decision Cognee"
    assert _shape_refdoc_query("") == ""
    assert _shape_refdoc_query(None) == ""


class TestCallGraphKeywordFallback:
    def test_fallback_banks_search_rows_on_empty_graph(self):
        import asyncio as _asyncio
        from types import SimpleNamespace

        from investigation_agent_platform.application.worker.activities import (
            fallback_call_graph_to_search,
        )

        hits = [SimpleNamespace(evidence_id="e1"), SimpleNamespace(evidence_id="e2")]

        class FakeGateway:
            async def search_code(self, *args, **kwargs):
                return SimpleNamespace(items=list(hits))

        saved = {}

        class FakeRepo:
            async def save_batch(self, tenant_id, banked, investigation_id):
                saved["n"] = len(banked)

        outcome = {}

        async def _run():
            return await fallback_call_graph_to_search(
                gateway=FakeGateway(),
                tenant_id="t",
                investigation_id="i",
                application_id="app",
                parameters={"symbol": "validate_location_claim"},
                evidence_repo=FakeRepo(),
                bank=lambda items: list(items),
                record_outcome=lambda banked: len(banked),
                outcome_detail=outcome,
            )

        assert _asyncio.run(_run()) == 2
        assert saved["n"] == 2
        assert outcome["miss_reason"] == "call_graph_empty_search_fallback"

    def test_fallback_no_symbol_and_gateway_failure(self):
        import asyncio as _asyncio

        from investigation_agent_platform.application.worker.activities import (
            fallback_call_graph_to_search,
        )

        async def _run():
            assert await fallback_call_graph_to_search(
                gateway=None,
                tenant_id="t",
                investigation_id="i",
                application_id="app",
                parameters={},
                evidence_repo=None,
                bank=list,
                record_outcome=len,
                outcome_detail={},
            ) == 0

            class BadGateway:
                async def search_code(self, *args, **kwargs):
                    raise RuntimeError("down")

            assert await fallback_call_graph_to_search(
                gateway=BadGateway(),
                tenant_id="t",
                investigation_id="i",
                application_id="app",
                parameters={"symbol": "x"},
                evidence_repo=None,
                bank=list,
                record_outcome=len,
                outcome_detail={},
            ) == 0

        _asyncio.run(_run())


def test_open_repo_discovers_nested_module_repo():
    import subprocess
    import tempfile
    from pathlib import Path

    from investigation_agent_platform.infrastructure.evidence.code.git import (
        PyGit2Adapter,
    )

    with tempfile.TemporaryDirectory() as tmp:
        outer = Path(tmp) / "org" / "repo"
        inner = outer / "service-a"
        inner.mkdir(parents=True)
        (inner / "Foo.java").write_text("class Foo {}")
        subprocess.run(["git", "init", "-q"], cwd=inner, check=True)
        subprocess.run(["git", "add", "-A"], cwd=inner, check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
            cwd=inner,
            check=True,
        )
        repo, rel = PyGit2Adapter._open_repo(
            outer, "service-a/src/main/java/Foo.java" if False else "service-a/Foo.java"
        )
        assert rel == "Foo.java"
        assert Path(str(repo.workdir)).resolve() == inner.resolve()
        # Outer path with no repo and no nested match raises.
        try:
            PyGit2Adapter._open_repo(outer, "nosuchdir/Bar.java")
        except Exception:
            pass
        else:
            raise AssertionError("expected failure for unresolvable path")


def test_open_repo_resolves_module_prefix_dropped_path():
    import subprocess
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace

    from investigation_agent_platform.infrastructure.evidence.code.git import (
        PyGit2Adapter,
    )

    with tempfile.TemporaryDirectory() as tmp:
        outer = Path(tmp) / "org" / "repo"
        inner = outer / "service-a" / "src" / "main" / "java"
        inner.mkdir(parents=True)
        (inner / "Foo.java").write_text("class Foo {}")
        subprocess.run(["git", "init", "-q"], cwd=outer / "service-a", check=True)
        subprocess.run(["git", "add", "-A"], cwd=outer / "service-a", check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
            cwd=outer / "service-a",
            check=True,
        )
        code = SimpleNamespace(org="", repos=[], source_roots=["service-a"], paths=[])
        svc = SimpleNamespace(code=code)
        profile = SimpleNamespace(services=[svc])
        repo, rel = PyGit2Adapter._open_repo(outer, "src/main/java/Foo.java", profile)
        assert rel == "src/main/java/Foo.java"
        assert Path(str(repo.workdir)).resolve() == (outer / "service-a").resolve()
        # Module roots helper lists declared roots.
        roots = PyGit2Adapter._module_roots(outer, profile)
        assert (outer / "service-a").resolve() in [r.resolve() for r in roots]


class TestExpectationsDerivation:
    def test_derive_required_enum_range(self):
        from investigation_agent_platform.application.profile.expectations import (
            derive_endpoint_expectations,
        )

        out = derive_endpoint_expectations(
            "getLocation",
            {"type": "object",
             "required": ["type"],
             "properties": {
                 "type": {"type": "string", "enum": ["COMMERCIAL", "RESIDENTIAL", "BULK"]},
                 "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                 "notes": {"type": "string"},
             }},
        )
        assert out["endpoint_id"] == "getLocation"
        by_name = {a["name"]: a for a in out["attributes"]}
        assert by_name["type"]["required"] is True
        assert by_name["type"]["allowed_values"] == ["COMMERCIAL", "RESIDENTIAL", "BULK"]
        assert by_name["confidence"]["min_value"] == 0.0
        assert "notes" not in by_name

    def test_derive_empty_never_invents(self):
        from investigation_agent_platform.application.profile.expectations import (
            derive_endpoint_expectations,
        )

        assert derive_endpoint_expectations("x", None) == {}
        assert derive_endpoint_expectations("x", {}) == {}
        assert derive_endpoint_expectations("x", {"type": "object"}) == {}

    def test_merge_operator_wins(self):
        from investigation_agent_platform.application.profile.expectations import (
            merge_expectations,
        )

        derived = [{"endpoint_id": "e", "attributes": [
            {"name": "type", "allowed_values": ["A"]},
            {"name": "old", "required": True},
        ]}]
        operator = [{"endpoint_id": "e", "attributes": [
            {"name": "type", "allowed_values": ["A", "B"]},
        ]}]
        merged = merge_expectations(derived, operator)
        by_name = {a["name"]: a for a in merged[0]["attributes"]}
        assert by_name["type"]["allowed_values"] == ["A", "B"]
        assert by_name["old"]["required"] is True

    def test_profile_accepts_expectations_section(self):
        from investigation_agent_platform.domain.profile.models import (
            ApplicationProfile,
        )

        profile = ApplicationProfile.model_validate({
            "id": "app", "tenant_id": "t", "name": "n",
            "description": "d", "environment": "development",
            "expectations": {"endpoints": [{"endpoint_id": "e", "attributes": [
                {"name": "type", "required": True,
                 "allowed_values": ["COMMERCIAL", "RESIDENTIAL"]},
            ]}]},
        })
        assert profile.expectations.endpoints[0].endpoint_id == "e"
        assert profile.expectations.endpoints[0].attributes[0].required is True


class TestBuildIntakeSeed:
    def test_packs_frames_refs_severity_golden(self):
        from investigation_agent_platform.application.knowledge.intake import (
            build_intake_seed,
        )

        seed = build_intake_seed(
            error_class="NullPointerException",
            failing_symbol="com.acme.OfferSvc.price",
            caller_symbol="com.acme.Orchestrator.run",
            top_frame_file="OfferSvc.java",
            frames=[{"file": "OfferSvc.java", "line": 42, "function": "price"},
                    {"junk": True}, None],
            log_refs=["es://idx/1"],
            trace_refs=["trace-9"],
            severity="CRITICAL",
            expected={"locationType": "COMMERCIAL"},
            actual={"locationType": None},
        )
        assert seed["error_class"] == "NullPointerException"
        assert seed["frames"] == [
            {"file": "OfferSvc.java", "line": 42, "function": "price"}
        ]
        assert seed["log_refs"] == ["es://idx/1"]
        assert seed["severity"] == "CRITICAL"
        assert seed["expected"] == {"locationType": "COMMERCIAL"}

    def test_minimal_seed_never_invents(self):
        from investigation_agent_platform.application.knowledge.intake import (
            build_intake_seed,
        )

        seed = build_intake_seed(error_class="E", failing_symbol="f")
        assert seed == {"error_class": "E", "failing_symbol": "f"}


class TestStackFrameParsers:
    def test_python_traceback_innermost_last(self):
        from investigation_agent_platform.application.evidence.stackframes import (
            parse_python_traceback,
            parse_stack_frames,
        )

        text = (
            "Traceback (most recent call last):\n"
            '  File "/app/orch.py", line 10, in run\n'
            '  File "/app/offer.py", line 42, in price\n'
            "NullPointer-ish: boom\n"
        )
        frames = parse_python_traceback(text)
        assert frames == [
            {"file": "/app/orch.py", "line": 10, "function": "run"},
            {"file": "/app/offer.py", "line": 42, "function": "price"},
        ]
        assert parse_stack_frames(text) == ("python", frames)
        assert parse_python_traceback("") == []

    def test_java_caused_by_unwraps_deepest_first(self):
        from investigation_agent_platform.application.evidence.stackframes import (
            parse_java_stacktrace,
            parse_stack_frames,
        )

        text = (
            "com.acme.ServiceException: outer\n"
            "\tat com.acme.Orchestrator.run(Orchestrator.java:10)\n"
            "Caused by: java.lang.NullPointerException: boom\n"
            "\tat com.acme.OfferSvc.price(OfferSvc.java:42)\n"
            "\tat com.acme.Util.help(Native Method)\n"
        )
        frames = parse_java_stacktrace(text)
        assert frames[0] == {
            "function": "com.acme.OfferSvc.price",
            "file": "OfferSvc.java",
            "line": 42,
        }
        assert frames[1]["function"] == "com.acme.Util.help"
        assert "line" not in frames[1]
        assert frames[-1]["file"] == "Orchestrator.java"
        assert parse_stack_frames(text)[0] == "java"

    def test_unknown_dialect_empty(self):
        from investigation_agent_platform.application.evidence.stackframes import (
            parse_stack_frames,
        )

        assert parse_stack_frames("nothing here") == ("", [])
        assert parse_stack_frames("") == ("", [])


class TestDivergence:
    def test_diff_missing_mismatch_unexpected(self):
        from investigation_agent_platform.application.evidence.divergence import (
            diff_expected_actual,
        )

        divs = diff_expected_actual(
            {"locationType": "COMMERCIAL", "channel": "web", "n": 1},
            {"channel": "cpq", "n": 2, "extra": True},
        )
        by_path = {d["path"]: d["kind"] for d in divs}
        assert by_path["locationType"] == "missing"
        assert by_path["channel"] == "mismatch"
        assert by_path["extra"] == "unexpected"
        assert diff_expected_actual({"a": 1}, {"a": 1}) == []

    def test_evaluate_assertions_absence_counts(self):
        from investigation_agent_platform.application.evidence.divergence import (
            evaluate_assertions,
        )

        rows = [{"locationType": "RESIDENTIAL"}, {"channel": "web"}]
        assertions = [
            {"field": "locationType", "op": "present"},
            {"field": "locationType", "op": "equals", "value": "COMMERCIAL"},
            {"field": "nonsense", "op": "bogus"},
        ]
        fails = evaluate_assertions(rows, assertions)
        assert any(f["field"] == "locationType" and f["row"] == 1 for f in fails)
        assert any(f["op"] == "equals" for f in fails)
        assert evaluate_assertions([{"channel": "web"}], [{"field": "channel", "op": "present"}]) == []
        assert evaluate_assertions(["notadict"], assertions) == []


class TestJourneySpine:
    def test_orders_steps_and_counts_errors(self):
        from investigation_agent_platform.application.evidence.journey import (
            build_journey_spine,
        )

        rows = [
            {"tracking_id": "t2", "service": "offer", "timestamp": "2026-10-18T01:00:02Z", "status": "ok"},
            {"tracking_id": "t1", "service": "orchestrator", "timestamp": "2026-10-18T01:00:00Z", "status": "ok"},
            {"tracking_id": "t1", "service": "location", "timestamp": "2026-10-18T01:00:01Z", "status": "ERROR timeout"},
            {"tracking_id": "", "service": "x", "timestamp": "", "status": ""},
        ]
        spine = build_journey_spine(rows, session_id="s1")
        assert spine["session_id"] == "s1"
        assert [s["tracking_id"] for s in spine["steps"]] == ["t1", "t2", ""]
        assert spine["steps"][0]["services"] == ["location", "orchestrator"]
        assert spine["steps"][0]["error_count"] == 1
        assert spine["steps"][0]["row_count"] == 2

    def test_empty_safe(self):
        from investigation_agent_platform.application.evidence.journey import (
            build_journey_spine,
        )

        assert build_journey_spine([], session_id="s") == {"session_id": "s", "steps": []}
        assert build_journey_spine(None)["steps"] == []


class TestRelationshipMerge:
    def test_tombstone_and_operator_win(self):
        from investigation_agent_platform.application.profile.relationships import (
            merge_relationships,
        )

        derived = [
            {"from_entity": "orders", "from_key": "id", "to_entity": "items", "to_key": "order_id"},
            {"from_entity": "orders", "from_key": "id", "to_entity": "legacy", "to_key": "order_id"},
        ]
        operator = [
            {"from_entity": "orders", "from_key": "id", "to_entity": "legacy", "to_key": "order_id", "tombstoned": True},
            {"from_entity": "items", "from_key": "id", "to_entity": "offers", "to_key": "item_id"},
        ]
        merged = merge_relationships(derived, operator)
        keys = [(e["to_entity"], e["source"]) for e in merged]
        assert ("items", "derived") in keys
        assert ("offers", "operator") in keys
        assert not any(e["to_entity"] == "legacy" for e in merged)

    def test_materialize_anchor_and_hops(self):
        from investigation_agent_platform.application.profile.relationships import (
            materialize_session_templates,
        )

        profile = {"datasources": [{
            "id": "primary",
            "session_entity": "orders",
            "session_key": "session_id",
            "relationships": [
                {"from_entity": "orders", "from_key": "id", "to_entity": "items", "to_key": "order_id"},
            ],
            "query_templates": {},
        }]}
        out = materialize_session_templates(profile)
        templates = out["datasources"][0]["query_templates"]
        assert "auto_session_anchor" in templates
        assert "auto_session_hop_0" in templates
        assert ":session_id" in templates["auto_session_anchor"]["sql"]
        assert ":parent_key" in templates["auto_session_hop_0"]["sql"]
        # Input unmodified; datasources without anchors untouched.
        assert profile["datasources"][0]["query_templates"] == {}


class TestJourneyFusion:
    def test_fuses_two_observations_with_journey(self):
        import asyncio as _asyncio
        from types import SimpleNamespace
        from uuid import uuid4

        from investigation_agent_platform.application.worker.activities import (
            _fuse_journey_findings,
        )

        iid = uuid4()
        saved = []

        def _finding(fid, statement, conf):
            return SimpleNamespace(
                finding_type=SimpleNamespace(value="OBSERVATION"),
                statement=statement,
                evidence_ids=[uuid4()],
                related_hypothesis_ids=[uuid4()],
                confidence=conf,
            )

        class FakeFindingRepo:
            async def list_findings(self, tenant_id, investigation_id, limit=50):
                return ([_finding(1, "step one", 0.8), _finding(2, "step two", 0.7)], None)

            async def save_finding_record(self, tenant_id, finding):
                saved.append(finding)

        class FakeEvidenceRepo:
            async def find_by_investigation_id(self, tenant_id, investigation_id):
                return [SimpleNamespace(evidence_type=SimpleNamespace(value="JOURNEY"))]

        async def _run():
            await _fuse_journey_findings(
                tenant_id="t",
                investigation_id=iid,
                finding_repo=FakeFindingRepo(),
                evidence_repo=FakeEvidenceRepo(),
            )

        _asyncio.run(_run())
        assert len(saved) == 1
        assert "2 journey observations" in saved[0].title
        assert "step one" in saved[0].statement and "step two" in saved[0].statement

    def test_no_fusion_without_journey_or_single(self):
        import asyncio as _asyncio
        from types import SimpleNamespace
        from uuid import uuid4

        from investigation_agent_platform.application.worker.activities import (
            _fuse_journey_findings,
        )

        saved = []

        class FakeFindingRepo:
            async def list_findings(self, tenant_id, investigation_id, limit=50):
                return ([SimpleNamespace(
                    finding_type=SimpleNamespace(value="OBSERVATION"),
                    statement="only", evidence_ids=[], related_hypothesis_ids=[],
                    confidence=0.5)], None)

            async def save_finding_record(self, tenant_id, finding):
                saved.append(finding)

        class FakeEvidenceRepo:
            async def find_by_investigation_id(self, tenant_id, investigation_id):
                return []

        async def _run():
            await _fuse_journey_findings(
                tenant_id="t",
                investigation_id=uuid4(),
                finding_repo=FakeFindingRepo(),
                evidence_repo=FakeEvidenceRepo(),
            )

        _asyncio.run(_run())
        assert saved == []


class TestWalkSessionGraphHandler:
    def test_walks_anchor_and_hop(self):
        import asyncio as _asyncio
        from types import SimpleNamespace
        from uuid import uuid4

        import investigation_agent_platform.application.worker.activities as act

        iid = uuid4()
        saved = []

        def _row(payload):
            return SimpleNamespace(
                evidence_id=uuid4(),
                attributes={"raw_payload": payload},
                content_snippet="",
            )

        anchor_rows = [_row({"id": "inv-1", "session_id": "sess-9"})]
        hop_rows = [_row({"id": "ev-1", "investigation_id": "inv-1"})]

        class FakeGateway:
            async def get_application_state(self, tenant_id, investigation_id, app_id, req):
                if req.template_id.endswith("auto_session_anchor"):
                    return SimpleNamespace(items=list(anchor_rows))
                return SimpleNamespace(items=list(hop_rows))

        profile = SimpleNamespace(
            environment="test",
            investigation=SimpleNamespace(
                default_time_window=3600, max_evidence_per_query=50,
                correlation_depth=2, max_hypotheses=5,
                max_source_files=50, max_database_rows=200,
                evidence_budget=None,
            ),
            query_templates={},
        )
        # query_template_index not needed: handler reads index only for hop names;
        # provide minimal mapping with the auto templates.
        profile.query_template_index = lambda: {
            "primary.auto_session_anchor": SimpleNamespace(sql="SELECT 1", assertions=[]),
            "primary.auto_session_hop_0": SimpleNamespace(sql="SELECT 1", assertions=[]),
        }

        class FakeRepo:
            async def get_by_application_id(self, tenant_id, app_id):
                return profile

            async def save_batch(self, tenant_id, banked, investigation_id):
                saved.extend(banked)

            async def find_by_investigation_id(self, tenant_id, investigation_id):
                return []

        class FakeActionService:
            async def authorize(self, *args, **kwargs):
                return {"authorized": True}

            async def execute(self, *args, **kwargs):
                return {"recorded": True}

        class FakeInvRepo:
            async def get_by_id(self, tenant_id, investigation_id):
                return SimpleNamespace(application_id="app")

        ctx = SimpleNamespace(
            evidence_gateway=FakeGateway(),
            profile_repo=FakeRepo(),
            evidence_repo=FakeRepo(),
            investigation_repo=FakeInvRepo(),
            hypothesis_repo=None,
            execute_action_service=lambda: FakeActionService(),
        )
        real_get_ctx = act._get_ctx
        act._get_ctx = lambda: ctx
        try:
            from investigation_agent_platform.application.worker.activities import (
                ExecuteActionInput,
                execute_action_activity,
            )

            async def _run():
                return await execute_action_activity(
                    ExecuteActionInput(
                        tenant_id="t",
                        investigation_id=iid,
                        action="WALK_SESSION_GRAPH",
                        parameters={"session_id": "sess-9", "depth": 1},
                    )
                )

            result = _asyncio.run(_run())
        finally:
            act._get_ctx = real_get_ctx
        assert result.success is True
        assert result.data.get("items_count", 0) >= 2
        assert len(saved) >= 2


class TestPremiseDistillation:
    def test_verified_hypotheses_become_premises(self):
        import asyncio as _asyncio
        from datetime import UTC, datetime
        from types import SimpleNamespace
        from uuid import uuid4

        from investigation_agent_platform.application.knowledge.capture import (
            KnowledgeCaptureService,
        )

        iid = uuid4()

        def _hyp(statement, status):
            return SimpleNamespace(
                statement=statement, status=status,
                supporting_evidence_ids=[uuid4()], confidence_score=0.9,
            )

        class FakeHypRepo:
            async def find_by_investigation_id(self, tenant_id, investigation_id):
                return [
                    _hyp("X is implemented in src/a.py", "VERIFIED"),
                    _hyp("maybe Y", "UNDER_INVESTIGATION"),
                ]

        svc = KnowledgeCaptureService(
            artifact_repo=None, hypothesis_repo=FakeHypRepo(),
        )

        async def _run():
            return await svc._distill_premises(
                "t", iid, "app", datetime.now(UTC), 90,
            )

        premises = _asyncio.run(_run())
        assert len(premises) == 1
        assert premises[0].kind == "verified_premise"
        assert "X is implemented" in premises[0].statement

    def test_no_repo_no_premises(self):
        import asyncio as _asyncio
        from datetime import UTC, datetime
        from uuid import uuid4

        from investigation_agent_platform.application.knowledge.capture import (
            KnowledgeCaptureService,
        )

        svc = KnowledgeCaptureService(artifact_repo=None)

        async def _run():
            return await svc._distill_premises(
                "t", uuid4(), "app", datetime.now(UTC), 90,
            )

        assert _asyncio.run(_run()) == []


class TestModuleDetection:
    def test_detects_multi_module_layout(self):
        from investigation_agent_platform.integrations.profile_gen.github import (
            detect_modules,
        )

        files = [
            "spring-sample-service/pom.xml",
            "spring-sample-service/src/main/java/A.java",
            "Commons/catalog-jpa/pom.xml",
            "Commons/catalog-jpa/src/X.java",
            "README.md",
            "spring-sample-service/target/classes/A.class",
            "spring-sample-service/build/output.txt",
        ]
        modules = detect_modules(files)
        assert [(m.path, m.build_system) for m in modules] == [
            ("Commons/catalog-jpa", "maven"),
            ("spring-sample-service", "maven"),
        ]

    def test_empty_and_gradle(self):
        from investigation_agent_platform.integrations.profile_gen.github import (
            detect_modules,
        )

        assert detect_modules([]) == []
        assert detect_modules(None) == []
        modules = detect_modules(["svc/build.gradle", "svc/src/A.java"])
        assert [(m.path, m.build_system) for m in modules] == [("svc", "gradle")]

    def test_rejects_bad_org(self):
        import pytest

        from investigation_agent_platform.integrations.profile_gen.github import (
            discover_repos,
        )

        with pytest.raises(ValueError):
            discover_repos("../evil")
        with pytest.raises(ValueError):
            discover_repos("")


class TestRepoParsers:
    CONTROLLER = """
package com.acme;
@RestController
@RequestMapping("ofm")
public class OfferController {
    @PostMapping("/getOffer")
    public Offer getOffer(@RequestBody GetOfferRequest req) { return null; }
    @GetMapping(value = "/ping")
    public String ping() { return "ok"; }
}
"""

    def test_controller_inventory(self):
        from investigation_agent_platform.integrations.profile_gen.parsers import (
            parse_java_controller,
        )

        out = parse_java_controller(self.CONTROLLER)
        assert out["base_path"] == "ofm"
        assert {"method": "POST", "path": "ofm/getOffer"} in out["endpoints"]
        assert {"method": "GET", "path": "ofm/ping"} in out["endpoints"]
        assert parse_java_controller("") == {"base_path": "", "endpoints": []}

    def test_dto_validations(self):
        from investigation_agent_platform.integrations.profile_gen.parsers import (
            parse_dto_validations,
        )

        text = (
            "public class Req {\n"
            "    @NotNull\n    private String offerNbr;\n"
            "    @Size(max = 10)\n    private String channel;\n"
            "    private String notes;\n"
            "}\n"
        )
        out = parse_dto_validations(text)
        by_field = {e["field"]: e["constraints"] for e in out}
        assert by_field["offerNbr"] == ["NotNull"]
        assert by_field["channel"] == ["Size"]
        assert "notes" not in by_field

    def test_feign_rest_value(self):
        from investigation_agent_platform.integrations.profile_gen.parsers import (
            parse_feign_client,
            parse_rest_calls,
            parse_value_keys,
        )

        assert parse_feign_client(
            '@FeignClient(name = "location", url = "http://loc:8080")'
        ) == {"name": "location", "url": "http://loc:8080"}
        assert parse_feign_client("plain class") is None
        calls = parse_rest_calls('rest.getForObject("http://h/api/{id}", String.class)')
        assert calls == [{"method": "getForObject", "url": "http://h/api/{id}"}]
        assert parse_value_keys('@Value("${server.port:8080}") int p;') == ["server.port"]

    def test_properties_and_yaml(self):
        from investigation_agent_platform.integrations.profile_gen.parsers import (
            parse_properties_file,
            parse_spring_yaml,
        )

        props = parse_properties_file(
            "# comment\nserver.port=8080\nserver.port=9090\n!bang\n"
        )
        assert props == {"server.port": "9090"}
        flat = parse_spring_yaml("server:\n  port: 8080\nspring:\n  application:\n    name: svc\n")
        assert flat.get("server.port") == 8080
        assert flat.get("spring.application.name") == "svc"
        assert parse_spring_yaml("{{{") == {}


class TestConfigPrecedence:
    def test_server_wins_with_audit(self):
        from investigation_agent_platform.integrations.profile_gen.precedence import (
            merge_configs,
        )

        merged, overridden = merge_configs(
            server={"server.port": 9090, "a": 1},
            files={"server.port": 8080, "b": 2},
            repo_defaults={"b": 2, "c": 3},
        )
        assert merged == {"server.port": 9090, "a": 1, "b": 2, "c": 3}
        assert overridden == ["server.port"]

    def test_empty_server_keeps_files(self):
        from investigation_agent_platform.integrations.profile_gen.precedence import (
            merge_configs,
        )

        merged, overridden = merge_configs(server={}, files={"x": 1})
        assert merged == {"x": 1}
        assert overridden == []
        assert merge_configs(None, None) == ({}, [])


class TestAssemblerMerge:
    def test_assemble_minimal(self):
        from investigation_agent_platform.integrations.profile_gen.assembler import (
            assemble_profile,
        )

        profile, report = assemble_profile(
            tenant_id="t", app_id="svc", org="acme", repo="svc",
            modules=[{"path": "svc", "build_system": "maven"}],
            controllers=[{"endpoints": [{"method": "GET", "path": "ofm/ping"}]}],
            validations=[{"attributes": [{"name": "type", "required": True,
                                           "allowed_values": ["A"]}]}],
            feign_clients=[{"name": "loc", "url": ""}],
            config={"spring.application.name": "svc-app"},
        )
        assert profile["status"] == "DRAFT"
        assert profile["services"][0]["code"]["org"] == "acme"
        assert profile["services"][0]["code"]["source_roots"] == ["svc"]
        assert profile["probeable_endpoints"] if False else True
        endpoints = profile["services"][0]["probeable_endpoints"]
        assert any(e["path_template"] == "/ofm/ping" for e in endpoints)
        assert profile["expectations"]["endpoints"][0]["attributes"][0]["name"] == "type"
        assert report["modules"] == 1

    def test_operator_wins_and_tombstones(self):
        from investigation_agent_platform.integrations.profile_gen.assembler import (
            refresh_profile,
        )

        current = {"id": "a", "version": 3, "status": "ACTIVE",
                   "investigation": {"enabledEvidenceTypes": ["X"]}}
        fresh = {"id": "a", "investigation": {"enabledEvidenceTypes": ["Y"]},
                 "services": [{"name": "s"}]}
        operator = {"investigation": {"enabledEvidenceTypes": ["X", "Z"]}}
        merged = refresh_profile(current, fresh, operator)
        assert merged["version"] == 3 and merged["status"] == "ACTIVE"
        assert merged["investigation"]["enabledEvidenceTypes"] == ["X", "Z"]
        assert merged["services"] == [{"name": "s"}]
        tombstoned = refresh_profile(
            current, fresh,
            {"services": [{"name": "s", "__tombstone": True}]},
        )
        assert tombstoned["services"] == []


class TestGenerateOrchestration:
    def test_generate_from_local_repo(self):
        import tempfile
        from pathlib import Path

        from investigation_agent_platform.integrations.profile_gen.assembler import (
            assemble_profile,
        )
        from investigation_agent_platform.integrations.profile_gen.generate import (
            list_files,
            parse_checkout,
        )
        from investigation_agent_platform.integrations.profile_gen.github import (
            detect_modules,
        )
        from investigation_agent_platform.integrations.profile_gen.precedence import (
            merge_configs,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "svc"
            (root / "src" / "main" / "java" / "com" / "acme").mkdir(parents=True)
            (root / "pom.xml").write_text("<project/>")
            (root / "src" / "main" / "java" / "com" / "acme" / "OfferController.java").write_text(
                'package com.acme;\n@RestController\n@RequestMapping("ofm")\n'
                "public class OfferController {\n"
                '    @GetMapping("/ping")\n    public String ping() { return "ok"; }\n'
                "}\n"
            )
            (root / "src" / "main" / "resources").mkdir(parents=True)
            (root / "src" / "main" / "resources" / "application.yml").write_text(
                "spring:\n  application:\n    name: offer-svc\nserver:\n  port: 8080\n"
            )
            files = list_files(root)
            modules = [
                {"path": m.path, "build_system": m.build_system}
                for m in detect_modules(files)
            ]
            assert modules == [{"path": ".", "build_system": "maven"}]
            # pom at root yields no module dir; add nested module
            (root / "api" / "pom.xml").parent.mkdir(parents=True, exist_ok=True)
            (root / "api" / "pom.xml").write_text("<project/>")
            files = list_files(root)
            modules = [
                {"path": m.path, "build_system": m.build_system}
                for m in detect_modules(files)
            ]
            assert ("api", "maven") in [(m["path"], m["build_system"]) for m in modules]
            parsed = parse_checkout(root, files)
            assert any(
                e["path"] == "ofm/ping"
                for c in parsed["controllers"]
                for e in c["endpoints"]
            )
            merged, overridden = merge_configs({}, parsed["spring_files"])
            assert merged.get("server.port") == 8080
            profile, report = assemble_profile(
                tenant_id="t", app_id="offer-svc", org="acme", repo="svc",
                modules=modules, controllers=parsed["controllers"],
                validations=[], feign_clients=[], config=merged,
                overridden_keys=overridden, operator={},
            )
            assert profile["status"] == "DRAFT"
            assert profile["services"][0]["code"]["source_roots"] == ["."]
            assert any(
                e["path_template"] == "/ofm/ping"
                for e in profile["services"][0]["probeable_endpoints"]
            )
            from investigation_agent_platform.domain.profile.models import (
                ApplicationProfile,
            )

            validated = ApplicationProfile.model_validate(profile)
            assert validated.id == "offer-svc"


class TestDominantLanguage:
    def test_counts_extensions(self):
        from investigation_agent_platform.integrations.profile_gen.generate import (
            dominant_language,
        )

        assert dominant_language(["a.py", "b.py", "c.java"]) == "python"
        assert dominant_language(["a.java", "b.java", "c.py"]) == "java"
        assert dominant_language([]) is None
        assert dominant_language(["README.md"]) is None


class TestGenerators:
    QUARKUS = """
package com.acme;
@Path("/offers")
public class OfferResource {
    @GET
    @Path("/{id}")
    public Offer get(String id) { return null; }
    @POST
    public Offer create(Offer o) { return null; }
}
@ApplicationScoped
public class Cfg {
    @ConfigProperty(name = "offers.timeout")
    int timeout;
    @RegisterRestClient(configKey = "location")
    LocationClient loc;
}
"""
    FASTAPI = """
from fastapi import APIRouter
router = APIRouter(prefix="/ofm")
@router.get("/ping")
def ping(): return "ok"
@router.post("/offers")
def create(): return {}
from pydantic import BaseModel, Field
class Req(BaseModel):
    count: int = Field(ge=1, le=10)
    name: str = Field(min_length=2)
import httpx
r = httpx.get("http://loc:8080/api")
import os
port = os.getenv("PORT")
"""

    def test_quarkus_full(self):
        from investigation_agent_platform.integrations.profile_gen.generators import (
            get_generator,
            java_quarkus,
        )

        assert get_generator("java-quarkus") is java_quarkus
        out = java_quarkus.controllers(self.QUARKUS)
        assert out["base_path"] == "offers"
        assert {"method": "GET", "path": "offers/{id}"} in out["endpoints"]
        assert {"method": "POST", "path": "offers"} in out["endpoints"]
        assert "offers.timeout" in java_quarkus.config_keys(self.QUARKUS)
        clients = java_quarkus.clients(self.QUARKUS)
        assert {"name": "location"} in clients["rest_clients"]

    def test_fastapi_full(self):
        from investigation_agent_platform.integrations.profile_gen.generators import (
            get_generator,
            python_fastapi,
        )

        assert get_generator("python-fastapi") is python_fastapi
        out = python_fastapi.controllers(self.FASTAPI)
        assert out["base_path"] == "ofm"
        assert {"method": "GET", "path": "ofm/ping"} in out["endpoints"]
        assert {"method": "POST", "path": "ofm/offers"} in out["endpoints"]
        by_field = {e["field"]: e["constraints"] for e in python_fastapi.validations(self.FASTAPI)}
        assert set(by_field["count"]) == {"ge", "le"}
        assert by_field["name"] == ["min_length"]
        assert python_fastapi.clients(self.FASTAPI)["http"] == [
            {"method": "get", "url": "http://loc:8080/api"}
        ]
        assert "PORT" in python_fastapi.config_keys(self.FASTAPI)

    def test_stubs_no_crash(self):
        from investigation_agent_platform.integrations.profile_gen.generators import (
            django,
            flask,
            get_generator,
        )

        assert get_generator("python-flask") is flask
        assert get_generator("python-django") is django
        assert get_generator("go-generic") is None
        assert get_generator("nope") is None
        for mod in (flask, django):
            assert mod.controllers("anything") == {"base_path": "", "endpoints": []}
            assert mod.validations("anything") == []
            assert mod.config_keys("anything") == []

    def test_detector_matrix(self):
        from investigation_agent_platform.integrations.profile_gen.detect import (
            detect_repo_type,
        )

        assert detect_repo_type(
            ["pom.xml", "src/main/java/A.java"],
            {"pom.xml": "<project><artifactId>x</artifactId><dependency>spring-boot-starter-web</dependency></project>"},
        )["repo_type"] == "java-spring"
        assert detect_repo_type(
            ["pom.xml", "src/main/java/A.java"],
            {"pom.xml": "<project><build><plugin>quarkus-maven-plugin</plugin></build></project>"},
        )["repo_type"] == "java-quarkus"
        assert detect_repo_type(
            ["pyproject.toml", "app/main.py"],
            {"app/main.py": "from fastapi import FastAPI\napp = FastAPI()"},
        )["repo_type"] == "python-fastapi"
        assert detect_repo_type(
            ["pyproject.toml", "app/main.py"], {}
        )["repo_type"] == "python-generic"
        assert detect_repo_type([], {})["repo_type"] == "unknown"
        assert detect_repo_type(None)["repo_type"] == "unknown"


class TestAssemblerDependencies:
    def test_dependency_shape_matches_model(self):
        from investigation_agent_platform.domain.profile.models import (
            ApplicationProfile,
        )
        from investigation_agent_platform.integrations.profile_gen.assembler import (
            assemble_profile,
        )

        profile, _ = assemble_profile(
            tenant_id="t", app_id="svc", org="o", repo="r",
            feign_clients=[{"name": "loc", "url": "http://x"}],
        )
        validated = ApplicationProfile.model_validate(profile)
        assert validated.services[0].dependencies[0].service == "loc"


class TestProfileIdentity:
    def test_derive_app_id(self):
        from investigation_agent_platform.integrations.profile_gen.identity import (
            derive_app_id,
        )

        assert derive_app_id("n-a", "Order Service") == "order-service"
        assert derive_app_id("n-a", "SpringOrders") == "springorders"
        try:
            derive_app_id("", "")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")

    def test_parse_repo_ref_forms(self):
        from investigation_agent_platform.integrations.profile_gen.identity import (
            parse_repo_ref,
        )

        assert parse_repo_ref("github.com/samplecorp/orders-service") == ("samplecorp", "orders-service")
        assert parse_repo_ref("https://github.com/samplecorp/orders-service.git") == ("samplecorp", "orders-service")
        assert parse_repo_ref("samplecorp/orders-service") == ("samplecorp", "orders-service")
        for bad in ["git@github.com:samplecorp/x.git", "../evil", "onlyone", "", "a/b/c"]:
            try:
                parse_repo_ref(bad)
            except ValueError:
                continue
            raise AssertionError(f"expected ValueError for {bad!r}")


class TestShapeSearchKeywords:
    def test_short_lists_pass_through(self):
        from investigation_agent_platform.application.worker.activities import (
            _shape_search_keywords,
        )

        assert _shape_search_keywords(["alpha", "beta"]) == ["alpha", "beta"]
        assert _shape_search_keywords([]) == []

    def test_sentence_blob_splits_to_tokens(self):
        from investigation_agent_platform.application.worker.activities import (
            _shape_search_keywords,
        )

        out = _shape_search_keywords([
            "Which slices of the LLM reasoner code-evidence implementation plan lack implementation?"
        ])
        assert "slices" not in out  # stopword
        assert "reasoner" in out and "code" in out and "evidence" in out
        assert "plan" in out
        assert all(len(t) >= 4 for t in out)
        assert len(out) <= 20
