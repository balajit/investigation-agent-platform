"""Phase 0 — Java baseline audit, counterexample fixtures, and contract decision.

Migration decision (recorded, no src changes in Phase 0): versioned-key
(dual-emit). The current CodeFactResolution uses extra="forbid" and requires
the field-shaped keys, so any generalized payload (fact_category,
subject_identity, spans, ...) fails validation in current readers and in
Evidence.attributes validation. Reader-first rollout is rejected for Phase 0
because it needs a synchronized upgrade of every consumer with no consumer
inventory in hand. When generalization lands, emit the new contract under a
distinct versioned key (e.g. code_fact_resolution_v2) while continuing to
emit the legacy code_fact_resolution shape; new readers accept both and
reject conflicting duplicates; old readers ignore the v2 key. Downgrade and
replay tests below pin the strict-reader behavior this decision relies on.

Scope: audit + tests + contract decision only. No new resolvers, no new Java
semantics. tests/unit/test_java_field_facts.py is the untouched anchor.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investigation_agent_platform.application.analysis.java_field_facts import (
    resolve_java_field_fact,
)
from investigation_agent_platform.application.investigation.verification import (
    VerificationEngine,
)
from investigation_agent_platform.domain.analysis.question_shape import (
    classify_question_shape,
)
from investigation_agent_platform.domain.code.models import (
    CodeFactResolution,
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


def _source_evidence(
    source: str,
    *,
    start: int = 10,
    path: str = "src/Foo.java",
    extra_attrs: dict | None = None,
):
    investigation_id = uuid4()
    evidence_id = uuid4()
    now = datetime.now(UTC)
    lines = "\n".join(f"{start + index}| {line}" for index, line in enumerate(source.splitlines()))
    end = start + max(0, len(source.splitlines()) - 1)
    attributes = {"read_mode": "range"}
    if extra_attrs:
        attributes.update(extra_attrs)
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
        attributes=attributes,
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


def _raw_numbered_evidence(numbered_lines: str, *, start: int = 10, end: int = 13):
    investigation_id = uuid4()
    evidence_id = uuid4()
    now = datetime.now(UTC)
    path = "src/Foo.java"
    return Evidence(
        evidence_id=evidence_id,
        tenant_id="tenant-a",
        investigation_id=investigation_id,
        evidence_type=EvidenceType.SOURCE_CODE,
        provider="pygit2-local",
        source=f"git://repo/{path}@abc123",
        title="Java source range",
        summary="Pinned Java source",
        content_snippet=numbered_lines,
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


# ---------------------------------------------------------------------------
# A. Baseline verification: the Phase 0 audit confirms these claims at HEAD.
# ---------------------------------------------------------------------------


def test_baseline_explicit_literal_resolves():
    evidence = _source_evidence("class Foo {\n int x = 3;\n}")
    result = resolve_java_field_fact(evidence, "x")
    assert result.status == CodeFactStatus.RESOLVED
    assert result.resolved_value == "3"
    assert result.value_semantics == CodeValueSemantics.EXPLICIT_INITIALIZER
    assert result.revision == "abc123"
    assert result.source_evidence_ids == (evidence.evidence_id,)


def test_baseline_resolution_shape_is_field_shaped_and_strict():
    assert {s.value for s in CodeFactStatus} == {"RESOLVED", "PARTIAL", "UNRESOLVED"}
    assert {
        "EXPLICIT_INITIALIZER",
        "LANGUAGE_DEFAULT",
        "POST_CONSTRUCTION",
        "LATER_MUTABLE",
        "RUNTIME_EFFECTIVE",
    } <= {s.value for s in CodeValueSemantics}
    evidence = _source_evidence("class Foo {\n int x = 3;\n}")
    dumped = resolve_java_field_fact(evidence, "x").model_dump(mode="json")
    assert {"field_name", "field_type", "field_category"} <= set(dumped)
    with pytest.raises(ValidationError):
        CodeFactResolution.model_validate({**dumped, "symbol_name": "x"})


@pytest.mark.parametrize(
    "question",
    [
        "what is the default value of retryCount?",
        "what is the field initializer for retryCount?",
        "what value does retryCount initialize to?",
        "what is the declared default for retryCount?",
    ],
)
def test_baseline_source_value_questions_route_to_code(question):
    assert classify_question_shape(question) == "code"


@pytest.mark.parametrize(
    "question",
    [
        "what is the default value of retryCount in production?",
        "what is the effective runtime value of retryCount?",
    ],
)
def test_baseline_runtime_marked_questions_do_not_route_to_code_only(question):
    assert classify_question_shape(question) != "code"


def test_baseline_field_value_claim_detector():
    assert VerificationEngine.claims_field_value("The default value of x is 3")
    assert VerificationEngine.claims_field_value("The field initializes to 3")
    assert not VerificationEngine.claims_field_value("The service calls the database")


def test_baseline_legacy_resolved_claim_still_proves():
    evidence = _source_evidence("class Foo {\n int x = 3;\n}")
    dumped = resolve_java_field_fact(evidence, "x").model_dump(mode="json")
    banked = evidence.model_copy(
        update={"attributes": {**evidence.attributes, "code_fact_resolution": dumped}}
    )
    Evidence.model_validate(banked.model_dump(mode="json"))
    hypothesis = SimpleNamespace(
        id=uuid4(),
        statement="The default value of x is 3",
        claim_value_semantics="EXPLICIT_INITIALIZER",
        claimed_value="3",
        claim_target=SimpleNamespace(
            symbol="x", declaring_type="Foo", file_path="src/Foo.java", line_start=11
        ),
        supporting_evidence_ids=[banked.evidence_id],
    )
    ok, reason = VerificationEngine.code_value_proof(hypothesis, [banked])
    assert ok, reason
    result = VerificationEngine().validate_code_value_claim(hypothesis, [banked], [])
    assert result is not None
    assert result.code_value_validated


# ---------------------------------------------------------------------------
# B. Counterexample fixtures. xfail = confirmed gap at HEAD, pinned so a
# future phase must turn it into a strict fail-closed test.
# ---------------------------------------------------------------------------


def test_python_octal_spelling_is_not_java_octal():
    evidence = _source_evidence("class Foo {\n int x = 0o17;\n}")
    result = resolve_java_field_fact(evidence, "x")
    assert result.status in {CodeFactStatus.PARTIAL, CodeFactStatus.UNRESOLVED}


def test_java_s_escape_resolves_to_space():
    evidence = _source_evidence('class Foo {\n String x = "\\s";\n}')
    result = resolve_java_field_fact(evidence, "x", language_version=17)
    assert result.status == CodeFactStatus.RESOLVED
    assert result.resolved_value == " "


def test_java_s_escape_abstains_without_source_level():
    evidence = _source_evidence('class Foo {\n String x = "\\s";\n}')
    result = resolve_java_field_fact(evidence, "x")
    assert result.status in {CodeFactStatus.PARTIAL, CodeFactStatus.UNRESOLVED}
    assert "unsupported_language_version" in result.limitation_codes


def test_text_block_strips_incidental_whitespace_per_jls():
    evidence = _source_evidence('class Foo {\n String x = """\n     hello\n     """;\n}')
    result = resolve_java_field_fact(evidence, "x", language_version=17)
    assert result.status == CodeFactStatus.RESOLVED
    assert result.resolved_value == "hello\n"


def test_text_block_abstains_without_source_level():
    evidence = _source_evidence('class Foo {\n String x = """\n     hello\n     """;\n}')
    result = resolve_java_field_fact(evidence, "x")
    assert result.status in {CodeFactStatus.PARTIAL, CodeFactStatus.UNRESOLVED}
    assert "unsupported_language_version" in result.limitation_codes


def test_language_version_gating_in_phase2():
    assert "language_version" in inspect.signature(resolve_java_field_fact).parameters
    evidence = _source_evidence('class Foo {\n String x = """\n hello\n """;\n}')
    result = resolve_java_field_fact(evidence, "x", language_version=17)
    assert result.status == CodeFactStatus.RESOLVED
    assert result.value_semantics == CodeValueSemantics.EXPLICIT_INITIALIZER


def test_constructor_write_is_not_claimed_as_post_construction():
    evidence = _source_evidence("class Foo {\n int x = 1;\n Foo() { x = 2; }\n}")
    result = resolve_java_field_fact(evidence, "x")
    assert result.status == CodeFactStatus.RESOLVED
    assert result.resolved_value == "1"
    assert result.value_semantics == CodeValueSemantics.EXPLICIT_INITIALIZER


def test_static_initializer_write_is_not_claimed():
    evidence = _source_evidence("class Foo {\n static int x = 1;\n static { x = 2; }\n}")
    result = resolve_java_field_fact(evidence, "x")
    assert result.status == CodeFactStatus.RESOLVED
    assert result.resolved_value == "1"
    assert result.value_semantics == CodeValueSemantics.EXPLICIT_INITIALIZER
    assert result.field_category == "STATIC_FIELD"


@pytest.mark.parametrize(
    "numbered",
    [
        "10| class Foo {\n12|  int x = 3;\n13| }",
        "10| class Foo {\n13|  int x = 3;\n11| }",
    ],
)
def test_noncontiguous_numbered_lines_fail_closed(numbered):
    evidence = _raw_numbered_evidence(numbered)
    result = resolve_java_field_fact(evidence, "x")
    assert result.status == CodeFactStatus.UNRESOLVED


def test_syntax_error_fails_closed():
    evidence = _source_evidence("class Foo {\n int x = 1__2;\n}")
    result = resolve_java_field_fact(evidence, "x")
    assert result.status == CodeFactStatus.UNRESOLVED
    assert "java_syntax_error_or_incomplete_source_window" in result.limitation_codes


# ---------------------------------------------------------------------------
# C. Legacy payload round-trips and default-deny unknown shapes.
# ---------------------------------------------------------------------------


def test_legacy_banked_dict_round_trips():
    evidence = _source_evidence("class Foo {\n int x = 3;\n}")
    dumped = resolve_java_field_fact(evidence, "x").model_dump(mode="json")
    assert CodeFactResolution.model_validate(dumped).model_dump(mode="json") == dumped
    banked = _source_evidence(
        "class Foo {\n int x = 3;\n}", extra_attrs={"code_fact_resolution": dumped}
    )
    assert banked.attributes["code_fact_resolution"] == dumped


def test_malformed_banked_resolution_rejected_by_evidence_validation():
    with pytest.raises(ValidationError):
        _source_evidence(
            "class Foo {\n int x = 3;\n}",
            extra_attrs={"code_fact_resolution": {"status": "RESOLVED"}},
        )


def test_unknown_status_string_rejected():
    evidence = _source_evidence("class Foo {\n int x = 3;\n}")
    dumped = resolve_java_field_fact(evidence, "x").model_dump(mode="json")
    with pytest.raises(ValidationError):
        CodeFactResolution.model_validate({**dumped, "status": "LIKELY"})


def test_unknown_value_semantics_rejected():
    evidence = _source_evidence("class Foo {\n int x = 3;\n}")
    dumped = resolve_java_field_fact(evidence, "x").model_dump(mode="json")
    with pytest.raises(ValidationError):
        CodeFactResolution.model_validate({**dumped, "value_semantics": "SOURCE_VALUE_FACT"})


def test_partial_and_unresolved_banked_rows_are_non_verifying():
    partial_evidence = _source_evidence(
        "class Foo {\n int x = configured();\n}",
        extra_attrs={
            "code_fact_resolution": resolve_java_field_fact(
                _source_evidence("class Foo {\n int x = configured();\n}"), "x"
            ).model_dump(mode="json")
        },
    )
    assert VerificationEngine._resolution_from_evidence([partial_evidence]) is None
    hypothesis = SimpleNamespace(statement="The default value of x is 1")
    ok, _ = VerificationEngine.code_value_proof(hypothesis, [partial_evidence])
    assert not ok


def test_extended_shape_rejected_by_current_strict_reader():
    evidence = _source_evidence("class Foo {\n int x = 3;\n}")
    dumped = resolve_java_field_fact(evidence, "x").model_dump(mode="json")
    with pytest.raises(ValidationError):
        CodeFactResolution.model_validate({**dumped, "fact_category": "SOURCE_VALUE_FACT"})


def test_unknown_category_payload_is_non_verifying():
    evidence = _source_evidence("class Foo {\n int x = 3;\n}")
    dumped = resolve_java_field_fact(evidence, "x").model_dump(mode="json")
    foreign = {**dumped, "fact_category": "SOURCE_VALUE_FACT", "status": "RESOLVED"}
    assert (
        VerificationEngine._resolution_from_evidence(
            [SimpleNamespace(attributes={"code_fact_resolution": foreign})]
        )
        is None
    )
    hypothesis = SimpleNamespace(statement="The default value of x is 3")
    ok, _ = VerificationEngine.code_value_proof(
        hypothesis, [SimpleNamespace(attributes={"code_fact_resolution": foreign})]
    )
    assert not ok
