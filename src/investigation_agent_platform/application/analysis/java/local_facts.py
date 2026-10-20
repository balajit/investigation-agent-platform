# src/investigation_agent_platform/application/analysis/java/local_facts.py
"""Deterministic Java local-variable and parameter resolution (Phase 2).

Locals have no language defaults: a declaration without a supported
initializer is UNRESOLVED, never defaulted. Results are versioned
``CodeFactResolutionV2`` facts scoped to their enclosing method.
"""

from __future__ import annotations

from typing import Any

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

_LOCAL_TYPES = frozenset({"local_variable_declaration"})
_PARAM_TYPES = frozenset({"formal_parameter", "spread_parameter"})


def _unresolved(
    repository: str,
    revision: str | None,
    subject: str,
    code: str,
    evidence_ids: tuple[Any, ...],
    language_version: str | None,
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
        parser_version=_p.parser_version(),
    )


def _enclosing_method(node: Any, source: bytes) -> tuple[str, Any | None]:
    current = node.parent
    while current is not None:
        if current.type in {"method_declaration", "constructor_declaration"}:
            name = current.child_by_field_name("name")
            label = _p.node_text(name, source) if name is not None else "anonymous"
            return (label, current)
        current = current.parent
    return ("", None)


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

    source_bytes = "\n".join(line.text for line in lines).encode("utf-8")
    try:
        tree = _p.parse_java(source_bytes)
    except Exception as exc:
        return _unresolved(
            repository,
            revision,
            subject,
            f"java_parser_unavailable:{type(exc).__name__}",
            evidence_ids,
            level_text,
        )
    if tree.root_node.has_error:
        return _unresolved(
            repository,
            revision,
            subject,
            "java_syntax_error_or_incomplete_source_window",
            evidence_ids,
            level_text,
        )

    package = _p.package_name(tree.root_node, source_bytes)
    candidates: list[tuple[Any, Any, str, str]] = []
    for node in _p.walk(tree.root_node):
        if node.type in _LOCAL_TYPES:
            declarators = [c for c in node.children if c.type == "variable_declarator"]
            kind = "local"
        elif node.type in _PARAM_TYPES:
            declarators = [node]
            kind = "parameter"
        else:
            continue
        for declarator in declarators:
            name_node = declarator.child_by_field_name("name")
            if name_node is None or _p.node_text(name_node, source_bytes) != symbol_name:
                continue
            method_label, _ = _enclosing_method(node, source_bytes)
            if method_scope and not _p.matches_declaring_type(method_label, method_scope):
                continue
            candidates.append((node, declarator, kind, method_label))

    if len(candidates) != 1:
        code = "local_not_found" if not candidates else "ambiguous_local_declaration"
        if method_scope and candidates:
            code = "ambiguous_local_declaration"
        result = _unresolved(repository, revision, subject, code, evidence_ids, level_text)
        return result

    node, declarator, kind, method_label = candidates[0]
    type_node = node.child_by_field_name("type")
    declared_type = (
        _p.node_text(type_node, source_bytes).strip() if type_node is not None else "unknown"
    )
    value_node = declarator.child_by_field_name("value")
    raw_initializer = (
        _p.node_text(value_node, source_bytes).strip() if value_node is not None else None
    )
    qualified_subject = f"{method_label}.{symbol_name}" if method_label else symbol_name
    if package:
        qualified_subject = f"{package}.{qualified_subject}"
    first_line = lines[0].number
    declaration_location = location.model_copy(
        update={
            "line_start": first_line + node.start_point[0],
            "line_end": first_line + node.end_point[0],
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

    evaluated, limitation = _expr.evaluate(value_node, source_bytes, level)
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
