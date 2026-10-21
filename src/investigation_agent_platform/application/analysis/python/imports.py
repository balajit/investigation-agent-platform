# src/investigation_agent_platform/application/analysis/python/imports.py
"""Python import alias resolution (definition identity, never live binding).

A plain ``import a.b as c`` or ``from m import n as c`` resolves the local
name to its module path as a declaration fact. Conditional imports
(``if``/``try``), star imports, relative imports without a package anchor,
and dynamic ``importlib`` calls cannot be proven statically and yield
PARTIAL. An imported name is a reference to another module's declaration,
never proof of the referenced value.
"""

from __future__ import annotations

from typing import Any

from investigation_agent_platform.application.analysis.python import _parse as _p
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

_DYNAMIC_IMPORTS = frozenset({"__import__", "import_module", "importlib"})


def _conditional(node: Any) -> bool:
    """True when the import sits under an if/try/with (not top-level/definite)."""
    current = node.parent
    while current is not None:
        if current.type in {"if_statement", "try_statement", "with_statement", "while_statement", "for_statement"}:
            return True
        if current.type in {"function_definition", "async_function_definition", "class_definition", "module"}:
            return current.type != "module"
        current = current.parent
    return False


def collect_imports(root: Any, source: bytes) -> list[dict[str, Any]]:
    """Collect static import bindings: {bound_name, module, conditional, star, node}."""
    found: list[dict[str, Any]] = []
    for node in _p.walk(root):
        if node.type == "import_statement":
            for child in node.children:
                if child.type == "dotted_name":
                    mod = _p.node_text(child, source).strip()
                    found.append(
                        {
                            "bound_name": mod.split(".")[0],
                            "module": mod,
                            "conditional": _conditional(node),
                            "star": False,
                            "dynamic": False,
                            "node": node,
                        }
                    )
                elif child.type == "aliased_import":
                    names = [c for c in child.children if c.is_named]
                    if len(names) >= 2:
                        mod = _p.node_text(names[0], source).strip()
                        alias = _p.node_text(names[-1], source).strip()
                        found.append(
                            {
                                "bound_name": alias,
                                "module": mod,
                                "conditional": _conditional(node),
                                "star": False,
                                "dynamic": False,
                                "node": node,
                            }
                        )
        elif node.type == "import_from_statement":
            # The module part precedes the `import` keyword; imported names
            # follow it. Splitting on position (not last-match) keeps
            # `from . import x` bound to `.x` instead of dropping it.
            kids = node.children
            try:
                import_idx = next(
                    i
                    for i, c in enumerate(kids)
                    if not c.is_named and _p.node_text(c, source).strip() == "import"
                )
            except StopIteration:
                import_idx = len(kids)
            module_node = next(
                (c for c in kids[:import_idx] if c.type in {"dotted_name", "relative_import"}),
                None,
            )
            module = _p.node_text(module_node, source).strip() if module_node is not None else ""

            def _join(base: str, name: str) -> str:
                if not base:
                    return name
                if set(base) <= {"."}:
                    return f"{base}{name}"
                return f"{base}.{name}"

            for child in kids[import_idx:]:
                if child.type == "wildcard_import":
                    found.append(
                        {
                            "bound_name": "*",
                            "module": module,
                            "conditional": _conditional(node),
                            "star": True,
                            "dynamic": False,
                            "node": node,
                        }
                    )
                elif child.type == "aliased_import":
                    names = [c for c in child.children if c.is_named]
                    if len(names) >= 2:
                        found.append(
                            {
                                "bound_name": _p.node_text(names[-1], source).strip(),
                                "module": _join(module, _p.node_text(names[0], source).strip()),
                                "conditional": _conditional(node),
                                "star": False,
                                "dynamic": False,
                                "node": node,
                            }
                        )
                elif child.type == "dotted_name":
                    name = _p.node_text(child, source).strip()
                    found.append(
                        {
                            "bound_name": name,
                            "module": _join(module, name),
                            "conditional": _conditional(node),
                            "star": False,
                            "dynamic": False,
                            "node": node,
                        }
                    )
                elif child.type == "identifier":
                    name = _p.node_text(child, source).strip()
                    if name not in {"from", "import"}:
                        found.append(
                            {
                                "bound_name": name,
                                "module": _join(module, name),
                                "conditional": _conditional(node),
                                "star": False,
                                "dynamic": False,
                                "node": node,
                            }
                        )
        elif node.type == "call":
            func = node.child_by_field_name("function")
            text = _p.node_text(func, source).strip() if func is not None else ""
            if text in _DYNAMIC_IMPORTS or "importlib" in text:
                found.append(
                    {
                        "bound_name": "",
                        "module": "",
                        "conditional": True,
                        "star": False,
                        "dynamic": True,
                        "node": node,
                    }
                )
    return found


