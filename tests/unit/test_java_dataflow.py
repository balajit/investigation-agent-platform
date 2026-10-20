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
        "class Runner {\n"
        "  int run() {\n"
        "    int x = 1;\n"
        "    x = 2;\n"
        "    return x;\n"
        "  }\n"
        "}"
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
        "class Runner {\n"
        "  int run() {\n"
        "    int y = x + 1;\n"
        "    int x = 1;\n"
        "    return y;\n"
        "  }\n"
        "}"
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
