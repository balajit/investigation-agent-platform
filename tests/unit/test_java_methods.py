"""Phase 4 call-target fixtures: overloads, overrides, and abstentions."""

from datetime import UTC, datetime
from uuid import uuid4

from investigation_agent_platform.application.analysis.java.method_facts import (
    resolve_java_call_target,
)
from investigation_agent_platform.domain.code.models import CodeFactStatus, JavaFactCategory
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)


def _source_evidence(source: str, *, start: int = 10, path: str = "src/Widget.java"):
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


def _no_run_wording(result) -> None:
    payload = result.model_dump_json().lower()
    for banned in ("will run", "does run", "runs when", "actually ran", "executes", "executed"):
        assert banned not in payload, banned


def test_widening_selects_most_specific_target():
    evidence = _source_evidence(
        "class Widget {\n"
        "  void process(int v) {}\n"
        "  void process(long v) {}\n"
        "  void run() {\n"
        "    process(5);\n"
        "  }\n"
        "}"
    )
    result = resolve_java_call_target(evidence, "process")
    assert result.status == CodeFactStatus.RESOLVED
    assert result.fact_category == JavaFactCategory.CALL_TARGET_FACT
    assert result.call_target == "Widget.process(int)"
    assert result.structured_fact["resolution_phase"] == "strict"
    _no_run_wording(result)


def test_widening_wrong_target_counterexample():
    evidence = _source_evidence(
        "class Widget {\n"
        "  void process(int v) {}\n"
        "  void process(long v) {}\n"
        "  void run() {\n"
        "    process(5);\n"
        "  }\n"
        "}"
    )
    result = resolve_java_call_target(evidence, "process")
    assert result.call_target != "Widget.process(long)"


def test_boxing_loses_to_widening_phase():
    evidence = _source_evidence(
        "class Widget {\n"
        "  void handle(Integer v) {}\n"
        "  void handle(long v) {}\n"
        "  void run() {\n"
        "    handle(5);\n"
        "  }\n"
        "}"
    )
    result = resolve_java_call_target(evidence, "handle")
    assert result.status == CodeFactStatus.RESOLVED
    assert result.call_target == "Widget.handle(long)"
    assert result.structured_fact["resolution_phase"] == "strict"
    _no_run_wording(result)


def test_boxing_wrong_target_counterexample():
    evidence = _source_evidence(
        "class Widget {\n"
        "  void handle(Integer v) {}\n"
        "  void handle(long v) {}\n"
        "  void run() {\n"
        "    handle(5);\n"
        "  }\n"
        "}"
    )
    result = resolve_java_call_target(evidence, "handle")
    assert result.call_target != "Widget.handle(Integer)"


def test_varargs_resolves_multi_argument_call():
    evidence = _source_evidence(
        "class Widget {\n"
        "  void join(String sep) {}\n"
        "  void join(String... parts) {}\n"
        "  void run() {\n"
        "    join(\"a\", \"b\");\n"
        "  }\n"
        "}"
    )
    result = resolve_java_call_target(evidence, "join")
    assert result.status == CodeFactStatus.RESOLVED
    assert result.call_target == "Widget.join(String)"
    assert result.structured_fact["resolution_phase"] == "varargs"
    _no_run_wording(result)


def test_varargs_fixed_arity_wins_single_argument_call():
    evidence = _source_evidence(
        "class Widget {\n"
        "  void join(String sep) {}\n"
        "  void join(String... parts) {}\n"
        "  void run() {\n"
        "    join(\"a\");\n"
        "  }\n"
        "}"
    )
    result = resolve_java_call_target(evidence, "join")
    assert result.status == CodeFactStatus.RESOLVED
    assert result.call_target == "Widget.join(String)"
    assert result.structured_fact["resolution_phase"] != "varargs"


