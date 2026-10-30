# src/investigation_agent_platform/application/analysis/python/scope_facts.py
"""Python assignment and parameter-default resolution (range-pinned, no execution)."""

from __future__ import annotations

from typing import Any

from investigation_agent_platform.application.analysis.python import _parse as _p
from investigation_agent_platform.application.analysis.python import expressions as _expr
from investigation_agent_platform.application.analysis.python._provenance import (
    python_source_lines,
    repository_of,
)
from investigation_agent_platform.domain.code.models import (
    CodeFactResolutionV2,
    CodeFactStatus,
    CodeValueSemantics,
    JavaFactCategory,
    SemanticStage,
)

_COMPREHENSION_TYPES = frozenset(
    {
        "list_comprehension",
        "set_comprehension",
        "dictionary_comprehension",
        "generator_expression",
    }
)


def _unresolved(
    repository: str,
    revision: str | None,
    subject: str,
    code: str,
    evidence_ids: tuple[Any, ...],
    language_version: str | None,
    semantics: CodeValueSemantics = CodeValueSemantics.EXPLICIT_INITIALIZER,
) -> CodeFactResolutionV2:
    from investigation_agent_platform.domain.provenance.models import SourceLocation

    short = (subject or "").rsplit(".", 1)[-1] or "unknown"
    scope = (subject or "").rsplit(".", 1)[0] or None
    return CodeFactResolutionV2(
        status=CodeFactStatus.UNRESOLVED,
        repository=repository or "unknown",
        revision=revision or None,
        fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
        semantic_stage=SemanticStage.DECLARATION,
        declaring_type=scope or "unknown",
        field_name=short if short != "unknown" else "unknown",
        subject_identity=subject or "unknown",
        value_semantics=semantics,
        language_version=language_version,
        analysis_method="tree-sitter-range-v1",
        source_evidence_ids=tuple(e for e in evidence_ids if e is not None),
        declaration_location=SourceLocation(system="Git", identifier="unknown"),
        limitation_codes=(code,),
        parser_version=_p.parser_version(),
    )


def _partial(
    repository: str,
    revision: str,
    subject: str,
    code: str,
    evidence_ids: tuple[Any, ...],
    language_version: str | None,
    location: Any,
    semantics: CodeValueSemantics = CodeValueSemantics.EXPLICIT_INITIALIZER,
    raw: str | None = None,
) -> CodeFactResolutionV2:
    short = subject.rsplit(".", 1)[-1]
    scope = subject.rsplit(".", 1)[0] or None
    return CodeFactResolutionV2(
        status=CodeFactStatus.PARTIAL,
        repository=repository,
        revision=revision,
        fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
        semantic_stage=SemanticStage.DECLARATION,
        declaring_type=scope,
        field_name=short,
        subject_identity=subject,
        value_semantics=semantics,
        language_version=language_version,
        analysis_method="tree-sitter-range-v1",
        raw_initializer=raw,
        source_evidence_ids=evidence_ids,
        declaration_location=location,
        limitation_codes=(code,),
        parser_version=_p.parser_version(),
    )


def _scope_of(node: Any) -> str:
    """Dotted scope (Class.func) enclosing a node; '' means module level."""
    parts: list[str] = []
    current = node.parent
    while current is not None:
        if current.type in {"function_definition", "async_function_definition", "class_definition"}:
            name = current.child_by_field_name("name")
            if name is not None:
                # Reconstruct source-free: walk needs bytes; use raw traversal placeholder.
                parts.append(f"<{current.type}>")
            else:
                parts.append(f"<{current.type}>")
        current = current.parent
    return ".".join(reversed(parts))


def _scope_of(node: Any, source: bytes) -> str:
    """Dotted enclosing scope (function/class chain); ``""`` at module level."""
    parts: list[str] = []
    cur = node.parent
    while cur is not None:
        if cur.type in {"function_definition", "async_function_definition", "class_definition"}:
            nm = cur.child_by_field_name("name")
            parts.append(_p.node_text(nm, source).strip() if nm is not None else "?")
        cur = cur.parent
    parts.reverse()
    return ".".join(parts)


