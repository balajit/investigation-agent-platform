# src/investigation_agent_platform/application/analysis/python/control.py
"""Python write-site enumeration and control-flow inventory (leads, not proofs).

A visible write or branch never proves execution: results are PARTIAL with
exact line-anchored spans so the reasoner can cite possible paths without
asserting reachability. Async functions, generators, and exception paths
are inventoried with explicit boundary codes.
"""

from __future__ import annotations

from typing import Any

from investigation_agent_platform.application.analysis.failure import failure_code
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


def _base(
    evidence: Any,
) -> tuple[Any, Any, str, str, tuple[Any, ...], str, Any, bytes, BaseException | None] | None:
    """Common gates. Returns (lines, location, repository, revision, evidence_ids, reason, tree, source_bytes, parse_exc)."""
    lines, reason = python_source_lines(evidence)
    provenance = getattr(evidence, "provenance", None)
    location = getattr(provenance, "source_location", None)
    source_path = str(getattr(location, "file_path", "") or "")
    revision = str(getattr(location, "revision", "") or "")
    identifier = str(getattr(location, "identifier", "") or "")
    repository = repository_of(identifier, source_path)
    evidence_id = getattr(evidence, "evidence_id", None)
    evidence_ids = (evidence_id,) if evidence_id is not None else ()
    if reason:
        return (None, None, repository, revision or "", evidence_ids, reason, None, b"", None)
    if location is None:
        return (
            None,
            None,
            repository,
            revision or "",
            evidence_ids,
            "missing_source_location",
            None,
            b"",
            None,
        )
    if not source_path.lower().endswith(".py"):
        return (
            None,
            None,
            repository,
            revision or "",
            evidence_ids,
            "not_a_python_source_file",
            None,
            b"",
            None,
        )
    if not repository:
        return (
            None,
            None,
            repository,
            revision or "",
            evidence_ids,
            "repository_identity_unavailable",
            None,
            b"",
            None,
        )
    source_bytes = "\n".join(line.text for line in lines).encode("utf-8")
    try:
        tree = _p.parse_python(source_bytes)
    except Exception as exc:
        return (
            None,
            None,
            repository,
            revision or "",
            evidence_ids,
            failure_code(exc),
            None,
            b"",
            exc,
        )
    if _p.has_syntax_error(tree.root_node):
        return (
            None,
            None,
            repository,
            revision or "",
            evidence_ids,
            "python_syntax_error_or_incomplete_source_window",
            None,
            b"",
            None,
        )
    return (lines, location, repository, revision, evidence_ids, "", tree, source_bytes, None)


