"""Phase 3 initialization and construction fixtures (D4).

Proves class initialization and per-instance construction are separate
traces, constructor delegation is extracted in textual order, and source
order alone never proves execution.
"""

from datetime import UTC, datetime
from uuid import uuid4

from investigation_agent_platform.application.analysis.java.init_facts import (
    constructor_chain_targets,
    initialization_order_kind,
    resolve_java_init_fact,
)
from investigation_agent_platform.domain.code.models import (
    CodeFactStatus,
    CodeValueSemantics,
    SemanticStage,
)
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


_BOUNDED = (
    "class Widget {\n"
    "  static int count = 0;\n"
    "  static {\n"
    "    count = 1;\n"
    "  }\n"
    "  int size = 4;\n"
    "  {\n"
    "    size = 5;\n"
    "  }\n"
    "  Widget() {\n"
    "    this.size = 6;\n"
    "  }\n"
    "}"
)

_CHAIN = (
    "class Widget {\n"
    "  int size;\n"
    "  Widget() {\n"
    "    this(4);\n"
    "  }\n"
    "  Widget(int size) {\n"
    "    this.size = size;\n"
    "  }\n"
    "}"
)

_SUPER = "class Widget {\n  int size;\n  Widget() {\n    super();\n    this.size = 1;\n  }\n}"


def test_post_construction_on_bounded_path():
    result = resolve_java_init_fact(_source_evidence(_BOUNDED), "Widget")
    assert result.status == CodeFactStatus.RESOLVED
    assert result.semantic_stage == SemanticStage.POST_CONSTRUCTION
    assert result.value_semantics == CodeValueSemantics.POST_CONSTRUCTION
    fact = result.structured_fact
    assert fact is not None
    assert "class_initialization_trace" in fact
    assert "instance_construction_trace" in fact
    assert result.resolved_value is None


def test_static_vs_instance_isolation():
    result = resolve_java_init_fact(_source_evidence(_BOUNDED), "Widget")
    fact = result.structured_fact
    assert fact is not None
    class_trace = fact["class_initialization_trace"]
    instance_trace = fact["instance_construction_trace"]
    assert class_trace["semantic_stage"] == SemanticStage.CLASS_INITIALIZATION.value
    assert instance_trace["semantic_stage"] != SemanticStage.CLASS_INITIALIZATION.value
    class_kinds = {step["kind"] for step in class_trace["steps"]}
    assert class_kinds <= {"static_field_initializer", "class_initializer"}
    instance_kinds = {step["kind"] for step in instance_trace["instance_steps"]}
    assert "static_field_initializer" not in instance_kinds
    assert "class_initializer" not in instance_kinds
    assert class_kinds.isdisjoint(instance_kinds)


def test_this_chain_extraction_in_textual_order():
    result = resolve_java_init_fact(_source_evidence(_CHAIN), "Widget")
    fact = result.structured_fact
    assert fact is not None
    constructors = fact["instance_construction_trace"]["constructors"]
    assert constructors[0]["chain_targets"] == ["this(4)"]
    assert constructors[1]["chain_targets"] == []
    assert fact["instance_construction_trace"]["constructor_chain_targets"] == ["this(4)"]


def test_super_chain_target_extraction():
    result = resolve_java_init_fact(_source_evidence(_SUPER), "Widget")
    fact = result.structured_fact
    assert fact is not None
    constructors = fact["instance_construction_trace"]["constructors"]
    assert constructors[0]["chain_targets"] == ["super()"]


def test_cross_file_superclass_is_partial():
    source = "class Child extends Base {\n  Child() {\n    super(1);\n  }\n}"
    result = resolve_java_init_fact(_source_evidence(source), "Child")
    assert result.status == CodeFactStatus.PARTIAL
    assert result.value_semantics == CodeValueSemantics.LATER_MUTABLE
    assert "requires_cross_file_scope" in result.limitation_codes
    assert result.semantic_stage != SemanticStage.RUNTIME_EFFECTIVE


def test_enum_constants_are_partial():
    source = "enum Color {\n  RED, GREEN;\n}"
    result = resolve_java_init_fact(_source_evidence(source), "Color")
    assert result.status == CodeFactStatus.PARTIAL
    assert "enum_constants_require_dedicated_rule" in result.limitation_codes
    assert result.value_semantics == CodeValueSemantics.LATER_MUTABLE