def _in_comprehension(node: Any) -> bool:
    current = node.parent
    while current is not None:
        if current.type in _COMPREHENSION_TYPES:
            return True
        if current.type in {
            "function_definition",
            "async_function_definition",
            "class_definition",
            "module",
        }:
            return False
        current = current.parent
    return False


def _assignment_targets(left: Any, source: bytes) -> list[str] | None:
    """Return bound plain names, or None when the target is not a plain name list."""
    if left.type == "identifier":
        return [_p.node_text(left, source).strip()]
    if left.type in {"pattern_list", "expression_list", "tuple_pattern", "list_pattern"}:
        names: list[str] = []
        for child in left.children:
            if not child.is_named:
                continue
            if child.type != "identifier":
                return None
            names.append(_p.node_text(child, source).strip())
        return names
    return None


def _annotation_text(node: Any, source: bytes) -> str:
    """Source text of an assignment's ``type`` annotation, or ``""``."""
    for child in node.children:
        if child.type == "type":
            return _p.node_text(child, source)
    return ""


def _is_conditional(node: Any) -> bool:
    """True when the node sits under a branch/handler (may not execute)."""
    current = node.parent
    while current is not None:
        if current.type in {
            "if_statement",
            "try_statement",
            "except_clause",
            "with_statement",
            "while_statement",
            "for_statement",
            "match_statement",
        }:
            return True
        if current.type in {
            "function_definition",
            "async_function_definition",
            "class_definition",
            "module",
        }:
            return False
        current = current.parent
    return False


def _collect_rebindings(root: Any, source: bytes, symbol: str) -> list[tuple[Any, str]]:
    """Collect (node, kind) for non-plain writes binding the symbol.

    Kinds: ``augmented`` (``x += …``), ``loop_target`` (``for x in …``),
    ``context_target`` (``except … as x`` / ``with … as x``).
    """
    found: list[tuple[Any, str]] = []
    for node in _p.walk(root):
        if node.type == "augmented_assignment":
            named = [c for c in node.children if c.is_named]
            if (
                named
                and named[0].type == "identifier"
                and _p.node_text(named[0], source).strip() == symbol
            ):
                found.append((node, "augmented"))
        elif node.type == "for_statement":
            seen_for, seen_in = False, False
            for child in node.children:
                if (
                    not seen_for
                    and not child.is_named
                    and _p.node_text(child, source).strip() == "for"
                ):
                    seen_for = True
                    continue
                if (
                    seen_for
                    and not seen_in
                    and not child.is_named
                    and _p.node_text(child, source).strip() == "in"
                ):
                    seen_in = True
                    break
                if seen_for and not seen_in and child.is_named:
                    targets = _assignment_targets(child, source)
                    if targets is not None and symbol in targets:
                        found.append((node, "loop_target"))
        elif node.type in {"except_clause", "with_item"}:
            # `except E as e` / `with open() as f`: the bound name sits in
            # an `as_pattern_target` under an `as_pattern`.
            for sub in _p.walk(node):
                if sub.type == "as_pattern_target":
                    for leaf in sub.children:
                        if (
                            leaf.is_named
                            and leaf.type == "identifier"
                            and _p.node_text(leaf, source).strip() == symbol
                        ):
                            found.append((node, "context_target"))
                            break
                    break
    return found


def _collect_deletions(root: Any, source: bytes, symbol: str) -> list[tuple[Any, bool]]:
    """Collect (node, conditional) for ``del symbol`` statements."""
    found: list[tuple[Any, bool]] = []
    for node in _p.walk(root):
        if node.type != "delete_statement":
            continue
        for child in node.children:
            if (
                child.is_named
                and child.type == "identifier"
                and _p.node_text(child, source).strip() == symbol
            ):
                found.append((node, _is_conditional(node)))
    return found


