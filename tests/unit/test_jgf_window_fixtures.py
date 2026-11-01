# tests/unit/test_jgf_window_fixtures.py
"""JGF golden fixtures: review-8.4 gap list as window-enumeration cases (T01-T05).

Status/codes ride the manifest battery; this file pins the detailed
expectations per fixture. T06-T09 (Spring) are staged inert under
tests/fixtures/spring_config/ until the Spring gate lifts.
"""

from __future__ import annotations

from test_java_fixture_battery import _evidence_for

from investigation_agent_platform.application.analysis.java import window_facts


def test_jgf_t01_records_sealed_types_and_patterns_with_a_syntax_error() -> None:
    analysis = window_facts.enumerate_java_window_facts(
        _evidence_for("Shapes17.java"), language_version=17
    )
    assert analysis.status.value == "degraded"
    assert "java_syntax_error_or_incomplete_source_window" in analysis.limitations
    assert "analysis_failed" not in analysis.limitations
    subjects = {(r.subject_identity or "") for r in analysis.records}
    assert any("Circle" in s and "radius" in s for s in subjects)
    assert any("Square" in s and "side" in s for s in subjects)
    assert not any("broken" in s for s in subjects), "the broken method yields nothing"


def test_jgf_t02_unknown_language_version_never_guesses() -> None:
    from investigation_agent_platform.application.analysis.java import type_facts

    rec = type_facts.resolve_java_type_fact(_evidence_for("Shapes17.java"), "Point")
    assert rec.status.value == "UNRESOLVED"
    # Never a guessed parse: no level is stamped when none was given.
    assert rec.language_version is None


def test_jgf_t03_nested_enum_interface_constants_and_class() -> None:
    analysis = window_facts.enumerate_java_window_facts(
        _evidence_for("Nested.java"), language_version=17
    )
    assert analysis.status.value == "ok"
    subjects = {(r.subject_identity or "") for r in analysis.records}
    # Full nesting on every subject.
    assert any("OrderServiceHandler.Mode" in s for s in subjects)
    # Interface constant resolves.
    assert any(s.endswith("Limits.LIMIT") for s in subjects)
    # Enum constants enumerate (the enum_constant_not_modeled marker path
    # lives in the backfill layer, pinned by J5 tests).
    assert any("Mode.STANDARD" in s for s in subjects)


def test_jgf_t04_budget_and_truncation() -> None:
    import investigation_agent_platform.application.analysis.java.window_facts as wf

    cap = wf.MAX_WINDOW_RECORDS
    try:
        wf.MAX_WINDOW_RECORDS = 10
        analysis = window_facts.enumerate_java_window_facts(
            _evidence_for("Wide.java"),
            language_version=17,
            subject_hint="com.example.wide.Wide.tail",
        )
    finally:
        wf.MAX_WINDOW_RECORDS = cap
    subjects = {(r.subject_identity or "") for r in analysis.records}
    assert "com.example.wide.Wide.tail" in subjects
    assert analysis.omitted > 0, "truncation is reported, never silent"
    assert "window_records_truncated" in analysis.limitations


def test_jgf_t05_line_separators() -> None:
    from pathlib import Path

    raw = (Path(__file__).resolve().parents[1] / "fixtures" / "java" / "Separators.java").read_bytes()
    assert b"\r\n" in raw and b"\x0c" in raw and " ".encode() in raw
    lf = (
        raw.decode("utf-8")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace(" ", " ")
        .replace("", "")
    )

    def _numbered(text: str):  # type: ignore[no-untyped-def]
        base = _evidence_for("Separators.java")
        lines = text.split("\n")
        return base.model_copy(
            update={
                "content_snippet": "\n".join(
                    f"{10 + i}| {ln}" for i, ln in enumerate(lines)
                )
            }
        )

    exotic = window_facts.enumerate_java_window_facts(_numbered(raw.decode("utf-8")))
    baseline = window_facts.enumerate_java_window_facts(_numbered(lf))

    def _shape(analysis):  # type: ignore[no-untyped-def]
        return sorted(
            (str(r.fact_category), r.subject_identity or "", r.declaration_location.line_start)
            for r in analysis.records
        )

    assert exotic.status.value == "ok"
    assert _shape(exotic) == _shape(baseline), "records match the LF baseline"
    assert [r.declaration_location.line_start for r in exotic.records] == [
        r.declaration_location.line_start for r in baseline.records
    ]