def test_record_with_compact_constructor_is_partial():
    source = (
        "record Point(int x, int y) {\n"
        "  Point {\n"
        "    if (x < 0) {\n"
        "      throw new IllegalArgumentException();\n"
        "    }\n"
        "  }\n"
        "}"
    )
    result = resolve_java_init_fact(_source_evidence(source), "Point")
    assert result.status == CodeFactStatus.PARTIAL
    assert "record_components_require_dedicated_rule" in result.limitation_codes
    assert "compact_constructor_requires_dedicated_rule" in result.limitation_codes


def test_di_construction_is_partial():
    source = (
        "import javax.inject.Inject;\n"
        "public class Widget {\n"
        "  int size;\n"
        "  @Inject\n"
        "  public Widget(int size) {\n"
        "    this.size = size;\n"
        "  }\n"
        "}"
    )
    result = resolve_java_init_fact(_source_evidence(source), "Widget")
    assert result.status == CodeFactStatus.PARTIAL
    assert "requires_cross_file_scope" in result.limitation_codes


def test_generated_constructor_is_partial():
    source = (
        "import lombok.AllArgsConstructor;\n@AllArgsConstructor\nclass Widget {\n  int size;\n}"
    )
    result = resolve_java_init_fact(_source_evidence(source), "Widget")
    assert result.status == CodeFactStatus.PARTIAL
    assert "requires_generated_source" in result.limitation_codes


def test_constructor_body_call_is_partial():
    source = "class Widget {\n  int size;\n  Widget() {\n    this.size = compute();\n  }\n}"
    result = resolve_java_init_fact(_source_evidence(source), "Widget")
    assert result.status == CodeFactStatus.PARTIAL
    assert "constructor_body_not_bounded_in_window" in result.limitation_codes
    assert result.value_semantics == CodeValueSemantics.LATER_MUTABLE


def test_source_order_does_not_prove_execution():
    # A later method writes the field after construction; the construction
    # trace must exclude it and stay honest about later mutation.
    source = (
        "class Widget {\n"
        "  int size = 1;\n"
        "  Widget() {\n"
        "    this.size = 2;\n"
        "  }\n"
        "  void resize(int size) {\n"
        "    this.size = size;\n"
        "  }\n"
        "}"
    )
    result = resolve_java_init_fact(_source_evidence(source), "Widget")
    assert result.status == CodeFactStatus.PARTIAL
    assert result.semantic_stage == SemanticStage.LATER_MUTATION
    assert "later_field_mutation_in_window" in result.limitation_codes
    assert "source_order_does_not_prove_execution" in result.assumptions


def test_nothing_is_ever_runtime_effective():
    fixtures = [_BOUNDED, _CHAIN, _SUPER]
    for fixture in fixtures:
        result = resolve_java_init_fact(_source_evidence(fixture), "Widget")
        assert result.value_semantics != CodeValueSemantics.RUNTIME_EFFECTIVE
        assert result.semantic_stage != SemanticStage.RUNTIME_EFFECTIVE


def test_initialization_order_kind_helpers():
    from tree_sitter_language_pack import get_parser

    parser = get_parser("java")
    source = (
        b"class Widget { static int a; static { a = 1; } { b = 2; } "
        b"Widget() {} int b; void idle() {} }"
    )
    root = parser.parse(source).root_node
    kinds: dict[str, str] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        kinds[node.type] = initialization_order_kind(node)
        stack.extend(node.children)
    assert kinds["static_initializer"] == "class_initializer"
    assert kinds["block"] == "instance_initializer"
    assert kinds["constructor_declaration"] == "constructor_body"
    assert kinds["field_declaration"] == "field_initializer"
    assert kinds["method_declaration"] == "unsupported_initialization_node"


def test_constructor_chain_targets_helper():
    from tree_sitter_language_pack import get_parser

    from investigation_agent_platform.application.analysis.java import _parse as _p

    parser = get_parser("java")
    source = b"class Widget { Widget() { this(1); } Widget(int size) { super(); } }"
    root = parser.parse(source).root_node
    constructors = [node for node in _p.walk(root) if node.type == "constructor_declaration"]
    assert constructor_chain_targets(constructors[0], source) == ["this(1)"]
    assert constructor_chain_targets(constructors[1], source) == ["super()"]
