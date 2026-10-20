"""Phase 2 local-variable and parameter fixtures."""

from datetime import UTC, datetime
from uuid import uuid4

from investigation_agent_platform.application.analysis.java.local_facts import (
    resolve_java_local_fact,
)
from investigation_agent_platform.domain.code.models import CodeFactStatus
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


def test_local_literal_resolves_with_method_scope():
    evidence = _source_evidence("class Runner {\n  void run() {\n    int n = 3;\n  }\n}")
    result = resolve_java_local_fact(evidence, "n")
    assert result.status == CodeFactStatus.RESOLVED
    assert result.resolved_value == "3"
    assert "run" in (result.subject_identity or "")
    assert result.declaring_type == "run"


def test_local_without_initializer_is_unresolved():
    evidence = _source_evidence("class Runner {\n  void run() {\n    int n;\n  }\n}")
    result = resolve_java_local_fact(evidence, "n")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "uninitialized_local" in result.limitation_codes


def test_parameter_value_is_call_site_dependent():
    evidence = _source_evidence("class Runner {\n  void run(int n) {\n  }\n}")
    result = resolve_java_local_fact(evidence, "n")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "parameter_value_unknown" in result.limitation_codes


def test_local_shadows_field_but_field_resolver_is_unaffected():
    from investigation_agent_platform.application.analysis.java.field_facts import (
        resolve_java_field_fact,
    )

    evidence = _source_evidence(
        "class Runner {\n  int n = 1;\n  void run() {\n    int n = 2;\n  }\n}"
    )
    local = resolve_java_local_fact(evidence, "n")
    assert local.status == CodeFactStatus.RESOLVED
    assert local.resolved_value == "2"
    field = resolve_java_field_fact(evidence, "n")
    assert field.status == CodeFactStatus.RESOLVED
    assert field.resolved_value == "1"


def test_same_named_locals_in_two_methods_are_ambiguous():
    evidence = _source_evidence(
        "class Runner {\n"
        "  void run() {\n    int n = 1;\n  }\n"
        "  void walk() {\n    int n = 2;\n  }\n}"
    )
    result = resolve_java_local_fact(evidence, "n")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "ambiguous_local_declaration" in result.limitation_codes


def test_method_scope_disambiguates_locals():
    evidence = _source_evidence(
        "class Runner {\n"
        "  void run() {\n    int n = 1;\n  }\n"
        "  void walk() {\n    int n = 2;\n  }\n}"
    )
    result = resolve_java_local_fact(evidence, "n", method_scope="walk")
    assert result.status == CodeFactStatus.RESOLVED
    assert result.resolved_value == "2"


def test_var_is_a_declared_anchor_with_known_value():
    evidence = _source_evidence("class Runner {\n  void run() {\n    var n = 3;\n  }\n}")
    result = resolve_java_local_fact(evidence, "n")
    assert result.status == CodeFactStatus.RESOLVED
    assert result.resolved_value == "3"
    assert result.field_type == "var"


def test_local_with_call_initializer_is_partial():
    evidence = _source_evidence("class Runner {\n  void run() {\n    int n = compute();\n  }\n}")
    result = resolve_java_local_fact(evidence, "n")
    assert result.status == CodeFactStatus.PARTIAL
    assert "unsupported_initializer_expression" in result.limitation_codes


def test_missing_local_is_unresolved():
    evidence = _source_evidence("class Runner {\n  void run() {\n    int m = 1;\n  }\n}")
    result = resolve_java_local_fact(evidence, "n")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "local_not_found" in result.limitation_codes
