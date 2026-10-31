# src/investigation_agent_platform/application/analysis/java/local_facts.py
"""Deterministic Java local-variable and parameter resolution (Phase 2).

Locals have no language defaults: a declaration without a supported
initializer is UNRESOLVED, never defaulted. Results are versioned
``CodeFactResolutionV2`` facts scoped to their enclosing method.
"""

from __future__ import annotations

from typing import Any

from investigation_agent_platform.application.analysis.failure import failure_code
from investigation_agent_platform.application.analysis.java import _parse as _p
from investigation_agent_platform.application.analysis.java import expressions as _expr
from investigation_agent_platform.application.analysis.java import literals as _lit
from investigation_agent_platform.application.analysis.java._provenance import (
    java_source_lines,
    repository_of,
)
from investigation_agent_platform.domain.code.models import (
    CodeFactResolutionV2,
    CodeFactStatus,
    CodeValueSemantics,
    JavaFactCategory,
    SemanticStage,
)


def _unresolved(
    repository: str,
    revision: str | None,
    subject: str,
    code: str,
    evidence_ids: tuple[Any, ...],
    language_version: str | None,
    exc: BaseException | None = None,
) -> CodeFactResolutionV2:
    from investigation_agent_platform.domain.provenance.models import SourceLocation

    return CodeFactResolutionV2(
        status=CodeFactStatus.UNRESOLVED,
        repository=repository or "unknown",
        revision=revision or None,
        fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
        semantic_stage=SemanticStage.DECLARATION,
        declaring_type="unknown",
        subject_identity=subject or "unknown",
        value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
        language_version=language_version,
        analysis_method="tree-sitter-range-v1",
        source_evidence_ids=tuple(e for e in evidence_ids if e is not None),
        declaration_location=SourceLocation(system="Git", identifier="unknown"),
        limitation_codes=(code,),
        assumptions=() if exc is None else (f"exception_type={type(exc).__name__}",),
        parser_version=_p.parser_version(),
    )