def _collect_assignments(
    root: Any, source: bytes, symbol: str
) -> list[tuple[Any, Any, str | None, str]]:
    """Collect (assignment_node, value_node_or_None, scope_label, kind)."""
    found: list[tuple[Any, Any, str | None, str]] = []

    def scope_label(node: Any) -> str:
        parts: list[str] = []
        cur = node.parent
        while cur is not None:
            if cur.type in {"function_definition", "async_function_definition", "class_definition"}:
                nm = cur.child_by_field_name("name")
                parts.append(_p.node_text(nm, source).strip() if nm is not None else "?")
            cur = cur.parent
        parts.reverse()
        return ".".join(parts)

    for node in _p.walk(root):
        if node.type != "assignment":
            continue
        if _in_comprehension(node):
            continue
        named = [c for c in node.children if c.is_named]
        if not named:
            continue
        left = named[0]
        # annotated `x: int` without value has only identifier + type children
        has_equals = any(
            (not c.is_named and _p.node_text(c, source).strip() == "=") for c in node.children
        )
        if not has_equals:
            # Bare annotation: record as no-binding candidate.
            if left.type == "identifier" and _p.node_text(left, source).strip() == symbol:
                found.append((node, None, scope_label(node), "annotation_only"))
            continue
        value = named[-1]
        # `InitVar` members are constructor-only parameters, never plain
        # bindings: skip them so no value fact is reported.
        if left.type == "identifier" and "InitVar" in _annotation_text(node, source):
            continue
        targets = _assignment_targets(left, source)
        if targets is None:
            # attribute/subscript/destructuring-complex: only relevant if symbol matches textually
            text = _p.node_text(left, source)
            if symbol in text:
                found.append((node, value, scope_label(node), "complex_target"))
            continue
        if symbol in targets:
            if len(targets) > 1:
                # Unpacked: pair positionally with RHS expression_list.
                rhs_items = (
                    [c for c in value.children if c.is_named]
                    if value.type in {"expression_list", "tuple"}
                    else [value]
                )
                idx = targets.index(symbol)
                if value.type in {"expression_list", "tuple"} and len(rhs_items) == len(targets):
                    found.append((node, rhs_items[idx], scope_label(node), "unpacked"))
                elif value.type in {"expression_list", "tuple"}:
                    found.append((node, value, scope_label(node), "unpacked_arity_mismatch"))
                else:
                    # Chained `a = b = 30`: tree nests; each assignment node carries full RHS.
                    found.append((node, value, scope_label(node), "chained"))
            else:
                found.append((node, value, scope_label(node), "simple"))
    # Chained `a = b = 30` parses as nested assignments; dedupe identical value nodes.
    return found