def resolve_python_import_fact(
    evidence: Any,
    symbol: str,
    *,
    language_version: object = None,
) -> CodeFactResolutionV2:
    """Resolve a local name to its imported module path (declaration identity)."""
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

    def unresolved(code: str) -> CodeFactResolutionV2:
        from investigation_agent_platform.domain.provenance.models import SourceLocation

        return CodeFactResolutionV2(
            status=CodeFactStatus.UNRESOLVED,
            repository=repository or "unknown",
            revision=revision or None,
            fact_category=JavaFactCategory.DECLARATION_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            field_name=symbol,
            subject_identity=symbol,
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            language_version=version_label,
            analysis_method="tree-sitter-range-v1",
            source_evidence_ids=tuple(e for e in evidence_ids if e is not None),
            declaration_location=SourceLocation(system="Git", identifier="unknown"),
            limitation_codes=(code,),
            parser_version=_p.parser_version(),
        )

    if reason:
        return unresolved(reason)
    assert location is not None
    if not symbol or not symbol.strip():
        return unresolved("missing_symbol_name")
    symbol = symbol.strip()
    if not source_path.lower().endswith(".py"):
        return unresolved("not_a_python_source_file")
    if not repository:
        return unresolved("repository_identity_unavailable")

    source_bytes = "\n".join(line.text for line in lines).encode("utf-8")
    try:
        tree = _p.parse_python(source_bytes)
    except Exception as exc:
        return unresolved(f"python_parser_unavailable:{type(exc).__name__}")
    if _p.has_syntax_error(tree.root_node):
        return unresolved("python_syntax_error_or_incomplete_source_window")

    matches = [b for b in collect_imports(tree.root_node, source_bytes) if b["bound_name"] == symbol]
    if not matches:
        # A name bound by a dynamic import call binds nothing statically.
        for node in _p.walk(tree.root_node):
            if node.type != "assignment":
                continue
            named = [c for c in node.children if c.is_named]
            if not named or named[0].type != "identifier":
                continue
            if _p.node_text(named[0], source_bytes).strip() != symbol or len(named) < 2:
                continue
            call_text = _p.node_text(named[-1], source_bytes)
            if "__import__" in call_text or "importlib" in call_text or "import_module" in call_text:
                return CodeFactResolutionV2(
                    status=CodeFactStatus.PARTIAL,
                    repository=repository,
                    revision=revision,
                    fact_category=JavaFactCategory.DECLARATION_FACT,
                    semantic_stage=SemanticStage.DECLARATION,
                    field_name=symbol,
                    subject_identity=symbol,
                    value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
                    language_version=version_label,
                    analysis_method="tree-sitter-range-v1",
                    source_evidence_ids=evidence_ids,
                    declaration_location=location.model_copy(
                        update={
                            "line_start": lines[0].number + node.start_point[0],
                            "line_end": lines[0].number + node.end_point[0],
                        }
                    ),
                    limitation_codes=("dynamic_import_requires_runtime",),
                    parser_version=_p.parser_version(),
                )
        return unresolved("symbol_not_in_window")
    if len({b["module"] for b in matches}) > 1:
        return unresolved("ambiguous_import_binding")

    binding = matches[0]

    def loc_of(node: Any) -> Any:
        return location.model_copy(
            update={
                "line_start": lines[0].number + node.start_point[0],
                "line_end": lines[0].number + node.end_point[0],
            }
        )

    if binding["dynamic"]:
        return CodeFactResolutionV2(
            status=CodeFactStatus.PARTIAL,
            repository=repository,
            revision=revision,
            fact_category=JavaFactCategory.DECLARATION_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            field_name=symbol,
            subject_identity=symbol,
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            language_version=version_label,
            analysis_method="tree-sitter-range-v1",
            source_evidence_ids=evidence_ids,
            declaration_location=loc_of(binding["node"]),
            limitation_codes=("dynamic_import_requires_runtime",),
            parser_version=_p.parser_version(),
        )
    if binding["star"]:
        return CodeFactResolutionV2(
            status=CodeFactStatus.PARTIAL,
            repository=repository,
            revision=revision,
            fact_category=JavaFactCategory.DECLARATION_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            field_name=symbol,
            subject_identity=symbol,
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            language_version=version_label,
            analysis_method="tree-sitter-range-v1",
            source_evidence_ids=evidence_ids,
            declaration_location=loc_of(binding["node"]),
            limitation_codes=("star_import_binding_unknown",),
            parser_version=_p.parser_version(),
        )
    if binding["conditional"]:
        return CodeFactResolutionV2(
            status=CodeFactStatus.PARTIAL,
            repository=repository,
            revision=revision,
            fact_category=JavaFactCategory.DECLARATION_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            field_name=symbol,
            subject_identity=symbol,
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            language_version=version_label,
            analysis_method="tree-sitter-range-v1",
            raw_initializer=binding["module"],
            source_evidence_ids=evidence_ids,
            declaration_location=loc_of(binding["node"]),
            limitation_codes=("conditional_import_may_not_execute",),
            parser_version=_p.parser_version(),
        )
    return CodeFactResolutionV2(
        status=CodeFactStatus.RESOLVED,
        repository=repository,
        revision=revision,
        fact_category=JavaFactCategory.DECLARATION_FACT,
        semantic_stage=SemanticStage.DECLARATION,
        field_name=symbol,
        subject_identity=symbol,
        value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
        language_version=version_label,
        analysis_method="tree-sitter-range-v1",
        raw_initializer=binding["module"],
        resolved_value=binding["module"],
        structured_fact={"imported_module": binding["module"]},
        source_evidence_ids=evidence_ids,
        declaration_location=loc_of(binding["node"]),
        assumptions=version_assumptions,
        parser_version=_p.parser_version(),
    )