def resolve_java_local_fact(
    evidence: Any,
    symbol_name: str,
    *,
    method_scope: str | None = None,
    language_version: object = None,
) -> CodeFactResolutionV2:
    """Resolve one local variable or parameter from source-read evidence."""
    lines, reason = java_source_lines(evidence)
    provenance = getattr(evidence, "provenance", None)
    location = getattr(provenance, "source_location", None)
    source_path = str(getattr(location, "file_path", "") or "")
    revision = str(getattr(location, "revision", "") or "")
    identifier = str(getattr(location, "identifier", "") or "")
    repository = repository_of(identifier, source_path)
    evidence_id = getattr(evidence, "evidence_id", None)
    evidence_ids = (evidence_id,) if evidence_id is not None else ()
    level = _lit.normalize_source_level(language_version)
    level_text = str(level) if level is not None else None
    subject = symbol_name or "unknown"

    if reason:
        return _unresolved(repository, revision, subject, reason, evidence_ids, level_text)
    assert location is not None
    if not symbol_name or not symbol_name.strip():
        return _unresolved(
            repository, revision, subject, "missing_symbol_name", evidence_ids, level_text
        )
    if not source_path.lower().endswith(".java"):
        return _unresolved(
            repository, revision, subject, "not_a_java_source_file", evidence_ids, level_text
        )
    if not repository:
        return _unresolved(
            repository,
            revision,
            subject,
            "repository_identity_unavailable",
            evidence_ids,
            level_text,
        )

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
        return _unresolved(
            repository,
            revision,
            subject,
            failure_code(exc),
            evidence_ids,
            level_text,
            exc=exc,
        )
    if code_model.has_syntax_error:
        return _unresolved(
            repository,
            revision,
            subject,
            "java_syntax_error_or_incomplete_source_window",
            evidence_ids,
            level_text,
        )

    package = code_model.package
    candidates: list[tuple[Any, str, str]] = []
    for local in code_model.locals:
        if local.name != symbol_name:
            continue
        if method_scope and not _p.matches_declaring_type(local.method_name, method_scope):
            continue
        candidates.append((local, "local", local.method_name))
    for param in code_model.params:
        if param.is_varargs or param.name != symbol_name:
            continue
        if method_scope and not _p.matches_declaring_type(param.method_name, method_scope):
            continue
        candidates.append((param, "parameter", param.method_name))

    if len(candidates) != 1:
        code = "local_not_found" if not candidates else "ambiguous_local_declaration"
        if method_scope and candidates:
            code = "ambiguous_local_declaration"
        result = _unresolved(repository, revision, subject, code, evidence_ids, level_text)
        return result

    record, kind, method_label = candidates[0]
    declared_type = record.var_type
    if kind == "parameter":
        # Parameters carry no initializer in the model (mirrors the resolver,
        # which reads the declarator itself as the parameter node).
        raw_initializer = None
        evaluated = None
        limitation = None
    else:
        raw_initializer = record.raw_initializer
        evaluated = _model.coerce_evaluated(record.evaluated_kind, record.evaluated_value)
        limitation = record.evaluation_limitation
    qualified_subject = f"{method_label}.{symbol_name}" if method_label else symbol_name
    if package:
        qualified_subject = f"{package}.{qualified_subject}"
    first_line = lines[0].number
    declaration_location = location.model_copy(
        update={
            "line_start": first_line + record.span.line_start - 1,
            "line_end": first_line + record.span.line_end - 1,
        }
    )

    if raw_initializer is None:
        code = "parameter_value_unknown" if kind == "parameter" else "uninitialized_local"
        return CodeFactResolutionV2(
            status=CodeFactStatus.UNRESOLVED,
            repository=repository,
            revision=revision,
            fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            declaring_type=method_label or "unknown",
            subject_identity=qualified_subject,
            field_name=symbol_name,
            field_type=declared_type,
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            language_version=level_text,
            analysis_method="tree-sitter-range-v1",
            raw_initializer=None,
            source_evidence_ids=evidence_ids,
            declaration_location=declaration_location,
            limitation_codes=(code,),
            parser_version=_p.parser_version(),
        )

    evaluated = _model.coerce_evaluated(record.evaluated_kind, record.evaluated_value)
    limitation = record.evaluation_limitation
    if limitation is not None or evaluated is None:
        return CodeFactResolutionV2(
            status=CodeFactStatus.PARTIAL,
            repository=repository,
            revision=revision,
            fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            declaring_type=method_label or "unknown",
            subject_identity=qualified_subject,
            field_name=symbol_name,
            field_type=declared_type,
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            language_version=level_text,
            analysis_method="tree-sitter-range-v1",
            raw_initializer=raw_initializer,
            source_evidence_ids=evidence_ids,
            declaration_location=declaration_location,
            limitation_codes=(limitation or "unsupported_initializer_expression",),
            parser_version=_p.parser_version(),
        )
    formatted, format_limitation = _expr.format_for_field_type(evaluated, declared_type)
    if formatted is None:
        return CodeFactResolutionV2(
            status=CodeFactStatus.PARTIAL,
            repository=repository,
            revision=revision,
            fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            declaring_type=method_label or "unknown",
            subject_identity=qualified_subject,
            field_name=symbol_name,
            field_type=declared_type,
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            language_version=level_text,
            analysis_method="tree-sitter-range-v1",
            raw_initializer=raw_initializer,
            source_evidence_ids=evidence_ids,
            declaration_location=declaration_location,
            limitation_codes=(format_limitation or "unsupported_initializer_expression",),
            parser_version=_p.parser_version(),
        )
    _ = kind
    return CodeFactResolutionV2(
        status=CodeFactStatus.RESOLVED,
        repository=repository,
        revision=revision,
        fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
        semantic_stage=SemanticStage.DECLARATION,
        declaring_type=method_label or "unknown",
        subject_identity=qualified_subject,
        field_name=symbol_name,
        field_type=declared_type,
        value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
        language_version=level_text,
        analysis_method="tree-sitter-range-v1",
        raw_initializer=raw_initializer,
        resolved_value=formatted,
        source_evidence_ids=evidence_ids,
        declaration_location=declaration_location,
        parser_version=_p.parser_version(),
    )