def resolve_python_value_fact(
    evidence: Any,
    symbol: str,
    *,
    scope: str | None = None,
    language_version: object = None,
) -> CodeFactResolutionV2:
    """Resolve one Python name's assigned value from exact range evidence."""
    from investigation_agent_platform.application.analysis.python import literals as _lit

    lines, reason = python_source_lines(evidence)
    provenance = getattr(evidence, "provenance", None)
    location = getattr(provenance, "source_location", None)
    source_path = str(getattr(location, "file_path", "") or "")
    revision = str(getattr(location, "revision", "") or "")
    identifier = str(getattr(location, "identifier", "") or "")
    repository = repository_of(identifier, source_path)
    evidence_id = getattr(evidence, "evidence_id", None)
    evidence_ids = (evidence_id,) if evidence_id is not None else ()
    version_label, version_assumed = _lit.resolve_language_version(language_version)
    version_assumptions = ("assumed_default_language_version",) if version_assumed else ()

    if reason:
        return _unresolved(
            repository, revision or None, symbol, reason, evidence_ids, version_label
        )
    assert location is not None
    if not symbol or not symbol.strip() or not _lit.is_valid_name(symbol.strip()):
        return _unresolved(
            repository, revision or None, symbol, "missing_symbol_name", evidence_ids, version_label
        )
    symbol = symbol.strip()
    if not source_path.lower().endswith(".py"):
        return _unresolved(
            repository,
            revision or None,
            symbol,
            "not_a_python_source_file",
            evidence_ids,
            version_label,
        )
    if not repository:
        return _unresolved(
            repository,
            revision or None,
            symbol,
            "repository_identity_unavailable",
            evidence_ids,
            version_label,
        )

    source_bytes = "\n".join(line.text for line in lines).encode("utf-8")
    try:
        tree = _p.parse_python(source_bytes)
    except Exception as exc:
        bad = _unresolved(
            repository,
            revision or None,
            symbol,
            f"python_parser_unavailable:{type(exc).__name__}",
            evidence_ids,
            version_label,
        )
        return bad
    if _p.has_syntax_error(tree.root_node):
        return _unresolved(
            repository,
            revision or None,
            symbol,
            "python_syntax_error_or_incomplete_source_window",
            evidence_ids,
            version_label,
        )

    # global/nonlocal for this name anywhere in window forces scope caution.
    for node in _p.walk(tree.root_node):
        if node.type in {"global_statement", "nonlocal_statement"}:
            names = [_p.node_text(c, source_bytes).strip() for c in node.children if c.is_named]
            if symbol in names:
                loc = location.model_copy(
                    update={
                        "line_start": lines[0].number + node.start_point[0],
                        "line_end": lines[0].number + node.end_point[0],
                    }
                )
                return _partial(
                    repository,
                    revision,
                    symbol,
                    "global_nonlocal_scope_boundary",
                    evidence_ids,
                    version_label,
                    loc,
                )

    candidates = _collect_assignments(tree.root_node, source_bytes, symbol)
    if scope:
        candidates = [c for c in candidates if _p.matches_scope(c[2], scope)]
    rebindings = _collect_rebindings(tree.root_node, source_bytes, symbol)
    deletions_all = _collect_deletions(tree.root_node, source_bytes, symbol)
    if scope:
        rebindings = [
            (n, k) for n, k in rebindings if _p.matches_scope(_scope_of(n, source_bytes), scope)
        ]
        deletions_all = [
            (n, c) for n, c in deletions_all if _p.matches_scope(_scope_of(n, source_bytes), scope)
        ]
    if not candidates and not rebindings and not deletions_all:
        return _unresolved(
            repository,
            revision or None,
            symbol,
            "symbol_not_in_window",
            evidence_ids,
            version_label,
        )
    valued = [(n, v, s, k) for n, v, s, k in candidates if v is not None]
    # Source-order deletion analysis: an unconditional `del` positioned
    # after the last write removes the binding; anything else (conditional
    # `del`, or `del` overwritten by a later write) leaves doubt.
    write_positions = [n.start_point for n, _, _, _ in valued] + [
        n.start_point for n, _ in rebindings
    ]
    latest_write = max(write_positions) if write_positions else (-1, -1)
    killer = [
        n
        for n, conditional in deletions_all
        if not conditional and tuple(n.start_point) > tuple(latest_write)
    ]
    maybe = [n for n, conditional in deletions_all if conditional]
    # annotation-only with no value anywhere
    if (
        candidates
        and all(v is None for _, v, _, _ in candidates)
        and not rebindings
        and not deletions_all
    ):
        loc = location.model_copy(
            update={
                "line_start": lines[0].number + candidates[0][0].start_point[0],
                "line_end": lines[0].number + candidates[0][0].end_point[0],
            }
        )
        return CodeFactResolutionV2(
            status=CodeFactStatus.UNRESOLVED,
            repository=repository,
            revision=revision,
            fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            field_name=symbol,
            subject_identity=symbol,
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            language_version=version_label,
            analysis_method="tree-sitter-range-v1",
            source_evidence_ids=evidence_ids,
            declaration_location=loc,
            limitation_codes=("annotation_without_assignment",),
            parser_version=_p.parser_version(),
        )

    def _loc(node: Any) -> Any:
        return location.model_copy(
            update={
                "line_start": lines[0].number + node.start_point[0],
                "line_end": lines[0].number + node.end_point[0],
            }
        )

    if killer:
        # Unconditional `del` positioned after the last write removes the binding.
        return CodeFactResolutionV2(
            status=CodeFactStatus.UNRESOLVED,
            repository=repository,
            revision=revision,
            fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            field_name=symbol,
            subject_identity=symbol,
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            language_version=version_label,
            analysis_method="tree-sitter-range-v1",
            source_evidence_ids=evidence_ids,
            declaration_location=_loc(killer[0]),
            limitation_codes=("name_deleted",),
            parser_version=_p.parser_version(),
        )
    if maybe:
        # A conditional `del` leaves doubt: the value may or may not exist.
        anchor = maybe[0] if not valued else valued[0][0]
        raw = _p.node_text(valued[0][1], source_bytes).strip() if valued else None
        return _partial(
            repository,
            revision,
            symbol,
            "possible_deletion",
            evidence_ids,
            version_label,
            _loc(anchor),
            raw=raw,
        )
    if rebindings and not valued:
        # Only augmented/loop/context writes: no baseline value to report.
        return _partial(
            repository,
            revision,
            symbol,
            "rebinding_without_baseline",
            evidence_ids,
            version_label,
            _loc(rebindings[0][0]),
        )
    scopes = {s for _, _, s, _ in valued}
    if len(scopes) > 1 and not scope:
        return _unresolved(
            repository,
            revision or None,
            symbol,
            "ambiguous_target_identity",
            evidence_ids,
            version_label,
        )
    if any(k == "unpacked_arity_mismatch" for _, _, _, k in valued):
        node = next(n for n, _, _, k in valued if k == "unpacked_arity_mismatch")
        loc = location.model_copy(
            update={
                "line_start": lines[0].number + node.start_point[0],
                "line_end": lines[0].number + node.end_point[0],
            }
        )
        return _partial(
            repository,
            revision,
            symbol,
            "unpacked_arity_mismatch",
            evidence_ids,
            version_label,
            loc,
        )
    if any(k == "complex_target" for _, _, _, k in valued):
        node = next(n for n, _, _, k in valued if k == "complex_target")
        loc = location.model_copy(
            update={
                "line_start": lines[0].number + node.start_point[0],
                "line_end": lines[0].number + node.end_point[0],
            }
        )
        return _partial(
            repository,
            revision,
            symbol,
            "complex_assignment_target",
            evidence_ids,
            version_label,
            loc,
        )
    if len(valued) > 1 or (len(valued) == 1 and rebindings):
        # Multiple writes (plain, augmented, loop, or context rebinding):
        # possible overwrite; report first value as PARTIAL.
        node, value_node, _, _ = valued[0]
        raw = _p.node_text(value_node, source_bytes).strip()
        loc = location.model_copy(
            update={
                "line_start": lines[0].number + node.start_point[0],
                "line_end": lines[0].number + node.end_point[0],
            }
        )
        return _partial(
            repository,
            revision,
            symbol,
            "multiple_write_sites",
            evidence_ids,
            version_label,
            loc,
            raw=raw,
        )
    node, value_node, _, _ = valued[0]
    raw = _p.node_text(value_node, source_bytes).strip()
    evaluated, limitation = _expr.evaluate(value_node, source_bytes)
    loc = location.model_copy(
        update={
            "line_start": lines[0].number + node.start_point[0],
            "line_end": lines[0].number + node.end_point[0],
        }
    )
    if limitation is not None or evaluated is None:
        return _partial(
            repository,
            revision,
            symbol,
            limitation or "unsupported_expression",
            evidence_ids,
            version_label,
            loc,
            raw=raw,
        )
    return CodeFactResolutionV2(
        status=CodeFactStatus.RESOLVED,
        repository=repository,
        revision=revision,
        fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
        semantic_stage=SemanticStage.DECLARATION,
        field_name=symbol,
        subject_identity=symbol,
        value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
        language_version=version_label,
        analysis_method="tree-sitter-range-v1",
        raw_initializer=raw,
        resolved_value=_expr.format_value(evaluated),
        structured_fact={"python_value": _materialize_json(evaluated)},
        source_evidence_ids=evidence_ids,
        declaration_location=loc,
        assumptions=version_assumptions,
        parser_version=_p.parser_version(),
    )