def resolve_python_write_sites_fact(
    evidence: Any,
    symbol: str,
    *,
    scope: str | None = None,
    class_scope: str | None = None,
    language_version: object = None,
) -> CodeFactResolutionV2:
    """Enumerate every write site for a name: possible writes, never the outcome."""
    from investigation_agent_platform.application.analysis.python import literals as _lit
    from investigation_agent_platform.domain.provenance.models import SourceLocation

    version_label, version_assumed = _lit.resolve_language_version(language_version)
    version_assumptions = ("assumed_default_language_version",) if version_assumed else ()
    base = _base(evidence)
    assert base is not None
    lines, location, repository, revision, evidence_ids, reason, tree, source_bytes, parse_exc = base
    subject = (symbol or "").strip()

    def unresolved(code: str, *, exc: BaseException | None = None) -> CodeFactResolutionV2:
        return CodeFactResolutionV2(
            status=CodeFactStatus.UNRESOLVED,
            repository=repository or "unknown",
            revision=revision or None,
            fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            declaring_type=(class_scope or scope) or None,
            field_name=subject.rsplit(".", 1)[-1] or None,
            subject_identity=f"{class_scope}.{subject}" if class_scope else subject,
            value_semantics=CodeValueSemantics.LATER_MUTABLE,
            language_version=version_label,
            analysis_method="tree-sitter-range-v1",
            source_evidence_ids=tuple(e for e in evidence_ids if e is not None),
            declaration_location=SourceLocation(system="Git", identifier="unknown"),
            limitation_codes=(code,),
            assumptions=() if exc is None else (f"exception_type={type(exc).__name__}",),
            parser_version=_p.parser_version(),
        )

    if reason:
        return unresolved(reason, exc=parse_exc)
    assert lines is not None and location is not None and tree is not None
    if not subject:
        return unresolved("missing_symbol_name")

    def loc_of(node: Any) -> Any:
        return location.model_copy(
            update={
                "line_start": lines[0].number + node.start_point[0],
                "line_end": lines[0].number + node.end_point[0],
            }
        )

    sites: list[str] = []
    first_node: Any = None
    scope_label = class_scope or scope or ""
    for node in _p.walk(tree.root_node):
        if node.type != "assignment":
            continue
        named = [c for c in node.children if c.is_named]
        if not named:
            continue
        left = named[0]
        line_no = lines[0].number + node.start_point[0]
        if class_scope:
            if left.type != "attribute":
                continue
            obj = left.child_by_field_name("object")
            attr = left.child_by_field_name("attribute")
            if (
                obj is None
                or attr is None
                or _p.node_text(obj, source_bytes).strip() != "self"
                or _p.node_text(attr, source_bytes).strip() != subject
            ):
                continue
            method = ""
            cur = node.parent
            while cur is not None:
                if cur.type in {"function_definition", "async_function_definition"}:
                    nm = cur.child_by_field_name("name")
                    method = _p.node_text(nm, source_bytes).strip() if nm is not None else ""
                    break
                cur = cur.parent
            sites.append(f"{class_scope}.{subject}@{line_no}:{method or 'class-body'}")
        else:
            targets: list[str] = []
            if left.type == "identifier":
                targets = [_p.node_text(left, source_bytes).strip()]
            elif left.type in {"pattern_list", "expression_list", "tuple_pattern", "list_pattern"}:
                targets = [
                    _p.node_text(c, source_bytes).strip()
                    for c in left.children
                    if c.is_named and c.type == "identifier"
                ]
            if subject not in targets:
                continue
            if scope:
                cur = node.parent
                enclosing = ""
                while cur is not None:
                    if cur.type in {
                        "function_definition",
                        "async_function_definition",
                        "class_definition",
                    }:
                        nm = cur.child_by_field_name("name")
                        enclosing = _p.node_text(nm, source_bytes).strip() if nm is not None else ""
                        break
                    if cur.type == "module":
                        break
                    cur = cur.parent
                if enclosing != scope and not _p.matches_scope(enclosing, scope):
                    continue
            sites.append(f"{subject}@{line_no}")
        if first_node is None:
            first_node = node
    if not sites:
        return unresolved("symbol_not_in_window")
    codes = ("potential_write_sites",)
    if len(sites) > 1:
        codes = ("potential_write_sites", "multiple_write_sites")
    return CodeFactResolutionV2(
        status=CodeFactStatus.PARTIAL,
        repository=repository,
        revision=revision,
        fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
        semantic_stage=SemanticStage.LATER_MUTATION,
        declaring_type=(class_scope or scope) or None,
        field_name=subject.rsplit(".", 1)[-1],
        subject_identity=f"{class_scope}.{subject}"
        if class_scope
        else (f"{scope_label}.{subject}" if scope_label else subject),
        value_semantics=CodeValueSemantics.LATER_MUTABLE,
        language_version=version_label,
        analysis_method="tree-sitter-range-v1",
        source_evidence_ids=evidence_ids,
        declaration_location=loc_of(first_node),
        limitation_codes=codes,
        assumptions=version_assumptions,
        structured_fact={"write_sites": sites},
        parser_version=_p.parser_version(),
    )


