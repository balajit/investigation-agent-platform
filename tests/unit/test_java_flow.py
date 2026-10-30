"""Phase 5 control-flow/possible-effect plus Phase 6 type-feature fixtures.

Exercises the existing ``flow_facts`` module (untouched) and the new
``type_facts`` module: possible-path language, execution-wording fail-closed
scans, concurrency abstention, and in-subset vs abstain cases for generics,
lambdas, records, sealed types, patterns, and switch expressions.
"""

import re
from datetime import UTC, datetime
from uuid import uuid4

from investigation_agent_platform.application.analysis.java.flow_facts import (
    resolve_java_control_flow,
    resolve_java_possible_effect,
)
from investigation_agent_platform.application.analysis.java.type_facts import (
    resolve_java_type_fact,
)
from investigation_agent_platform.application.investigation.verification import (
    category_proof_rule,
)
from investigation_agent_platform.domain.code.models import CodeFactStatus, JavaFactCategory
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)

BANNED_WORDING = (
    "executes",
    "executed",
    "will run",
    "does run",
    "runs when",
    "actually ran",
    "was executed",
    "observed execution",
)

REF_RE = re.compile(r"^src/Widget\.java:\d+-\d+$")


def _source_evidence(source, *, start=10, path="src/Widget.java", revision="abc123"):
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
        source=f"git://repo/{path}@{revision}",
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
                revision=revision,
            ),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
        fingerprint=str(evidence_id),
    )


def _no_execution_wording(result) -> None:
    payload = result.model_dump_json().lower()
    for banned in BANNED_WORDING:
        assert banned not in payload, banned


def _stable_refs(result) -> None:
    assert result.control_flow_refs
    for ref in result.control_flow_refs:
        assert REF_RE.match(ref), ref


# --- Phase 5: control flow ---


def test_if_else_reports_possible_branch_paths():
    evidence = _source_evidence(
        "class Widget {\n"
        "  int decide(int x) {\n"
        "    if (x > 0) {\n"
        "      return 1;\n"
        "    } else {\n"
        "      return -1;\n"
        "    }\n"
        "  }\n"
        "}"
    )
    result = resolve_java_control_flow(evidence, "decide")
    assert result.status == CodeFactStatus.RESOLVED
    assert result.fact_category == JavaFactCategory.CONTROL_FLOW_FACT
    kinds = [p["kind"] for p in result.structured_fact["paths"]]
    assert kinds.count("possible_branch_path") == 2
    assert "possible_return_path" in kinds
    _stable_refs(result)
    _no_execution_wording(result)


def test_catch_existence_never_claims_execution():
    evidence = _source_evidence(
        "class Widget {\n"
        "  void load() {\n"
        "    try {\n"
        "      read();\n"
        "    } catch (java.io.IOException e) {\n"
        "      fallback();\n"
        "    } finally {\n"
        "      close();\n"
        "    }\n"
        "  }\n"
        "}"
    )
    result = resolve_java_control_flow(evidence, "load")
    assert result.status == CodeFactStatus.RESOLVED
    kinds = [p["kind"] for p in result.structured_fact["paths"]]
    assert "possible_catch_path" in kinds
    assert "possible_cleanup_path" in kinds
    catch = next(p for p in result.structured_fact["paths"] if p["kind"] == "possible_catch_path")
    assert "does not establish execution" in catch["note"]
    _no_execution_wording(result)


def test_finally_is_possible_cleanup_not_outcome():
    evidence = _source_evidence(
        "class Widget {\n"
        "  void load() {\n"
        "    try {\n"
        "      read();\n"
        "    } finally {\n"
        "      close();\n"
        "    }\n"
        "  }\n"
        "}"
    )
    result = resolve_java_control_flow(evidence, "load")
    cleanup = next(
        p for p in result.structured_fact["paths"] if p["kind"] == "possible_cleanup_path"
    )
    assert "may run as cleanup" in cleanup["detail"]
    assert "outcome is not proven" in cleanup["note"]


def test_try_with_resources_close_is_possible_not_observed():
    evidence = _source_evidence(
        "class Widget {\n"
        "  void load() throws Exception {\n"
        "    try (java.io.InputStream in = open()) {\n"
        "      read(in);\n"
        "    }\n"
        "  }\n"
        "}"
    )
    result = resolve_java_control_flow(evidence, "load")
    assert result.status == CodeFactStatus.RESOLVED
    assert any(p["kind"] == "possible_cleanup_path" for p in result.structured_fact["paths"])
    _no_execution_wording(result)


