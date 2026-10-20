# src/investigation_agent_platform/application/analysis/java/method_return_facts.py
"""Deterministic Java guarded-return literal resolution.

Covers method-return-on-guard claims (e.g. "if orderType is null,
LegacyCodeMapper.normalize returns 'STANDARD'") that the field-only
pipeline can never resolve: the subject is a method's guarded ``return``
literal, not a field initializer or language default. Emits V2
``SOURCE_VALUE_FACT`` payloads consumed by ``java_fact_proof``.
"""

from __future__ import annotations

import re
from typing import Any

from investigation_agent_platform.application.analysis.java import _parse as _p
from investigation_agent_platform.application.analysis.java._provenance import (
    java_source_lines,
    repository_of,
)
from investigation_agent_platform.domain.code.models import (
    CodeFactResolutionV2,
    CodeFactStatus,
    CodeValueSemantics,
    JavaFactCategory,
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _literal_value(node: Any, source: bytes) -> str | None:
    """Unquote a simple literal return expression; None when not a literal."""
    if node is None:
        return None
    if node.type == "string_literal":
        text = _p.node_text(node, source).strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
            return text[1:-1]
        return None
    if node.type in {"true", "false", "null"}:
        return _p.node_text(node, source).strip()
    if node.type in {
        "decimal_integer_literal",
        "hex_integer_literal",
        "octal_integer_literal",
        "binary_integer_literal",
        "decimal_floating_point_literal",
        "hex_floating_point_literal",
        "character_literal",
    }:
        return _p.node_text(node, source).strip()
    if node.type == "unary_expression" and node.child_count == 2:
        text = _p.node_text(node, source).strip()
        if re.fullmatch(r"[-+][^-+].*", text or ""):
            return text
        return None
    return None


def _iter_method_declarations(root: Any, source: bytes, method_name: str) -> Any:
    for node in _p.walk(root):
        if node.type != "method_declaration":
            continue
        name_node = node.child_by_field_name("name")
        if name_node is not None and _p.node_text(name_node, source).strip() == method_name:
            yield node


def _enclosing_class(node: Any, source: bytes) -> str | None:
    parent = node.parent
    while parent is not None:
        if parent.type == "class_declaration":
            name_node = parent.child_by_field_name("name")
            if name_node is not None:
                return _p.node_text(name_node, source).strip() or None
            return None
        parent = parent.parent
    return None


def resolve_java_method_return_fact(
    evidence: Any,
    method_name: str,
    *,
    guard_condition: str | None = None,
    declaring_type: str | None = None,
) -> CodeFactResolutionV2:
    """Resolve a guarded ``return`` literal of one Java method from source evidence.

    Reads no files; the only source of facts is the supplied,
    revision-pinned ``GET_CODE`` evidence. Finds ``method_declaration``
    ``method_name`` (optionally inside ``declaring_type``), then an
    ``if`` whose condition contains ``guard_condition`` (whitespace
    normalized), then a literal ``return`` in that branch. Emits a
    RESOLVED V2 ``SOURCE_VALUE_FACT`` with
    ``subject_identity="<declaring_type>.<method>"``; anything else is
    UNRESOLVED with a ``limitation_codes`` entry (V2 requires codes for
    non-RESOLVED). Without ``guard_condition`` the first literal return
    of any guard branch in the method resolves.
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

    def unresolved(code: str) -> CodeFactResolutionV2:
        return CodeFactResolutionV2(
            status=CodeFactStatus.UNRESOLVED,
            repository=repository or None,
            revision=revision or None,
            fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
            declaring_type=declaring_type,
            subject_identity=(
                f"{declaring_type}.{method_name}" if declaring_type else method_name or None
            ),
            value_semantics=CodeValueSemantics.GUARDED_RETURN_LITERAL,
            source_evidence_ids=evidence_ids,
            parser_version=_p.parser_version(),
            limitation_codes=(code,),
        )

    if reason:
        return unresolved(reason)
    assert location is not None
    if not method_name or not method_name.strip():
        return unresolved("missing_method_name")
    if not source_path.lower().endswith(".java"):
        return unresolved("not_a_java_source_file")
    if not repository:
        return unresolved("repository_identity_unavailable")
    if not revision or revision in {"HEAD", "UNPINNED"}:
        return unresolved("unpinned_revision")

    source_bytes = "\n".join(line.text for line in lines).encode("utf-8")
    try:
        tree = _p.parse_java(source_bytes)
    except Exception as exc:
        return unresolved(f"java_parser_unavailable:{type(exc).__name__}")
    if tree.root_node.has_error:
        return unresolved("java_syntax_error_or_incomplete_source_window")

    package = _p.package_name(tree.root_node, source_bytes)
    methods = list(_iter_method_declarations(tree.root_node, source_bytes, method_name.strip()))
    if declaring_type:
        methods = [
            m
            for m in methods
            if _p.matches_declaring_type(
                _enclosing_class(m, source_bytes) or "", declaring_type
            )
            or _p.matches_declaring_type(
                f"{package}.{(_enclosing_class(m, source_bytes) or '')}", declaring_type
            )
        ]
    if not methods:
        return unresolved("method_not_found")
    if len(methods) > 1:
        return unresolved("ambiguous_method_declaration")

    method = methods[0]
    class_name = _enclosing_class(method, source_bytes) or ""
    qualified = (
        f"{package}.{class_name}" if package and class_name else (class_name or package)
    )
    subject = f"{qualified}.{method_name.strip()}" if qualified else method_name.strip()
    first_line = lines[0].number
    want_guard = _normalize(guard_condition) if guard_condition else ""

    for node in _p.walk(method):
        if node.type != "if_statement":
            continue
        if want_guard:
            cond = node.child_by_field_name("condition")
            if want_guard not in _normalize(
                _p.node_text(cond, source_bytes) if cond is not None else ""
            ):
                continue
        consequence = node.child_by_field_name("consequence")
        if consequence is None:
            continue
        for inner in _iter_branch_returns(consequence):
            value = _literal_value(_return_value(inner), source_bytes)
            if value is None:
                continue
            ret_location = location.model_copy(
                update={
                    "line_start": first_line + inner.start_point[0],
                    "line_end": first_line + inner.end_point[0],
                }
            )
            return CodeFactResolutionV2(
                status=CodeFactStatus.RESOLVED,
                repository=repository,
                revision=revision,
                fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
                declaring_type=qualified or declaring_type,
                subject_identity=subject,
                value_semantics=CodeValueSemantics.GUARDED_RETURN_LITERAL,
                raw_initializer=_p.node_text(inner, source_bytes).strip()[:512],
                resolved_value=value,
                source_evidence_ids=evidence_ids,
                declaration_location=ret_location,
                parser_version=_p.parser_version(),
            )
    return unresolved("guard_return_not_found")


def _return_value(return_node: Any) -> Any:
    """The returned expression: first named child of ``return <expr>;``."""
    for child in return_node.children:
        if child.is_named:
            return child
    return None


def _iter_branch_returns(consequence: Any) -> Any:
    """Yield ``return_statement`` nodes of one guard branch.

    Descends into blocks but not into nested ``if_statement`` subtrees
    (their returns belong to a narrower guard) — the alternative
    (``else``) branch is never entered since only the consequence is
    walked.
    """
    if consequence.type == "return_statement":
        yield consequence
        return
    stack = [consequence]
    while stack:
        node = stack.pop()
        for child in node.children:
            if child.type == "if_statement":
                continue
            if child.type == "return_statement":
                yield child
            elif child.is_named:
                stack.append(child)