def resolve_python_control_fact(
    evidence: Any,
    symbol: str,
    *,
    branch_hint: str | None = None,
    class_scope: str | None = None,
    language_version: object = None,
) -> CodeFactResolutionV2:
    """Inventory a function's possible returns/raises/branches (reachability unknown)."""
    from investigation_agent_platform.application.analysis.python import literals as _lit
    from investigation_agent_platform.domain.provenance.models import SourceLocation

    version_label, version_assumed = _lit.resolve_language_version(language_version)
    version_assumptions = ("assumed_default_language_version",) if version_assumed else ()
    base = _base(evidence)
    assert base is not None
    lines, location, repository, revision, evidence_ids, reason, tree, source_bytes, parse_exc = base
    subject = (symbol or "").strip()

    def unresolved(code: str, *, exc: BaseException | None = None) -> CodeFactResolutionV2:
        return CodeFactResolutionV2(
            status=CodeFactStatus.UNRESOLVED,
            repository=repository or "unknown",
            revision=revision or None,
            fact_category=JavaFactCategory.CONTROL_FLOW_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            declaring_type=class_scope,
            field_name=subject.rsplit(".", 1)[-1] or None,
            subject_identity=f"{class_scope}.{subject}" if class_scope else subject,
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            language_version=version_label,
            analysis_method="tree-sitter-range-v1",
            source_evidence_ids=tuple(e for e in evidence_ids if e is not None),
            declaration_location=SourceLocation(system="Git", identifier="unknown"),
            limitation_codes=(code,),
            assumptions=() if exc is None else (f"exception_type={type(exc).__name__}",),
            parser_version=_p.parser_version(),
        )

    if reason:
        return unresolved(reason, exc=parse_exc)
    assert lines is not None and location is not None and tree is not None
    if not subject:
        return unresolved("missing_symbol_name")

    def loc_of(node: Any) -> Any:
        return location.model_copy(
            update={
                "line_start": lines[0].number + node.start_point[0],
                "line_end": lines[0].number + node.end_point[0],
            }
        )

    # Locate the function (optionally scoped to a class).
    func: Any = None
    for node in _p.walk(tree.root_node):
        if node.type not in {"function_definition", "async_function_definition"}:
            continue
        name = node.child_by_field_name("name")
        if name is None or _p.node_text(name, source_bytes).strip() != subject:
            continue
        if class_scope:
            cur, inside = node.parent, False
            while cur is not None:
                if cur.type == "class_definition":
                    nm = cur.child_by_field_name("name")
                    if nm is not None and _p.node_text(nm, source_bytes).strip() == class_scope:
                        inside = True
                    break
                cur = cur.parent
            if not inside:
                continue
        if func is not None:
            return unresolved("ambiguous_function_definition")
        func = node
        # The grammar also spells `async def` as function_definition with
        # an `async` leaf, so check both shapes.
        is_async = node.type == "async_function_definition" or any(
            not c.is_named and _p.node_text(c, source_bytes).strip() == "async"
            for c in node.children
        )
    if func is None:
        return unresolved("function_not_found")

    returns: list[str] = []
    raises: list[str] = []
    branches: list[str] = []
    is_generator = False
    for node in _p.walk(func):
        if node == func:
            continue
        if node.type in {"function_definition", "async_function_definition", "lambda"}:
            continue  # nested scopes are not this function's paths
        if node.type == "return_statement":
            kids = [c for c in node.children if c.is_named]
            line_no = lines[0].number + node.start_point[0]
            if not kids:
                returns.append(f"return<void>@{line_no}")
            else:
                value, limitation = _expr.evaluate(kids[0], source_bytes)
                label = (
                    _expr.format_value(value)
                    if limitation is None and value
                    else _p.node_text(kids[0], source_bytes).strip()[:60]
                )
                returns.append(f"return {label}@{line_no}")
        elif node.type == "raise_statement":
            line_no = lines[0].number + node.start_point[0]
            raises.append(f"raise@{line_no}")
        elif node.type in {
            "if_statement",
            "while_statement",
            "for_statement",
            "try_statement",
            "with_statement",
            "match_statement",
        }:
            line_no = lines[0].number + node.start_point[0]
            cond = ""
            for child in node.children:
                if child.type == "condition" or (
                    child.is_named and node.type in {"if_statement", "while_statement"}
                ):
                    cond = _p.node_text(child, source_bytes).strip()[:80]
                    break
            branches.append(f"{node.type}@{line_no}:{cond}")
        elif node.type == "yield":
            is_generator = True

    def hint_ok(text: str) -> bool:
        return branch_hint is None or branch_hint.strip().lower() in text.lower()

    if branch_hint and branch_hint.strip():
        kept_returns = [r for r in returns if hint_ok(r)]
        kept_branches = [b for b in branches if hint_ok(b)]
        if not kept_returns and not kept_branches:
            return unresolved("branch_hint_not_found")
        returns, branches = kept_returns, kept_branches

    codes = ["possible_paths_only"]
    if is_async:
        codes.append("async_function")
    if is_generator:
        codes.append("generator_function")
    refs = tuple(f"{r}" for r in returns[:10]) + tuple(f"{b}" for b in branches[:10])
    return CodeFactResolutionV2(
        status=CodeFactStatus.PARTIAL,
        repository=repository,
        revision=revision,
        fact_category=JavaFactCategory.CONTROL_FLOW_FACT,
        semantic_stage=SemanticStage.DECLARATION,
        declaring_type=class_scope,
        field_name=subject,
        subject_identity=f"{class_scope}.{subject}" if class_scope else subject,
        value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
        language_version=version_label,
        analysis_method="tree-sitter-range-v1",
        source_evidence_ids=evidence_ids,
        declaration_location=loc_of(func),
        limitation_codes=tuple(codes),
        assumptions=version_assumptions,
        control_flow_refs=refs,
        structured_fact={"returns": returns, "raises": raises, "branches": branches},
        parser_version=_p.parser_version(),
    )
