"""Phase 2 constant-expression fixtures: correct values and fail-closed cases."""

from datetime import UTC, datetime
from uuid import uuid4

from investigation_agent_platform.application.analysis.java.field_facts import (
    resolve_java_field_fact,
)
from investigation_agent_platform.domain.code.models import CodeFactStatus
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
    QueryFingerprint,
    SourceLocation,
)


def _source_evidence(source: str, *, start: int = 10, path: str = "src/Calc.java"):
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


def _resolved_value(field_type: str, initializer: str, **kwargs):
    evidence = _source_evidence(f"class Calc {{\n  {field_type} x = {initializer};\n}}", **kwargs)
    result = resolve_java_field_fact(evidence, "x")
    assert result.status == CodeFactStatus.RESOLVED
    return result.resolved_value


def test_arithmetic_with_precedence_and_wrapping():
    assert _resolved_value("int", "3 + 4") == "7"
    assert _resolved_value("int", "10 - 4 * 2") == "2"
    assert _resolved_value("int", "(2 + 3) * 4") == "20"
    assert _resolved_value("int", "7 / 2") == "3"
    assert _resolved_value("int", "-7 / 2") == "-3"
    assert _resolved_value("int", "7 % 3") == "1"
    assert _resolved_value("int", "2147483647 + 1") == "-2147483648"
    assert _resolved_value("int", "-2147483648") == "-2147483648"
    assert _resolved_value("long", "5L") == "5"


def test_integer_division_by_zero_is_partial():
    evidence = _source_evidence("class Calc {\n  int x = 7 / 0;\n}")
    result = resolve_java_field_fact(evidence, "x")
    assert result.status == CodeFactStatus.PARTIAL
    assert "constant_evaluation_error" in result.limitation_codes


def test_bare_overflowing_literal_is_invalid():
    evidence = _source_evidence("class Calc {\n  int x = 2147483648;\n}")
    result = resolve_java_field_fact(evidence, "x")
    assert result.status == CodeFactStatus.PARTIAL
    assert "invalid_java_literal" in result.limitation_codes


def test_bases_underscores_and_bad_spellings():
    assert _resolved_value("int", "1_000") == "1000"
    assert _resolved_value("int", "0xFF") == "255"
    assert _resolved_value("int", "0b101") == "5"
    assert _resolved_value("int", "017") == "15"
    for bad in ("08", "0o17", "1__000", "_100", "100_", "0x", "1.5"):
        evidence = _source_evidence(f"class Calc {{\n  int x = {bad.strip()};\n}}")
        result = resolve_java_field_fact(evidence, "x")
        # Lexically invalid spellings fail at literal validation (PARTIAL);
        # unparseable ones fail at the grammar level (UNRESOLVED). Both are
        # fail-closed; the distinction is recorded in limitation codes.
        assert result.status in {CodeFactStatus.PARTIAL, CodeFactStatus.UNRESOLVED}, bad


def test_long_needs_no_cast_but_int_rejects_long():
    assert _resolved_value("long", "5") == "5"
    evidence = _source_evidence("class Calc {\n  int x = 5L;\n}")
    result = resolve_java_field_fact(evidence, "x")
    assert result.status == CodeFactStatus.PARTIAL


def test_string_concatenation_and_conversion():
    assert _resolved_value("String", '"a" + "b"') == "ab"
    assert _resolved_value("String", '"n=" + 3') == "n=3"
    assert _resolved_value("String", '3 + "x"') == "3x"
    assert _resolved_value("String", '"x" + null') == "xnull"


def test_ternary_and_boolean_logic():
    assert _resolved_value("int", "true ? 1 : 2") == "1"
    assert _resolved_value("int", "false ? 1 : 2") == "2"
    assert _resolved_value("boolean", "!true") == "false"
    assert _resolved_value("boolean", "true && false") == "false"
    assert _resolved_value("boolean", "true || false") == "true"
    assert _resolved_value("boolean", "3 > 2") == "true"
    assert _resolved_value("boolean", "1 == 2") == "false"
    assert _resolved_value("boolean", "true & false") == "false"


def test_casts_narrow_with_wrap():
    assert _resolved_value("int", "(int) 300L") == "300"
    assert _resolved_value("int", "(byte) 200") == "-56"
    assert _resolved_value("String", "(char) 65") == "A"
    assert _resolved_value("char", "(char) 65") == "U+0041"


def test_shifts_follow_java_rules():
    assert _resolved_value("int", "1 << 3") == "8"
    assert _resolved_value("int", "-8 >> 2") == "-2"
    assert _resolved_value("int", "-8 >>> 2") == "1073741822"


def test_unsupported_expressions_stay_partial():
    for initializer in ("other + 1", "new int[3]", "values[0]", "true ? x : 1"):
        evidence = _source_evidence(f"class Calc {{\n  int x = {initializer};\n}}")
        result = resolve_java_field_fact(evidence, "x")
        assert result.status == CodeFactStatus.PARTIAL, initializer


def test_char_escapes_and_unicode():
    assert _resolved_value("char", r"'\n'") == "U+000A"
    assert _resolved_value("char", r"'\u0041'") == "U+0041"
    assert _resolved_value("String", r'"\u0041"') == "A"


def test_floats_and_narrowing_rules():
    assert _resolved_value("double", "1.5 + 1.5") == "3.0"
    assert _resolved_value("double", "1e3") == "1000.0"
    assert _resolved_value("double", "1.5f") == "1.5"
    evidence = _source_evidence("class Calc {\n  int x = 3.0;\n}")
    result = resolve_java_field_fact(evidence, "x")
    assert result.status == CodeFactStatus.PARTIAL
