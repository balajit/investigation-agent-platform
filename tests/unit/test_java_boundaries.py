"""Phase 7 framework/external-boundary fixtures.

Binding declarations resolve as ``CONFIGURATION_FACT`` (declared binding plus
source location); applied behavior, transaction outcomes, and concurrent
ordering always abstain with the documented named limitation code.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from investigation_agent_platform.application.analysis.java.boundary_facts import (
    resolve_java_framework_boundary,
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
    "actually ran",
    "was executed",
    "observed execution",
    "takes effect",
    "is applied",
)


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


def _no_applied_wording(result) -> None:
    payload = result.model_dump_json().lower()
    for banned in BANNED_WORDING:
        assert banned not in payload, banned


def test_bean_declaration_resolves_with_location():
    evidence = _source_evidence(
        "class AppConfig {\n"
        "  @Bean\n"
        "  public Foo foo() {\n"
        "    return new Foo();\n"
        "  }\n"
        "}"
    )
    result = resolve_java_framework_boundary(evidence, "foo")
    assert result.status == CodeFactStatus.RESOLVED
    assert result.fact_category == JavaFactCategory.CONFIGURATION_FACT
    binding = result.structured_fact["bindings"][0]
    assert binding["kind"] == "bean_definition"
    assert result.declaration_location is not None
    assert result.declaration_location.file_path == "src/Widget.java"
    assert result.declaration_location.revision == "abc123"
    _no_applied_wording(result)


def test_value_config_key_resolves_declared_key():
    evidence = _source_evidence(
        "class DbConfig {\n"
        '  @Value("${db.url}")\n'
        "  String url;\n"
        "}"
    )
    result = resolve_java_framework_boundary(evidence, "url")
    assert result.status == CodeFactStatus.RESOLVED
    binding = result.structured_fact["bindings"][0]
    assert binding["kind"] == "config_key"
    assert "db.url" in binding["detail"]
    _no_applied_wording(result)


def test_configuration_properties_resolves_prefix():
    evidence = _source_evidence(
        "class AppConfig {\n"
        '  @ConfigurationProperties(prefix = "app")\n'
        "  static class Props {}\n"
        "}"
    )
    result = resolve_java_framework_boundary(evidence, "Props")
    assert result.status == CodeFactStatus.RESOLVED
    assert result.structured_fact["bindings"][0]["kind"] == "configuration_properties"
    _no_applied_wording(result)


def test_injection_point_resolves_with_qualifier_note():
    evidence = _source_evidence(
        "class Orders {\n"
        "  @Autowired\n"
        '  @Qualifier("primary")\n'
        "  OrderRepo repo;\n"
        "}"
    )
    result = resolve_java_framework_boundary(evidence, "repo")
    assert result.status == CodeFactStatus.RESOLVED
    binding = result.structured_fact["bindings"][0]
    assert binding["kind"] == "injection_point"
    assert "Qualifier" in binding["detail"]
    _no_applied_wording(result)


def test_route_declaration_resolves():
    evidence = _source_evidence(
        "class Health {\n"
        '  @GetMapping("/health")\n'
        "  String check() { return \"ok\"; }\n"
        "}"
    )
    result = resolve_java_framework_boundary(evidence, "check")
    assert result.status == CodeFactStatus.RESOLVED
    binding = result.structured_fact["bindings"][0]
    assert binding["kind"] == "route_declaration"
    assert "/health" in binding["detail"]
    _no_applied_wording(result)


def test_declaration_never_claims_applied_behavior():
    evidence = _source_evidence(
        "class AppConfig {\n"
        "  @Bean\n"
        "  public Foo foo() {\n"
        "    return new Foo();\n"
        "  }\n"
        "}"
    )
    result = resolve_java_framework_boundary(evidence, "foo")
    assumptions = " ".join(result.assumptions).lower()
    assert "applied behavior requires runtime evidence" in assumptions
    assert "never verifies as applied configuration" in assumptions
    rule = category_proof_rule(JavaFactCategory.CONFIGURATION_FACT)
    assert rule is not None and rule.max_verdict == "LIKELY"
    _no_applied_wording(result)


def test_reflection_yields_named_partial():
    evidence = _source_evidence(
        "class Probe {\n"
        "  Object load(String name) throws Exception {\n"
        "    Class<?> type = Class.forName(name);\n"
        "    return type.getDeclaredConstructor().newInstance();\n"
        "  }\n"
        "}"
    )
    result = resolve_java_framework_boundary(evidence, "load")
    assert result.status == CodeFactStatus.PARTIAL
    assert "reflection_boundary" in result.limitation_codes
    assert "requires_runtime_observation" in result.limitation_codes
    _no_applied_wording(result)


def test_proxy_yields_named_partial():
    evidence = _source_evidence(
        "class Proxies {\n"
        "  Object wrap(Class<?> api, java.lang.reflect.InvocationHandler h) {\n"
        "    return java.lang.reflect.Proxy.newProxyInstance(\n"
        "        api.getClassLoader(), new Class<?>[] {api}, h);\n"
        "  }\n"
        "}"
    )
    result = resolve_java_framework_boundary(evidence, "wrap")
    assert result.status == CodeFactStatus.PARTIAL
    assert "dynamic_proxy_boundary" in result.limitation_codes


def test_lombok_annotation_yields_named_partial():
    evidence = _source_evidence(
        "import lombok.Data;\n"
        "@Data\n"
        "class User {\n"
        "  String name;\n"
        "}"
    )
    result = resolve_java_framework_boundary(evidence, "User")
    assert result.status == CodeFactStatus.PARTIAL
    assert "lombok_generated_code" in result.limitation_codes


def test_bare_value_annotation_is_lombok_not_config_key():
    evidence = _source_evidence(
        "class User {\n"
        "  @Value\n"
        "  String name;\n"
        "}"
    )
    result = resolve_java_framework_boundary(evidence, "name")
    assert result.status == CodeFactStatus.PARTIAL
    assert "lombok_generated_code" in result.limitation_codes
    assert result.structured_fact["bindings"] == []


def test_annotation_processing_yields_named_partial():
    evidence = _source_evidence(
        "class Gen extends javax.annotation.processing.AbstractProcessor {\n"
        "  public boolean process(java.util.Set<? extends javax.lang.model.element.TypeElement> a,\n"
        "      javax.annotation.processing.RoundEnvironment r) { return true; }\n"
        "}"
    )
    result = resolve_java_framework_boundary(evidence, "Gen")
    assert result.status == CodeFactStatus.PARTIAL
    assert "annotation_processing_boundary" in result.limitation_codes


def test_generated_source_yields_named_partial():
    evidence = _source_evidence(
        "class Mapper {\n"
        "  @jakarta.annotation.Generated(\"dagger\")\n"
        "  Object graph() { return null; }\n"
        "}"
    )
    result = resolve_java_framework_boundary(evidence, "graph")
    assert result.status == CodeFactStatus.PARTIAL
    assert "generated_source_boundary" in result.limitation_codes


def test_transactional_outcome_requires_runtime():
    evidence = _source_evidence(
        "class Orders {\n"
        "  @Transactional\n"
        "  public void save() {\n"
        "    store.put(\"a\");\n"
        "  }\n"
        "}"
    )
    result = resolve_java_framework_boundary(evidence, "save")
    assert result.status == CodeFactStatus.PARTIAL
    assert "transaction_outcome_requires_runtime" in result.limitation_codes
    assert "applied_behavior_requires_runtime_evidence" in result.limitation_codes
    _no_applied_wording(result)


def test_kafka_listener_names_binding_but_stays_partial():
    evidence = _source_evidence(
        "class Events {\n"
        '  @KafkaListener(topics = "orders")\n'
        "  void onOrder(String payload) {\n"
        "  }\n"
        "}"
    )
    result = resolve_java_framework_boundary(evidence, "onOrder")
    assert result.status == CodeFactStatus.PARTIAL
    assert "broker_runtime_boundary" in result.limitation_codes
    assert result.structured_fact["bindings"][0]["kind"] == "listener_binding"
    _no_applied_wording(result)


@pytest.mark.parametrize(
    ("source", "symbol", "code"),
    [
        (
            ("class Repo {\n"
             "  void save(String name) throws Exception {\n"
             "    java.sql.Connection c = open();\n"
             "  }\n"
             "}"),
            "save",
            "jdbc_runtime_boundary",
        ),
        (
            ("class Client {\n"
             "  org.springframework.web.client.RestTemplate rest;\n"
             "  String get(String url) {\n"
             "    return rest.getForObject(url, String.class);\n"
             "  }\n"
             "}"),
            "get",
            "http_client_runtime_boundary",
        ),
        (
            ("class Cache {\n"
             "  @Cacheable(\"items\")\n"
             "  String find(String id) { return load(id); }\n"
             "}"),
            "find",
            "cache_runtime_boundary",
        ),
        (
            ("class Codec {\n"
             "  com.fasterxml.jackson.databind.ObjectMapper mapper;\n"
             "  String write(Object o) throws Exception {\n"
             "    return mapper.writeValueAsString(o);\n"
             "  }\n"
             "}"),
            "write",
            "serialization_runtime_boundary",
        ),
        (
            ("class Clock {\n"
             "  String stamp() {\n"
             "    return java.time.Instant.now().toString();\n"
             "  }\n"
             "}"),
            "stamp",
            "time_dependence_requires_runtime",
        ),
        (
            ("class Labels {\n"
             "  String name() {\n"
             "    return java.util.ResourceBundle.getBundle(\"m\").getString(\"k\");\n"
             "  }\n"
             "}"),
            "name",
            "locale_dependence_requires_runtime",
        ),
        (
            ("class Dice {\n"
             "  int roll() {\n"
             "    return java.util.concurrent.ThreadLocalRandom.current().nextInt(6);\n"
             "  }\n"
             "}"),
            "roll",
            "randomness_requires_runtime",
        ),
        (
            ("class Jobs {\n"
             "  java.util.concurrent.Executor pool;\n"
             "  void run(Runnable task) {\n"
             "    pool.execute(task);\n"
             "  }\n"
             "}"),
            "run",
            "async_boundary",
        ),
    ],
)
def test_runtime_boundaries_abstain_with_named_codes(source, symbol, code):
    evidence = _source_evidence(source)
    result = resolve_java_framework_boundary(evidence, symbol)
    assert result.status == CodeFactStatus.PARTIAL
    assert code in result.limitation_codes
    _no_applied_wording(result)


def test_synchronized_abstains_nondeterministic():
    evidence = _source_evidence(
        "class Counter {\n"
        "  int total;\n"
        "  synchronized void tick() {\n"
        "    total = total + 1;\n"
        "  }\n"
        "}"
    )
    result = resolve_java_framework_boundary(evidence, "tick")
    assert result.status == CodeFactStatus.PARTIAL
    assert "nondeterministic_scheduling" in result.limitation_codes


def test_plain_method_has_no_binding_declaration():
    evidence = _source_evidence(
        "class Plain {\n"
        "  int add(int a, int b) {\n"
        "    return a + b;\n"
        "  }\n"
        "}"
    )
    result = resolve_java_framework_boundary(evidence, "add")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "no_binding_declaration_in_window" in result.limitation_codes


def test_binding_hint_filters_and_miss_abstains():
    evidence = _source_evidence(
        "class AppConfig {\n"
        "  @Bean\n"
        "  public Foo foo() {\n"
        "    return new Foo();\n"
        "  }\n"
        "}"
    )
    matched = resolve_java_framework_boundary(evidence, "foo", binding_hint="bean")
    assert matched.status == CodeFactStatus.RESOLVED
    missed = resolve_java_framework_boundary(evidence, "foo", binding_hint="route")
    assert missed.status == CodeFactStatus.UNRESOLVED
    assert "binding_hint_not_found" in missed.limitation_codes


def test_boundary_provenance_fail_closed():
    evidence = _source_evidence(
        "class Plain {\n  void run() {}\n}", revision="UNPINNED"
    )
    result = resolve_java_framework_boundary(evidence, "run")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "missing_immutable_source_provenance" in result.limitation_codes
    missing = resolve_java_framework_boundary(evidence, "absent")
    assert "missing_immutable_source_provenance" in missing.limitation_codes
    non_java = _source_evidence("class Plain {\n  void run() {}\n}", path="src/app.py")
    result = resolve_java_framework_boundary(non_java, "run")
    assert "not_a_java_source_file" in result.limitation_codes