def test_ambiguous_overload_abstains():
    evidence = _source_evidence(
        "class Widget {\n"
        "  void mix(int a, long b) {}\n"
        "  void mix(long a, int b) {}\n"
        "  void run() {\n"
        "    mix(1, 2);\n"
        "  }\n"
        "}"
    )
    result = resolve_java_call_target(evidence, "mix")
    assert result.status == CodeFactStatus.PARTIAL
    assert "ambiguous_overload" in result.limitation_codes
    assert result.call_target is None


def test_override_dispatch_abstains():
    evidence = _source_evidence(
        "class Base {\n"
        "  void run() {}\n"
        "}\n"
        "class Child extends Base {\n"
        "  @Override\n"
        "  void run() {}\n"
        "  void go() {\n"
        "    run();\n"
        "  }\n"
        "}"
    )
    result = resolve_java_call_target(evidence, "run")
    assert result.status in {CodeFactStatus.PARTIAL, CodeFactStatus.UNRESOLVED}
    assert "dynamic_dispatch" in result.limitation_codes
    assert result.call_target is None


def test_default_method_abstains():
    evidence = _source_evidence(
        "interface Greeter {\n"
        "  default void greet() {}\n"
        "}\n"
        "class Widget implements Greeter {\n"
        "  void run() {\n"
        "    greet();\n"
        "  }\n"
        "}"
    )
    result = resolve_java_call_target(evidence, "greet")
    assert result.status in {CodeFactStatus.PARTIAL, CodeFactStatus.UNRESOLVED}
    assert "dynamic_dispatch" in result.limitation_codes
    assert result.call_target is None


def test_reflection_abstains():
    evidence = _source_evidence(
        "class Widget {\n"
        "  void process(int v) {}\n"
        "  void run() throws Exception {\n"
        "    getClass().getMethod(\"process\", int.class).invoke(this, 5);\n"
        "  }\n"
        "}"
    )
    result = resolve_java_call_target(evidence, "process")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "reflection_boundary" in result.limitation_codes


def test_proxy_abstains():
    evidence = _source_evidence(
        "import java.lang.reflect.Proxy;\n"
        "class Widget {\n"
        "  void process(int v) {}\n"
        "  void run() {\n"
        "    Object p = Proxy.newProxyInstance(null, null, null);\n"
        "    process(1);\n"
        "  }\n"
        "}"
    )
    result = resolve_java_call_target(evidence, "process")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "reflection_boundary" in result.limitation_codes


def test_lambda_escape_abstains():
    evidence = _source_evidence(
        "class Widget {\n"
        "  void process(int v) {}\n"
        "  void run() {\n"
        "    Runnable r = () -> process(1);\n"
        "    r.run();\n"
        "  }\n"
        "}"
    )
    result = resolve_java_call_target(evidence, "process")
    assert result.status in {CodeFactStatus.PARTIAL, CodeFactStatus.UNRESOLVED}
    assert "dynamic_dispatch" in result.limitation_codes


def test_unknown_receiver_type_abstains():
    evidence = _source_evidence(
        "class Widget {\n"
        "  void process(int v) {}\n"
        "  void process(String v) {}\n"
        "  void run(Object box) {\n"
        "    process(box);\n"
        "  }\n"
        "}"
    )
    result = resolve_java_call_target(evidence, "process")
    assert result.status in {CodeFactStatus.PARTIAL, CodeFactStatus.UNRESOLVED}
    assert "incomplete_candidate_set" in result.limitation_codes
    assert result.call_target is None


def test_cross_module_scope_abstains():
    evidence = _source_evidence(
        "class Widget {\n"
        "  void process(int v) {}\n"
        "  void run() {\n"
        "    process(5);\n"
        "  }\n"
        "}"
    )
    result = resolve_java_call_target(evidence, "process", declaring_type="com.external.Lib")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "cross_module_scope" in result.limitation_codes


def test_missing_call_site_abstains():
    evidence = _source_evidence("class Widget {\n  void process(int v) {}\n}")
    result = resolve_java_call_target(evidence, "process")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "incomplete_candidate_set" in result.limitation_codes
