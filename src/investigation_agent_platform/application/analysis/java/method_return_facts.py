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
        # GET_CODE windows are method-anchored but often run through the
        # enclosing class's closing brace (live L5 banks 6-19: method 6-18
        # plus the lone class `}` on 19), which tree-sitter-java rejects as
        # a dangling close. Retry with trailing brace-only lines stripped
        # (bounded); the method body itself is never touched — a window
        # whose error lies elsewhere stays UNRESOLVED with the same code.
        stripped = list(lines)
        fixed = False
        for _ in range(3):
            if not stripped or stripped[-1].text.strip() != "}":
                break
            stripped = stripped[:-1]
            try:
                candidate = _p.parse_java("\n".join(line.text for line in stripped).encode("utf-8"))
            except Exception:
                break
            if not candidate.root_node.has_error:
                tree = candidate
                lines = stripped
                source_bytes = "\n".join(line.text for line in lines).encode("utf-8")
                fixed = True
                break
        if not fixed:
            return unresolved("java_syntax_error_or_incomplete_source_window")

    # Function-local to match the resolver-wide convention (method_facts <-> model would cycle if hoisted).
    from investigation_agent_platform.application.analysis.java import model as _model

    code_model = _model.build_java_model(
        source_bytes,
        file_path=source_path,
        repository=repository,
        revision=revision,
    )
    if code_model.has_syntax_error:
        # Unreachable after the retry above (it guarantees a clean tree),
        # retained as defense in depth.
        return unresolved("java_syntax_error_or_incomplete_source_window")

    package = code_model.package
    stripped = method_name.strip()
    methods = [m for m in code_model.methods if m.name == stripped and not m.is_constructor]
    if declaring_type:
        methods = [
            m
            for m in methods
            if _p.matches_declaring_type(m.nearest_class or "", declaring_type)
            or _p.matches_declaring_type(f"{package}.{m.nearest_class or ''}", declaring_type)
        ]
    if not methods and declaring_type:
        # Method-anchored windows carry no enclosing class, so the
        # declaring-type filter above matches nothing (live L5 banks 6-19:
        # the method without its class). Fall back to the evidence file's
        # basename stem: LegacyCodeMapper.java corroborates declaring type
        # LegacyCodeMapper. Still requires exactly one same-named method in
        # the window — overloads and wrong-file windows stay unresolved.
        stem = source_path.rsplit("/", 1)[-1]
        if "." in stem:
            stem = stem.rsplit(".", 1)[0]
        requested = declaring_type.rsplit(".", 1)[-1]
        if stem and requested and stem.casefold() == requested.casefold():
            methods = [m for m in code_model.methods if m.name == stripped and not m.is_constructor]
    if not methods:
        return unresolved("method_not_found")
    if len(methods) > 1:
        return unresolved("ambiguous_method_declaration")

    method = methods[0]
    class_name = method.nearest_class or ""
    qualified = f"{package}.{class_name}" if package and class_name else (class_name or package)
    _mname = method_name.strip()
    if qualified:
        subject = f"{qualified}.{_mname}"
    elif declaring_type and declaring_type.strip():
        # Method-anchored windows carry no package/class context, so the
        # resolved subject would be the bare method name — which no
        # declaring-type claim target identifies (live L5). Qualify with the
        # requested declaring type; the basename fallback above already tied
        # the method to the right file.
        subject = f"{declaring_type.strip()}.{_mname}"
    else:
        subject = _mname
    first_line = lines[0].number
    want_guard = _normalize(guard_condition) if guard_condition else ""

    for branch in code_model.guard_returns:
        if branch.method_identity != f"{method.qualified_name}#{method.key}":
            continue
        if want_guard and want_guard not in branch.guard_text:
            continue
        for ret in branch.returns:
            value = _model.literal_return_value(ret.value_shape, ret.value_text)
            if value is None:
                continue
            ret_location = location.model_copy(
                update={
                    "line_start": first_line + ret.span.line_start - 1,
                    "line_end": first_line + ret.span.line_end - 1,
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
                raw_initializer=ret.raw_text[:512],
                resolved_value=value,
                source_evidence_ids=evidence_ids,
                declaration_location=ret_location,
                parser_version=_p.parser_version(),
            )
    return unresolved("guard_return_not_found")