def test_throw_wrapping_named_not_proven():
    evidence = _source_evidence(
        "class Widget {\n"
        "  void load() {\n"
        "    try {\n"
        "      read();\n"
        "    } catch (java.io.IOException e) {\n"
        "      throw new IllegalStateException(e);\n"
        "    }\n"
        "  }\n"
        "}"
    )
    result = resolve_java_possible_effect(evidence, "load")
    assert result.status == CodeFactStatus.RESOLVED
    assert result.fact_category == JavaFactCategory.POSSIBLE_EFFECT_FACT
    thrown = next(e for e in result.structured_fact["effects"] if e["kind"] == "possible_exception")
    assert "named, not proven" in thrown["note"]
    _no_execution_wording(result)


def test_mutation_and_side_effect_are_possible_only():
    evidence = _source_evidence(
        "class Widget {\n"
        "  int count;\n"
        "  void bump() {\n"
        "    count = count + 1;\n"
        "    notifyListeners();\n"
        "  }\n"
        "}"
    )
    result = resolve_java_possible_effect(evidence, "bump")
    assert result.status == CodeFactStatus.RESOLVED
    kinds = [e["kind"] for e in result.structured_fact["effects"]]
    assert "possible_mutation" in kinds
    assert "possible_side_effect" in kinds
    _no_execution_wording(result)


def test_branch_hint_filters_and_miss_abstains():
    evidence = _source_evidence(
        "class Widget {\n"
        "  void load() {\n"
        "    try {\n"
        "      read();\n"
        "    } catch (java.io.IOException e) {\n"
        "      fallback();\n"
        "    }\n"
        "  }\n"
        "}"
    )
    filtered = resolve_java_control_flow(evidence, "load", branch_hint="catch")
    assert filtered.status == CodeFactStatus.RESOLVED
    assert all("catch" in p["kind"] for p in filtered.structured_fact["paths"])
    missed = resolve_java_control_flow(evidence, "load", branch_hint="lambda")
    assert missed.status == CodeFactStatus.UNRESOLVED
    assert "branch_hint_not_found" in missed.limitation_codes


def test_synchronized_yields_partial_nondeterministic():
    evidence = _source_evidence(
        "class Widget {\n"
        "  int count;\n"
        "  void bump() {\n"
        "    synchronized (this) {\n"
        "      count = count + 1;\n"
        "    }\n"
        "  }\n"
        "}"
    )
    result = resolve_java_control_flow(evidence, "bump")
    assert result.status == CodeFactStatus.PARTIAL
    assert "nondeterministic_scheduling" in result.limitation_codes
    assert "requires_runtime_observation" in result.limitation_codes


def test_completable_future_yields_partial_async_boundary():
    evidence = _source_evidence(
        "class Widget {\n"
        "  java.util.concurrent.CompletableFuture<String> load() {\n"
        "    return java.util.concurrent.CompletableFuture.supplyAsync(() -> fetch());\n"
        "  }\n"
        "}"
    )
    result = resolve_java_control_flow(evidence, "load")
    assert result.status == CodeFactStatus.PARTIAL
    assert "async_boundary" in result.limitation_codes
    assert "requires_runtime_observation" in result.limitation_codes


def test_reactive_chain_yields_partial_async_boundary():
    evidence = _source_evidence(
        "class Widget {\n"
        "  reactor.core.publisher.Mono<String> load() {\n"
        "    return fetchMono().map(String::trim);\n"
        "  }\n"
        "}"
    )
    result = resolve_java_possible_effect(evidence, "load")
    assert result.status == CodeFactStatus.PARTIAL
    assert "async_boundary" in result.limitation_codes


def test_control_flow_provenance_fail_closed():
    evidence = _source_evidence("class Widget {\n  void run() {}\n}", revision="HEAD")
    result = resolve_java_control_flow(evidence, "run")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "missing_immutable_source_provenance" in result.limitation_codes
    non_java = _source_evidence("class Widget {\n  void run() {}\n}", path="src/app.py")
    result = resolve_java_control_flow(non_java, "run")
    assert "not_a_java_source_file" in result.limitation_codes


