"""Phase 4 data-flow fixtures: reaching definitions, returns, abstentions."""

from datetime import UTC, datetime
from uuid import uuid4

from investigation_agent_platform.application.analysis.java.dataflow import (
    resolve_java_data_flow,
)
from investigation_agent_platform.domain.code.models import CodeFactStatus, JavaFactCategory
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)


def _source_evidence(source: str, *, start: int = 10, path: str = "src/Runner.java"):
    investigation_id = uuid4()
    evidence_id = uuid4()
    now = datetime.now(UTC)
    lines = "\n".join(f"{start + index}| {line}" for index, line in enumerate(source.splitlines()))
    end = start + max(0, len(source.splitlines()) - 1)
    return Evidence(
        evidence_id=evidence_id,
        tenant_id="tenant-a",
        investigation_id=investigation_id,
        evidence_type=EvidenceType.SOURCE_CODE,
        provider="pygit2-local",
        source=f"git://repo/{path}@abc123",
        title="Java source range",
        summary="Pinned Java source",
        content_snippet=lines,
        attributes={"read_mode": "range"},
        observed_at=now,
        retrieved_at=now,
        provenance=EvidenceProvenance(
            tenant_id="tenant-a",
            investigation_id=investigation_id,
            provider_type="GIT",
            requested_provider_id="pygit2-local",
            actual_provider_id="pygit2-local",
            source_system="Git",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="GIT", operation="GET_SOURCE", normalized_query_hash="hash"
            ),
            source_location=SourceLocation(
                system="Git",
                identifier=f"repo/{path}",
                file_path=path,
                line_start=start,
                line_end=end,
                revision="abc123",
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=str(evidence_id),
    )


def test_reaching_definitions_resolve_with_exact_ranges():
    evidence = _source_evidence(
        "class Runner {\n  int run() {\n    int x = 1;\n    x = 2;\n    return x;\n  }\n}"
    )
    result = resolve_java_data_flow(evidence, "x")
    assert result.status == CodeFactStatus.RESOLVED
    assert result.fact_category == JavaFactCategory.DATA_FLOW_FACT
    fact = result.structured_fact
    assert fact["definite_assignment"] == "established"
    assert len(fact["definitions"]) == 2
    assert len(fact["uses"]) >= 1
    assert result.data_flow_refs
    for ref in result.data_flow_refs:
        assert "def(" in ref and "->use(" in ref
        assert "src/Runner.java" in ref
        assert "abc123" in ref


def test_early_returns_enumerated():
    evidence = _source_evidence(
        "class Runner {\n"
        "  int run(int flag) {\n"
        "    int x = 1;\n"
        "    if (flag > 0) {\n"
        "      return x;\n"
        "    }\n"
        "    x = 2;\n"
        "    return x;\n"
        "  }\n"
        "}"
    )
    result = resolve_java_data_flow(evidence, "x")
    assert result.status == CodeFactStatus.RESOLVED
    assert len(result.structured_fact["early_returns"]) == 2
    assert result.structured_fact["definitions"]


def test_use_before_definition_is_partial():
    evidence = _source_evidence(
        "class Runner {\n  int run() {\n    int y = x + 1;\n    int x = 1;\n    return y;\n  }\n}"
    )
    result = resolve_java_data_flow(evidence, "x")
    assert result.status == CodeFactStatus.PARTIAL
    assert "use_before_definition" in result.limitation_codes


def test_out_of_window_reference_abstains():
    evidence = _source_evidence(
        "class Runner {\n"
        "  void run() {\n"
        "    int x = 1;\n"
        "  }\n"
        "  void walk() {\n"
        "    int y = x + 1;\n"
        "  }\n"
        "}"
    )
    result = resolve_java_data_flow(evidence, "x")
    assert result.status in {CodeFactStatus.PARTIAL, CodeFactStatus.UNRESOLVED}
    assert "out_of_window_reference" in result.limitation_codes


def test_aliasing_through_call_arg_is_partial():
    evidence = _source_evidence(
        "class Runner {\n"
        "  void sink(int v) {}\n"
        "  void run() {\n"
        "    int x = 1;\n"
        "    sink(x);\n"
        "  }\n"
        "}"
    )
    result = resolve_java_data_flow(evidence, "x")
    assert result.status == CodeFactStatus.PARTIAL
    assert "interprocedural_aliasing" in result.limitation_codes


def test_stream_laziness_is_partial():
    evidence = _source_evidence(
        "import java.util.List;\n"
        "class Runner {\n"
        "  void run(List<String> items) {\n"
        "    int x = items.stream().map(String::length).reduce(0, Integer::sum);\n"
        "  }\n"
        "}"
    )
    result = resolve_java_data_flow(evidence, "x")
    assert result.status == CodeFactStatus.PARTIAL
    assert "stream_laziness" in result.limitation_codes


def test_concurrent_visibility_is_partial():
    evidence = _source_evidence(
        "class Runner {\n"
        "  void run() {\n"
        "    int x = 1;\n"
        "    synchronized (this) {\n"
        "      x = 2;\n"
        "    }\n"
        "  }\n"
        "}"
    )
    result = resolve_java_data_flow(evidence, "x")
    assert result.status == CodeFactStatus.PARTIAL
    assert "concurrent_visibility" in result.limitation_codes


def test_missing_symbol_is_unresolved():
    evidence = _source_evidence("class Runner {\n  void run() {\n    int x = 1;\n  }\n}")
    result = resolve_java_data_flow(evidence, "absent")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "local_not_found" in result.limitation_codes


def test_model_dataflow_discovery_matches_legacy_reference_cases():
    cases = [
        (
            "class Runner { int run() { int x = 1; x = 2; return x; } }",
            "x",
            {},
        ),
        (
            "class Runner { int run(int flag) { int x = 1; if (flag > 0) { return x; } x = 2; return x; } }",
            "x",
            {},
        ),
        (
            "class Runner { int run() { int y = x + 1; int x = 1; return y; } }",
            "x",
            {},
        ),
        (
            "class Runner { void sink(int v) {} void run() { int x = 1; sink(x); } }",
            "x",
            {},
        ),
        (
            "class Runner { void run() { int x = 1; synchronized (this) { x = 2; } } }",
            "x",
            {},
        ),
        (
            "import java.util.List; class Runner { void run(List<String> items) { int x = items.stream().map(String::length).reduce(0, Integer::sum); } }",
            "x",
            {},
        ),
        (
            "class Runner { void run() { int x = 1; } void walk() { int y = x + 1; } }",
            "x",
            {},
        ),
        ("class Runner { void run() { int x = 1; } }", "absent", {}),
    ]
    for source, symbol, kwargs in cases:
        evidence = _source_evidence(source)
        legacy = __import__(
            "investigation_agent_platform.application.analysis.java.dataflow",
            fromlist=["_resolve_java_data_flow_legacy"],
        )._resolve_java_data_flow_legacy(evidence, symbol, **kwargs)
        model = resolve_java_data_flow(evidence, symbol, **kwargs)
        assert (
            model.status,
            tuple(sorted(model.limitation_codes)),
            model.structured_fact,
            model.subject_identity,
            model.declaration_location,
            model.data_flow_refs,
        ) == (
            legacy.status,
            tuple(sorted(legacy.limitation_codes)),
            legacy.structured_fact,
            legacy.subject_identity,
            legacy.declaration_location,
            legacy.data_flow_refs,
        )


def test_nested_call_argument_does_not_pollute_barrier_subjects():
    from investigation_agent_platform.application.analysis.java import model, query

    code = model.build_java_model(
        b"class Runner { void run() { int x = 1; sink(foo(x)); } int foo(int x) { return x; } void sink(int x) {} }",
        file_path="src/Runner.java",
        repository="repo",
        revision="abc123",
    )
    subjects = [barrier.subject for barrier in query.all_barriers(code)]
    assert "foo" not in subjects


def test_model_dataflow_records_are_queryable():
    from investigation_agent_platform.application.analysis.java import model, query

    source = "class Runner { int run() { int x = 1; x += 2; return x; } }"
    evidence = _source_evidence(source)
    lines, _ = __import__(
        "investigation_agent_platform.application.analysis.java._provenance",
        fromlist=["java_source_lines"],
    ).java_source_lines(evidence)
    code = model.build_java_model(
        "\n".join(line.text for line in lines).encode(),
        file_path="src/Runner.java",
        repository="repo",
        revision="abc123",
    )
    assert list(query.all_dataflow_bindings(code))
    assert any(event.kind == "compound_assignment" for event in query.all_dataflow_events(code))


def test_same_name_sibling_binding_is_not_out_of_window():
    evidence = _source_evidence(
        "class Runner {\n  void run() { int x = 1; }\n  void walk() { int x = 2; return; }\n}"
    )
    result = resolve_java_data_flow(evidence, "x")
    assert result.status == CodeFactStatus.RESOLVED


def test_nested_method_scope_does_not_pollute_outer_events():
    evidence = _source_evidence(
        "class Runner {\n"
        "  void run() { int x = 1; return; }\n"
        "  void walk() { int y = 2; return; }\n"
        "}"
    )
    result = resolve_java_data_flow(evidence, "x")
    assert result.status == CodeFactStatus.RESOLVED
    assert all(
        use["kind"] != "value_use" or use["line_start"] <= 12
        for use in result.structured_fact["uses"]
    )


def test_anonymous_class_binding_is_recorded_once_and_resolves():
    from investigation_agent_platform.application.analysis.java import model

    source = (
        "class Outer { void run() { int x = 1; Runnable r = new Runnable() { "
        "public void run() { int q = x; }; }; } }"
    )
    code = model.build_java_model(
        source.encode(),
        file_path="src/Outer.java",
        repository="repo",
        revision="abc123",
    )
    matches = [b for b in code.dataflow_bindings if b.symbol == "q"]
    assert len(matches) == 1
    evidence = _source_evidence(source)
    result = resolve_java_data_flow(evidence, "q")
    assert result.status == CodeFactStatus.RESOLVED


def test_effective_value_and_barrier_records_are_conservative():
    from investigation_agent_platform.application.analysis.java import model, query

    source = """class Runner {
        int run() {
            int x = 1;
            if (x > 0) { x = 2; }
            sink(x);
            return x;
        }
        void sink(int value) {}
    }"""
    evidence = _source_evidence(source)
    lines, _ = __import__(
        "investigation_agent_platform.application.analysis.java._provenance",
        fromlist=["java_source_lines"],
    ).java_source_lines(evidence)
    code = model.build_java_model(
        "\n".join(line.text for line in lines).encode(),
        file_path="src/Runner.java",
        repository="repo",
        revision="abc123",
    )
    states = list(query.all_value_states(code))
    barriers = list(query.all_barriers(code))
    assert any(query.value_is_constant(state) for state in states)
    assert any(barrier.kind == "GUARD" for barrier in barriers)
    assert any(barrier.kind == "REASSIGNMENT_KILL" for barrier in barriers)
    assert any(barrier.kind == "ALIAS_ESCAPE" for barrier in barriers)
    assert query.flow_is_blocked(states[0], barriers) in {
        "BLOCKED",
        "UNKNOWN",
        "NOT_BLOCKED_IN_WINDOW",
    }


def test_effective_constant_call_filter_requires_bare_name_and_explicit_validator_vocabulary():
    from investigation_agent_platform.application.analysis.java import model, query

    source = """class Runner {
        void run() {
            String queryText = "select";
            sink(queryText);
            validateNowhere(queryText);
        }
        void sink(String value) {}
        void validateNowhere(String value) {}
    }"""
    evidence = _source_evidence(source)
    lines, _ = __import__(
        "investigation_agent_platform.application.analysis.java._provenance",
        fromlist=["java_source_lines"],
    ).java_source_lines(evidence)
    code = model.build_java_model(
        "\n".join(line.text for line in lines).encode(),
        file_path="src/Runner.java",
        repository="repo",
        revision="abc123",
    )
    calls = list(query.all_calls(code))
    states = list(query.all_value_states(code))
    assert [
        call.target_name for call in query.calls_with_effective_constant_arg(calls, states, 0)
    ] == [
        "sink",
        "validateNowhere",
    ]
    assert [
        call.target_name for call in query.calls_with_validation_barrier(calls, ["validate"])
    ] == []
    assert [
        call.target_name for call in query.calls_with_validation_barrier(calls, ["validateNowhere"])
    ] == ["validateNowhere"]


def test_effective_constant_call_filter_isolated_by_overloaded_method_identity():
    from investigation_agent_platform.application.analysis.java import model, query

    source = """class Runner {
        void run() {
            String queryText = "select";
            sink(queryText);
        }
        void run(int ignored) {
            String queryText = "other";
            sink(queryText);
        }
        void sink(String value) {}
    }"""
    evidence = _source_evidence(source)
    lines, _ = __import__(
        "investigation_agent_platform.application.analysis.java._provenance",
        fromlist=["java_source_lines"],
    ).java_source_lines(evidence)
    code = model.build_java_model(
        "\n".join(line.text for line in lines).encode(),
        file_path="src/Runner.java",
        repository="repo",
        revision="abc123",
    )
    calls = list(query.all_calls(code))
    states = list(query.all_value_states(code))
    assert len(list(query.calls_with_effective_constant_arg(calls, states, 0))) == 2
    assert {call.enclosing_method_identity for call in calls if call.target_name == "sink"} == {
        "Runner.run#run/0()",
        "Runner.run#run/1(int)",
    }
