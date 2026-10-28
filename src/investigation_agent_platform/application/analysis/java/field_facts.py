# src/investigation_agent_platform/application/analysis/java/field_facts.py
"""Deterministic, revision-pinned Java field initializer/default resolution.

Phase 2 home of the field baseline (moved from
``application/analysis/java_field_facts.py``, now a re-export shim).
"""

from __future__ import annotations

from typing import Any, Literal

from investigation_agent_platform.application.analysis.java import _parse as _p
from investigation_agent_platform.application.analysis.java import expressions as _expr
from investigation_agent_platform.application.analysis.java._provenance import (
    java_source_lines,
    repository_of,
)
from investigation_agent_platform.domain.code.models import (
    CodeFactResolution,
    CodeFactStatus,
    CodeValueSemantics,
)


def _language_default(
    field_type: str,
) -> tuple[str | None, CodeValueSemantics | None]:
    if field_type == "boolean":
        return ("false", CodeValueSemantics.LANGUAGE_DEFAULT)
    if field_type in {"byte", "short", "int", "long"}:
        return ("0", CodeValueSemantics.LANGUAGE_DEFAULT)
    if field_type in {"float", "double"}:
        return ("0.0", CodeValueSemantics.LANGUAGE_DEFAULT)
    if field_type == "char":
        return ("U+0000", CodeValueSemantics.LANGUAGE_DEFAULT)
    type_base = field_type.replace("[]", "").split("<", 1)[0].strip()
    if "[]" in field_type or type_base not in {
        "boolean",
        "byte",
        "short",
        "int",
        "long",
        "char",
        "float",
        "double",
    }:
        return ("null", CodeValueSemantics.LANGUAGE_DEFAULT)
    return (None, None)


def resolve_java_field_fact(
    evidence: Any,
    field_name: str,
    *,
    declaring_type: str | None = None,
    language_version: object = None,
) -> CodeFactResolution:
    """Resolve one Java class field from exact source-read evidence.

    The function reads no files and follows no references. Its only source of
    facts is the supplied, revision-pinned ``GET_CODE`` evidence.
    ``language_version`` is a caller-attested trusted source level (tests or
    build metadata); ``None`` means unknown and version-gated syntax abstains.
    """
    lines, reason = java_source_lines(evidence)
    provenance = getattr(evidence, "provenance", None)
    location = getattr(provenance, "source_location", None)
    source_path = str(getattr(location, "file_path", "") or "")
    revision = str(getattr(location, "revision", "") or "")
    identifier = str(getattr(location, "identifier", "") or "")
    repository = repository_of(identifier, source_path)
    evidence_id = getattr(evidence, "evidence_id", None)
    evidence_ids = (evidence_id,) if evidence_id is not None else ()

    def unresolved(code: str) -> CodeFactResolution:
        fallback_location = location
        if fallback_location is None:
            from investigation_agent_platform.domain.provenance.models import SourceLocation

            fallback_location = SourceLocation(system="Git", identifier=source_path or "unknown")
        return CodeFactResolution(
            status=CodeFactStatus.UNRESOLVED,
            repository=repository or "unknown",
            revision=revision or None,
            declaring_type=declaring_type or "unknown",
            field_name=field_name or "unknown",
            field_type="unknown",
            field_category="INSTANCE_FIELD",
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            source_evidence_ids=evidence_ids,
            declaration_location=fallback_location,
            limitation_codes=(code,),
        )

    if reason:
        return unresolved(reason)
    assert location is not None
    if not field_name or not field_name.strip():
        return unresolved("missing_field_name")
    if not source_path.lower().endswith(".java"):
        return unresolved("not_a_java_source_file")
    if not repository:
        return unresolved("repository_identity_unavailable")

    from investigation_agent_platform.application.analysis.java import model as _model

    window_text = "\n".join(line.text for line in lines)
    try:
        code_model = _model.build_java_model(
            window_text.encode("utf-8"),
            file_path=source_path,
            repository=repository,
            revision=revision,
            language_version=language_version,
        )
    except Exception as exc:
        return unresolved(f"java_parser_unavailable:{type(exc).__name__}")
    if code_model.has_syntax_error:
        return unresolved("java_syntax_error_or_incomplete_source_window")

    declarations = [
        field
        for field in code_model.fields
        if field.name == field_name and field.declared_in == "class_declaration"
    ]
    if declaring_type:
        declarations = [
            item
            for item in declarations
            if _p.matches_declaring_type(item.enclosing_class or "", declaring_type)
        ]
    if not declarations:
        return unresolved("field_not_found")
    if len(declarations) > 1:
        return unresolved("ambiguous_field_declaration")

    declaration = declarations[0]
    if not declaration.has_declared_type:
        return unresolved("field_type_unavailable")
    field_type = declaration.field_type
    field_category: Literal["STATIC_FIELD", "INSTANCE_FIELD"] = (
        "STATIC_FIELD" if declaration.is_static else "INSTANCE_FIELD"
    )
    raw_initializer = declaration.raw_initializer
    if raw_initializer is None:
        value, semantics = _language_default(field_type)
        if value is None or semantics is None:
            return unresolved("unsupported_default_type")
    else:
        evaluated = _model.coerce_evaluated(declaration.evaluated_kind, declaration.evaluated_value)
        limitation = declaration.evaluation_limitation
        if limitation is not None or evaluated is None:
            return CodeFactResolution(
                status=CodeFactStatus.PARTIAL,
                repository=repository,
                revision=revision,
                declaring_type=declaration.enclosing_class or "unknown",
                field_name=field_name,
                field_type=field_type,
                field_category=field_category,
                value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
                raw_initializer=raw_initializer,
                source_evidence_ids=evidence_ids,
                declaration_location=location,
                limitation_codes=(limitation or "unsupported_initializer_expression",),
            )
        value, format_limitation = _expr.format_for_field_type(evaluated, field_type)
        if value is None:
            return CodeFactResolution(
                status=CodeFactStatus.PARTIAL,
                repository=repository,
                revision=revision,
                declaring_type=declaration.enclosing_class or "unknown",
                field_name=field_name,
                field_type=field_type,
                field_category=field_category,
                value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
                raw_initializer=raw_initializer,
                source_evidence_ids=evidence_ids,
                declaration_location=location,
                limitation_codes=(format_limitation or "unsupported_initializer_expression",),
            )
        semantics = CodeValueSemantics.EXPLICIT_INITIALIZER

    first_line = lines[0].number
    declaration_location = location.model_copy(
        update={
            "line_start": first_line + declaration.span.line_start - 1,
            "line_end": first_line + declaration.span.line_end - 1,
        }
    )
    return CodeFactResolution(
        status=CodeFactStatus.RESOLVED,
        repository=repository,
        revision=revision,
        declaring_type=declaration.enclosing_class or "unknown",
        field_name=field_name,
        field_type=field_type,
        field_category=field_category,
        value_semantics=semantics,
        raw_initializer=raw_initializer,
        resolved_value=value,
        source_evidence_ids=evidence_ids,
        declaration_location=declaration_location,
        node_id=None,
        parser_version=_p.parser_version(),
    )