def test_new_categories_flow_through_proof_table_read_only():
    for category in (
        JavaFactCategory.CONTROL_FLOW_FACT,
        JavaFactCategory.POSSIBLE_EFFECT_FACT,
        JavaFactCategory.CONFIGURATION_FACT,
    ):
        rule = category_proof_rule(category)
        assert rule is not None
        assert rule.max_verdict == "LIKELY"
        assert rule.source_only_verifiable is True
    semantic_rule = category_proof_rule(JavaFactCategory.LANGUAGE_SEMANTIC_FACT)
    assert semantic_rule is not None
    assert semantic_rule.max_verdict == "VERIFIED"


# --- Phase 6: types and advanced features ---


def test_record_resolves_with_known_level():
    evidence = _source_evidence("record Point(int x, int y) {}", start=10)
    result = resolve_java_type_fact(evidence, "Point", language_version=17)
    assert result.status == CodeFactStatus.RESOLVED
    assert result.fact_category == JavaFactCategory.LANGUAGE_SEMANTIC_FACT
    record = next(
        f for f in result.structured_fact["features"] if f["kind"] == "record_declaration"
    )
    assert "'x'" in record["detail"] and "'int'" in record["detail"]
    _no_execution_wording(result)


def test_record_unknown_level_abstains():
    evidence = _source_evidence("record Point(int x, int y) {}", start=10)
    result = resolve_java_type_fact(evidence, "Point")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "unsupported_language_version" in result.limitation_codes


def test_record_below_gate_level_abstains():
    evidence = _source_evidence("record Point(int x, int y) {}", start=10)
    result = resolve_java_type_fact(evidence, "Point", language_version=11)
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "unsupported_language_version" in result.limitation_codes


def test_sealed_permits_in_window_resolves():
    evidence = _source_evidence(
        "sealed interface Shape permits Circle, Square {}\n"
        "final class Circle implements Shape {}\n"
        "final class Square implements Shape {}"
    )
    result = resolve_java_type_fact(evidence, "Shape", language_version=17)
    assert result.status == CodeFactStatus.RESOLVED
    sealed = next(f for f in result.structured_fact["features"] if f["kind"] == "sealed_hierarchy")
    assert "Circle" in sealed["detail"] and "Square" in sealed["detail"]
    _no_execution_wording(result)


def test_sealed_permits_outside_window_abstains():
    evidence = _source_evidence(
        "sealed interface Shape permits Circle, Remote {}\nfinal class Circle implements Shape {}"
    )
    result = resolve_java_type_fact(evidence, "Shape", language_version=17)
    assert result.status == CodeFactStatus.PARTIAL
    assert "branch_target_outside_window" in result.limitation_codes


def test_generic_declaration_resolves_erasure_insensitive():
    evidence = _source_evidence("class Box<T> {\n  T value;\n  T get() { return value; }\n}")
    result = resolve_java_type_fact(evidence, "Box", language_version=11)
    assert result.status == CodeFactStatus.RESOLVED
    generic = next(
        f for f in result.structured_fact["features"] if f["kind"] == "generic_declaration"
    )
    assert "'T'" in generic["detail"] or '"T"' in generic["detail"] or "T" in generic["detail"]
    _no_execution_wording(result)


def test_cast_to_parameterized_type_abstains_erasure():
    evidence = _source_evidence(
        "class Widget {\n"
        "  void run(Object o) {\n"
        "    java.util.List<String> names = (java.util.List<String>) o;\n"
        "  }\n"
        "}"
    )
    result = resolve_java_type_fact(evidence, "run", language_version=17)
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "type_erasure_changes_answer" in result.limitation_codes


def test_lambda_in_window_captures_resolve():
    evidence = _source_evidence(
        "class Widget {\n"
        "  void run(String prefix) {\n"
        '    String suffix = "!";\n'
        "    Runnable task = () -> System.out.println(prefix + suffix);\n"
        "    task.run();\n"
        "  }\n"
        "}"
    )
    result = resolve_java_type_fact(evidence, "run", language_version=17)
    assert result.status == CodeFactStatus.RESOLVED
    shape = next(f for f in result.structured_fact["features"] if f["kind"] == "lambda_shape")
    assert "prefix" in shape["detail"] and "suffix" in shape["detail"]
    _no_execution_wording(result)


def test_lambda_passed_as_argument_escapes_scope():
    evidence = _source_evidence(
        "class Widget {\n"
        "  void run(java.util.List<String> tasks) {\n"
        "    tasks.forEach(item -> process(item));\n"
        "  }\n"
        "  void process(String s) {}\n"
        "}"
    )
    result = resolve_java_type_fact(evidence, "run", language_version=17)
    assert result.status == CodeFactStatus.PARTIAL
    assert "lambda_escapes_scope" in result.limitation_codes


