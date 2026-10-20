from datetime import UTC, datetime
from uuid import uuid4

import pytest

from investigation_agent_platform.application.analysis.java_field_facts import (
    resolve_java_field_fact,
)
from investigation_agent_platform.domain.code.models import (
    CodeFactStatus,
    CodeValueSemantics,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)


def _source_evidence(source: str, *, start: int = 10, path: str = "src/TransactionProcessor.java"):
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


@pytest.mark.parametrize(
    ("field_type", "initializer", "expected"),
    [
        ("int", "3", "3"),
        ("boolean", "false", "false"),
        ("String", '"ready"', "ready"),
        ("char", "'a'", "U+0061"),
        ("char", None, "U+0000"),
        ("double", None, "0.0"),
        ("String", None, "null"),
    ],
)
def test_resolves_explicit_literals_and_java_field_defaults(field_type, initializer, expected):
    value = f" = {initializer}" if initializer is not None else ""
    evidence = _source_evidence(
        f"class TransactionProcessor {{\n  private {field_type} retryCount{value};\n}}"
    )

    result = resolve_java_field_fact(evidence, "retryCount")

    assert result.status == CodeFactStatus.RESOLVED
    assert result.resolved_value == expected
    assert result.revision == "abc123"
    assert result.declaration_location.line_start == 11
    assert result.source_evidence_ids == (evidence.evidence_id,)
    assert result.value_semantics == (
        CodeValueSemantics.LANGUAGE_DEFAULT
        if initializer is None
        else CodeValueSemantics.EXPLICIT_INITIALIZER
    )


def test_resolves_multiple_declarators_with_shared_type():
    evidence = _source_evidence(
        "class TransactionProcessor {\n  private int retryCount = 3, other = 4;\n}"
    )
    result = resolve_java_field_fact(evidence, "other")
    assert result.status == CodeFactStatus.RESOLVED
    assert result.field_type == "int"
    assert result.resolved_value == "4"


def test_resolves_literal_signs_and_numeric_bases():
    for literal, expected in (("-3", "-3"), ("0x10", "16"), ("012", "10")):
        evidence = _source_evidence(f"class TransactionProcessor {{ int retryCount = {literal}; }}")
        result = resolve_java_field_fact(evidence, "retryCount")
        assert result.status == CodeFactStatus.RESOLVED
        assert result.resolved_value == expected


def test_local_variable_is_not_resolved_as_field():
    evidence = _source_evidence("class TransactionProcessor {\n  void run() { int retryCount; }\n}")
    result = resolve_java_field_fact(evidence, "retryCount")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "field_not_found" in result.limitation_codes


def test_unsupported_expression_is_partial():
    evidence = _source_evidence(
        "class TransactionProcessor {\n  private int retryCount = configuredRetries();\n}"
    )
    result = resolve_java_field_fact(evidence, "retryCount")
    assert result.status == CodeFactStatus.PARTIAL
    assert result.raw_initializer == "configuredRetries()"
    assert "unsupported_initializer_expression" in result.limitation_codes


def test_ambiguous_same_named_fields_are_unresolved():
    evidence = _source_evidence(
        "class TransactionProcessor {\n  int retryCount = 1;\n}\n"
        "class OtherProcessor {\n  int retryCount = 2;\n}"
    )
    result = resolve_java_field_fact(evidence, "retryCount")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "ambiguous_field_declaration" in result.limitation_codes


def test_wrong_declaring_type_is_not_matched():
    evidence = _source_evidence("class TransactionProcessor { int retryCount = 3; }")
    result = resolve_java_field_fact(evidence, "retryCount", declaring_type="OtherProcessor")
    assert result.status == CodeFactStatus.UNRESOLVED


def test_non_java_file_is_unresolved():
    evidence = _source_evidence(
        "class TransactionProcessor { int retryCount = 3; }",
        path="src/TransactionProcessor.py",
    )
    result = resolve_java_field_fact(evidence, "retryCount")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "not_a_java_source_file" in result.limitation_codes


def test_unpinned_or_path_only_evidence_is_rejected():
    evidence = _source_evidence("class TransactionProcessor { int retryCount = 3; }")
    attrs = dict(evidence.attributes)
    attrs["read_mode"] = "path_only"
    evidence = evidence.model_copy(update={"attributes": attrs})
    result = resolve_java_field_fact(evidence, "retryCount")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "source_read_not_range_pinned" in result.limitation_codes


def test_missing_revision_returns_unresolved_without_fabricating_it():
    evidence = _source_evidence("class TransactionProcessor { int retryCount = 3; }")
    provenance = evidence.provenance.model_copy(
        update={
            "source_location": evidence.provenance.source_location.model_copy(
                update={"revision": None}
            )
        }
    )
    evidence = evidence.model_copy(update={"provenance": provenance})
    result = resolve_java_field_fact(evidence, "retryCount")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert result.revision is None
    assert "missing_immutable_source_provenance" in result.limitation_codes