def _materialize_json(value: _expr._Value) -> Any:
    kind, payload = value
    if kind == "none":
        return None
    if kind in {"int", "float", "bool", "str"}:
        return payload
    if kind == "tuple":
        return {"__tuple__": list(payload)}
    if kind == "set":
        return {"__set__": list(payload)}
    if kind in {"list", "dict"}:
        return payload
    return str(payload)


def _function_nodes(root: Any, source: bytes, function_name: str):
    for node in _p.walk(root):
        if node.type in {"function_definition", "async_function_definition"}:
            # Decorated defs are yielded via the decorated_definition
            # branch below; skip the inner node to avoid double counting.
            if node.parent is not None and node.parent.type == "decorated_definition":
                continue
            name = node.child_by_field_name("name")
            if name is not None and _p.node_text(name, source).strip() == function_name:
                yield node
            continue
        # Decorated defs wrap the real definition.
        if node.type == "decorated_definition":
            for child in node.children:
                if child.type in {"function_definition", "async_function_definition"}:
                    name = child.child_by_field_name("name")
                    if name is not None and _p.node_text(name, source).strip() == function_name:
                        yield child


def resolve_python_param_default_fact(
    evidence: Any,
    symbol: str,
    *,
    function_scope: str,
    language_version: object = None,
) -> CodeFactResolutionV2:
    """Resolve a function parameter's default expression (never a call-site value)."""
    from investigation_agent_platform.application.analysis.python import literals as _lit

    lines, reason = python_source_lines(evidence)
    provenance = getattr(evidence, "provenance", None)
    location = getattr(provenance, "source_location", None)
    source_path = str(getattr(location, "file_path", "") or "")
    revision = str(getattr(location, "revision", "") or "")
    identifier = str(getattr(location, "identifier", "") or "")
    repository = repository_of(identifier, source_path)
    evidence_id = getattr(evidence, "evidence_id", None)
    evidence_ids = (evidence_id,) if evidence_id is not None else ()
    version_label, version_assumed = _lit.resolve_language_version(language_version)
    version_assumptions = ("assumed_default_language_version",) if version_assumed else ()

    if reason:
        return _unresolved(
            repository,
            revision or None,
            symbol,
            reason,
            evidence_ids,
            version_label,
            semantics=CodeValueSemantics.PARAMETER_DEFAULT_EXPRESSION,
        )
    assert location is not None
    if not symbol or not symbol.strip():
        return _unresolved(
            repository,
            revision or None,
            symbol,
            "missing_symbol_name",
            evidence_ids,
            version_label,
            semantics=CodeValueSemantics.PARAMETER_DEFAULT_EXPRESSION,
        )
    symbol = symbol.strip()
    if not function_scope or not function_scope.strip():
        return _unresolved(
            repository,
            revision or None,
            symbol,
            "missing_function_scope",
            evidence_ids,
            version_label,
            semantics=CodeValueSemantics.PARAMETER_DEFAULT_EXPRESSION,
        )
    if not source_path.lower().endswith(".py"):
        return _unresolved(
            repository,
            revision or None,
            symbol,
            "not_a_python_source_file",
            evidence_ids,
            version_label,
            semantics=CodeValueSemantics.PARAMETER_DEFAULT_EXPRESSION,
        )
    if not repository:
        return _unresolved(
            repository,
            revision or None,
            symbol,
            "repository_identity_unavailable",
            evidence_ids,
            version_label,
            semantics=CodeValueSemantics.PARAMETER_DEFAULT_EXPRESSION,
        )

    source_bytes = "\n".join(line.text for line in lines).encode("utf-8")
    try:
        tree = _p.parse_python(source_bytes)
    except Exception as exc:
        return _unresolved(
            repository,
            revision or None,
            symbol,
            f"python_parser_unavailable:{type(exc).__name__}",
            evidence_ids,
            version_label,
            semantics=CodeValueSemantics.PARAMETER_DEFAULT_EXPRESSION,
        )
    if _p.has_syntax_error(tree.root_node):
        return _unresolved(
            repository,
            revision or None,
            symbol,
            "python_syntax_error_or_incomplete_source_window",
            evidence_ids,
            version_label,
            semantics=CodeValueSemantics.PARAMETER_DEFAULT_EXPRESSION,
        )

    matches = list(_function_nodes(tree.root_node, source_bytes, function_scope.strip()))
    if len(matches) != 1:
        code = "function_not_found" if not matches else "ambiguous_function_definition"
        return _unresolved(
            repository,
            revision or None,
            symbol,
            code,
            evidence_ids,
            version_label,
            semantics=CodeValueSemantics.PARAMETER_DEFAULT_EXPRESSION,
        )
    func = matches[0]
    params = next((c for c in func.children if c.type == "parameters"), None)
    if params is None:
        return _unresolved(
            repository,
            revision or None,
            symbol,
            "parameter_not_found",
            evidence_ids,
            version_label,
            semantics=CodeValueSemantics.PARAMETER_DEFAULT_EXPRESSION,
        )
    for child in params.children:
        if child.type in {"default_parameter", "typed_default_parameter"}:
            name = child.child_by_field_name("name")
            if name is None or _p.node_text(name, source_bytes).strip() != symbol:
                continue
            value_node = child.child_by_field_name("value")
            if value_node is None:
                named = [c for c in child.children if c.is_named]
                value_node = named[-1] if named else None
            if value_node is None:
                return _unresolved(
                    repository,
                    revision or None,
                    symbol,
                    "parameter_default_missing",
                    evidence_ids,
                    version_label,
                    semantics=CodeValueSemantics.PARAMETER_DEFAULT_EXPRESSION,
                )
            raw = _p.node_text(value_node, source_bytes).strip()
            evaluated, limitation = _expr.evaluate(value_node, source_bytes)
            loc = location.model_copy(
                update={
                    "line_start": lines[0].number + child.start_point[0],
                    "line_end": lines[0].number + child.end_point[0],
                }
            )
            if limitation is not None or evaluated is None:
                return _partial(
                    repository,
                    revision,
                    f"{function_scope}.{symbol}",
                    limitation or "unsupported_expression",
                    evidence_ids,
                    version_label,
                    loc,
                    semantics=CodeValueSemantics.PARAMETER_DEFAULT_EXPRESSION,
                    raw=raw,
                )
            assumptions: tuple[str, ...] = ()
            if evaluated[0] in {"list", "dict", "set"}:
                assumptions = ("mutable_default_shared",)
            assumptions = (*assumptions, *version_assumptions)
            return CodeFactResolutionV2(
                status=CodeFactStatus.RESOLVED,
                repository=repository,
                revision=revision,
                fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
                semantic_stage=SemanticStage.DECLARATION,
                declaring_type=function_scope,
                field_name=symbol,
                subject_identity=f"{function_scope}.{symbol}",
                value_semantics=CodeValueSemantics.PARAMETER_DEFAULT_EXPRESSION,
                language_version=version_label,
                analysis_method="tree-sitter-range-v1",
                raw_initializer=raw,
                resolved_value=_expr.format_value(evaluated),
                structured_fact={"python_value": _materialize_json(evaluated)},
                source_evidence_ids=evidence_ids,
                declaration_location=loc,
                assumptions=assumptions,
                parser_version=_p.parser_version(),
            )
        if child.type in {"identifier"} and _p.node_text(child, source_bytes).strip() == symbol:
            loc = location.model_copy(
                update={
                    "line_start": lines[0].number + child.start_point[0],
                    "line_end": lines[0].number + child.end_point[0],
                }
            )
            return CodeFactResolutionV2(
                status=CodeFactStatus.UNRESOLVED,
                repository=repository,
                revision=revision,
                fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
                semantic_stage=SemanticStage.DECLARATION,
                declaring_type=function_scope,
                field_name=symbol,
                subject_identity=f"{function_scope}.{symbol}",
                value_semantics=CodeValueSemantics.PARAMETER_DEFAULT_EXPRESSION,
                language_version=version_label,
                analysis_method="tree-sitter-range-v1",
                source_evidence_ids=evidence_ids,
                declaration_location=loc,
                limitation_codes=("parameter_has_no_default",),
                parser_version=_p.parser_version(),
            )
    return _unresolved(
        repository,
        revision or None,
        symbol,
        "parameter_not_found",
        evidence_ids,
        version_label,
        semantics=CodeValueSemantics.PARAMETER_DEFAULT_EXPRESSION,
    )
