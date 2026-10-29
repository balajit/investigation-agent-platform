# src/investigation_agent_platform/application/analysis/java/window_facts.py
"""Bank-time enumeration of Java source-value facts (single home).

When a range read banks Java source without selector-driven analysis,
enumerate every cleanly-declared guarded method-return literal and
every cleanly-declared class-field value in the window as RESOLVED v2
records. Verify-time selection picks by subject — no method_name /
field_name parameter, no forcing turn, and no per-case trigger code is
ever needed to make analysis happen.

Soundness, not intent: a record attaches only when the resolved
declaration's own syntax subtree is error-free. Errors elsewhere in
the window (an unclosed sibling past the provider's byte cap) never
contribute records, and the proof's revision, path, window, subject,
and value checks still bind every record. Anything unparseable yields
no records, never partial rows.
"""

from __future__ import annotations

from typing import Any

from investigation_agent_platform.application.analysis.java import _parse as _p
from investigation_agent_platform.application.analysis.java import expressions as _expr
from investigation_agent_platform.application.analysis.java import model as _model
from investigation_agent_platform.application.analysis.java._provenance import (
    java_source_lines,
    repository_of,
)
from investigation_agent_platform.application.analysis.java.field_facts import (
    _language_default,
)
from investigation_agent_platform.domain.code.models import (
    CodeFactResolutionV2,
    CodeFactStatus,
    CodeValueSemantics,
    JavaFactCategory,
)

MAX_WINDOW_RECORDS = 25


def _declaration_subtree_clean(tree: Any, node_type: str, line_start: int, line_end: int) -> bool:
    """True when the declaration spanning those window-relative lines parses clean."""
    try:
        for node in _p.walk(tree.root_node):
            if (
                node.type == node_type
                and int(node.start_point[0]) + 1 == line_start
                and int(node.end_point[0]) + 1 == line_end
            ):
                return not bool(getattr(node, "has_error", False))
        return False
    except Exception:
        return False


def enumerate_java_window_facts(
    evidence: Any, *, language_version: object = None
) -> list[CodeFactResolutionV2]:
    """Enumerate RESOLVED source-value facts for one banked Java read (pure)."""
    try:
        lines, reason = java_source_lines(evidence)
        if reason:
            return []
        provenance = getattr(evidence, "provenance", None)
        location = getattr(provenance, "source_location", None)
        source_path = str(getattr(location, "file_path", "") or "")
        revision = str(getattr(location, "revision", "") or "")
        identifier = str(getattr(location, "identifier", "") or "")
        if not source_path.lower().endswith(".java"):
            return []
        if not revision or revision in {"HEAD", "UNPINNED"}:
            return []
        repository = repository_of(identifier, source_path)
        evidence_id = getattr(evidence, "evidence_id", None)
        evidence_ids = (evidence_id,) if evidence_id is not None else ()
        source_bytes = "\n".join(line.text for line in lines).encode("utf-8")
        try:
            tree = _p.parse_java(source_bytes)
        except Exception:
            return []
        try:
            code_model = _model.build_java_model(
                source_bytes,
                file_path=source_path,
                repository=repository or "unknown",
                revision=revision,
                language_version=language_version,
            )
        except Exception:
            return []
        first_line = lines[0].number
        records: list[CodeFactResolutionV2] = []

        def _shift(span: Any) -> Any:
            return location.model_copy(
                update={
                    "line_start": first_line + span.line_start - 1,
                    "line_end": first_line + span.line_end - 1,
                }
            )

        package = code_model.package or ""
        # Unclosed-tail windows (provider byte cap) break the enclosing
        # class node, leaving declarations unattributed. Mirror the
        # selector resolver's basename fallback: when the window names no
        # class at all, the file stem names it. Multi-class windows keep
        # failing closed (no guess across classes).
        stem = source_path.rsplit("/", 1)[-1]
        if "." in stem:
            stem = stem.rsplit(".", 1)[0]
        class_fallback = stem if not code_model.classes and stem else ""
        for branch in code_model.guard_returns:
            host = _model.method_for_identity(
                code_model.methods, branch.method_identity, branch.span
            )
            if host is None or host.is_constructor:
                continue
            if not _declaration_subtree_clean(
                tree, "method_declaration", host.span.line_start, host.span.line_end
            ):
                continue
            class_name = host.nearest_class or host.enclosing_class or class_fallback
            qualified = (
                f"{package}.{class_name}" if package and class_name else (class_name or package)
            )
            subject = f"{qualified}.{host.name}" if qualified else host.name
            for ret in branch.returns:
                value = _model.literal_return_value(ret.value_shape, ret.value_text)
                if value is None:
                    continue
                records.append(
                    CodeFactResolutionV2(
                        status=CodeFactStatus.RESOLVED,
                        repository=repository or None,
                        revision=revision,
                        fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
                        declaring_type=qualified or None,
                        subject_identity=subject or None,
                        value_semantics=CodeValueSemantics.GUARDED_RETURN_LITERAL,
                        raw_initializer=ret.raw_text[:512],
                        resolved_value=value,
                        source_evidence_ids=evidence_ids,
                        declaration_location=_shift(ret.span),
                        parser_version=_p.parser_version(),
                    )
                )
                if len(records) >= MAX_WINDOW_RECORDS:
                    return records
        for field in code_model.fields:
            if field.declared_in != "class_declaration" or not field.has_declared_type:
                continue
            if not _declaration_subtree_clean(
                tree, "field_declaration", field.span.line_start, field.span.line_end
            ):
                continue
            raw_initializer = field.raw_initializer
            if raw_initializer is None:
                value, semantics = _language_default(field.field_type)
                if value is None or semantics is None:
                    continue
            else:
                evaluated = _model.coerce_evaluated(field.evaluated_kind, field.evaluated_value)
                if field.evaluation_limitation is not None or evaluated is None:
                    continue
                value, _ = _expr.format_for_field_type(evaluated, field.field_type)
                if value is None:
                    continue
                semantics = CodeValueSemantics.EXPLICIT_INITIALIZER
            enclosing = field.enclosing_class or class_fallback
            subject = f"{enclosing}.{field.name}" if enclosing else field.name
            records.append(
                CodeFactResolutionV2(
                    status=CodeFactStatus.RESOLVED,
                    repository=repository or None,
                    revision=revision,
                    fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
                    declaring_type=enclosing or None,
                    subject_identity=subject or None,
                    field_name=field.name,
                    field_type=field.field_type,
                    field_category=("STATIC_FIELD" if field.is_static else "INSTANCE_FIELD"),
                    value_semantics=semantics,
                    raw_initializer=raw_initializer,
                    resolved_value=value,
                    source_evidence_ids=evidence_ids,
                    declaration_location=_shift(field.span),
                    parser_version=_p.parser_version(),
                )
            )
            if len(records) >= MAX_WINDOW_RECORDS:
                return records
        return records
    except Exception:
        return []