def test_lambda_unknown_capture_abstains():
    evidence = _source_evidence(
        "class Widget {\n"
        "  void run() {\n"
        "    Runnable task = () -> System.out.println(remote);\n"
        "    task.run();\n"
        "  }\n"
        "}"
    )
    result = resolve_java_type_fact(evidence, "run", language_version=17)
    assert result.status == CodeFactStatus.PARTIAL
    assert "lambda_capture_outside_window" in result.limitation_codes


def test_method_reference_to_in_window_target_resolves():
    evidence = _source_evidence(
        "class Widget {\n"
        "  void handle(String s) {}\n"
        "  void run(java.util.List<String> items) {\n"
        "    items.forEach(this::handle);\n"
        "  }\n"
        "}"
    )
    result = resolve_java_type_fact(evidence, "run", language_version=17)
    assert result.status == CodeFactStatus.RESOLVED
    shape = next(
        f for f in result.structured_fact["features"] if f["kind"] == "method_reference_shape"
    )
    assert "this::handle" in shape["detail"]
    _no_execution_wording(result)


def test_method_reference_outside_window_abstains():
    evidence = _source_evidence(
        "class Widget {\n"
        "  void run(java.util.List<String> items) {\n"
        "    items.forEach(Parser::parse);\n"
        "  }\n"
        "}"
    )
    result = resolve_java_type_fact(evidence, "run", language_version=17)
    assert result.status == CodeFactStatus.PARTIAL
    assert "method_reference_target_outside_window" in result.limitation_codes


def test_switch_expression_arms_resolve():
    evidence = _source_evidence(
        "class Widget {\n"
        "  String label(int g) {\n"
        "    return switch (g) {\n"
        '      case 1 -> "one";\n'
        '      default -> "other";\n'
        "    };\n"
        "  }\n"
        "}"
    )
    result = resolve_java_type_fact(evidence, "label", language_version=17)
    assert result.status == CodeFactStatus.RESOLVED
    switch = next(
        f for f in result.structured_fact["features"] if f["kind"] == "switch_expression_shape"
    )
    assert len(switch["arms"]) == 2
    _no_execution_wording(result)


def test_switch_expression_unknown_level_abstains():
    evidence = _source_evidence(
        "class Widget {\n"
        "  String label(int g) {\n"
        "    return switch (g) {\n"
        '      case 1 -> "one";\n'
        '      default -> "other";\n'
        "    };\n"
        "  }\n"
        "}"
    )
    result = resolve_java_type_fact(evidence, "label")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "unsupported_language_version" in result.limitation_codes


def test_pattern_target_outside_window_abstains():
    evidence = _source_evidence(
        "class Widget {\n"
        "  String describe(Object o) {\n"
        "    if (o instanceof Point p) {\n"
        '      return "point";\n'
        "    }\n"
        '    return "other";\n'
        "  }\n"
        "}"
    )
    result = resolve_java_type_fact(evidence, "describe", language_version=17)
    assert result.status == CodeFactStatus.PARTIAL
    assert "branch_target_outside_window" in result.limitation_codes


def test_pattern_target_in_window_resolves():
    evidence = _source_evidence(
        "record Point(int x, int y) {}\n"
        "class Widget {\n"
        "  String describe(Object o) {\n"
        "    if (o instanceof Point p) {\n"
        '      return "point";\n'
        "    }\n"
        '    return "other";\n'
        "  }\n"
        "}"
    )
    result = resolve_java_type_fact(evidence, "describe", language_version=17)
    assert result.status == CodeFactStatus.RESOLVED
    assert any(f["kind"] == "pattern_matching_shape" for f in result.structured_fact["features"])
    _no_execution_wording(result)


def test_compact_constructor_recorded_as_declared():
    evidence = _source_evidence(
        "record Range(int lo, int hi) {\n"
        "  Range {\n"
        "    if (lo > hi) { throw new IllegalArgumentException(); }\n"
        "  }\n"
        "}"
    )
    result = resolve_java_type_fact(evidence, "Range", language_version=17)
    assert result.status == CodeFactStatus.RESOLVED
    assert any(f["kind"] == "compact_constructor" for f in result.structured_fact["features"])
    _no_execution_wording(result)


def test_feature_hint_miss_abstains():
    evidence = _source_evidence("record Point(int x, int y) {}", start=10)
    result = resolve_java_type_fact(evidence, "Point", language_version=17, feature_hint="lambda")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "feature_hint_not_found" in result.limitation_codes
