# src/investigation_agent_platform/application/analysis/python/mro.py
"""Python static MRO and method-owner resolution (single pinned window only).

Computes C3 linearization when every base class is defined in the same
source window. Imported or otherwise out-of-window bases make the chain
unknowable statically and yield PARTIAL. The method owner is the first
MRO entry defining the name — the statically selected implementation,
never proof it executes (runtime type decides dispatch).
"""

from __future__ import annotations

from typing import Any

from investigation_agent_platform.application.analysis.python import _parse as _p
from investigation_agent_platform.application.analysis.python._provenance import (
    python_source_lines,
    repository_of,
)
from investigation_agent_platform.application.analysis.python.class_facts import (
    _class_nodes,
)
from investigation_agent_platform.domain.code.models import (
    CodeFactResolutionV2,
    CodeFactStatus,
    CodeValueSemantics,
    JavaFactCategory,
    SemanticStage,
)


def _base_names(cls: Any, source: bytes) -> list[str] | None:
    """Explicit base names, [] for implicit-object, None when unparseable.

    The grammar exposes bases as a bare ``argument_list`` child of the
    class definition (no ``superclass`` field); keyword arguments such as
    ``metaclass=`` are not bases and are skipped.
    """
    for child in cls.children:
        if child.type != "argument_list":
            continue
        names: list[str] = []
        for arg in child.children:
            if not arg.is_named:
                continue
            if arg.type == "keyword_argument":
                continue
            if arg.type not in {"identifier", "attribute", "subscript"}:
                return None
            names.append(_p.node_text(arg, source).strip())
        return names
    return []


def _c3(class_name: str, bases: dict[str, list[str]]) -> list[str] | None:
    """C3 linearization over in-window classes; None on unknown base or conflict."""

    def merge(seqs: list[list[str]]) -> list[str] | None:
        result: list[str] = []
        seqs = [list(s) for s in seqs if s]
        while True:
            seqs = [s for s in seqs if s]
            if not seqs:
                return result
            for seq in seqs:
                head = seq[0]
                if not any(head in s[1:] for s in seqs):
                    result.append(head)
                    for s in seqs:
                        if s and s[0] == head:
                            del s[0]
                    break
            else:
                return None

    def lin(name: str, stack: tuple[str, ...] = ()) -> list[str] | None:
        if name not in bases:
            return None
        if name in stack:
            return None
        direct = bases[name]
        seqs = []
        for base in direct:
            if base == "object":
                seqs.append(["object"])
                continue
            sub = lin(base, (*stack, name))
            if sub is None:
                return None
            seqs.append(sub)
        seqs.append(list(direct if direct else ["object"]))
        merged = merge(seqs)
        if merged is None:
            return None
        return [name, *[m for m in merged if m != name]]

    return lin(class_name)


def _defines_method(cls: Any, source: bytes, method: str) -> Any | None:
    for node in _p.walk(cls):
        if node.type in {"function_definition", "async_function_definition"}:
            name = node.child_by_field_name("name")
            if name is not None and _p.node_text(name, source).strip() == method:
                # Must be a direct member, not a nested function.
                cur = node.parent
                while cur is not None and cur != cls:
                    if cur.type in {"function_definition", "async_function_definition"}:
                        break
                    cur = cur.parent
                else:
                    return node
    return None


def _context(
    evidence: Any, source_path_suffix: str = ".py"
) -> tuple[Any, Any, str, str, str, tuple[Any, ...], str, tuple[str, ...]] | tuple[None, ...]:
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
        return (None, None, repository, revision or "", "", evidence_ids, reason, ())
    if location is None:
        return (
            None,
            None,
            repository,
            revision or "",
            "",
            evidence_ids,
            "missing_source_location",
            (),
        )
    if not source_path.lower().endswith(source_path_suffix):
        return (
            None,
            None,
            repository,
            revision or "",
            "",
            evidence_ids,
            "not_a_python_source_file",
            (),
        )
    if not repository:
        return (
            None,
            None,
            repository,
            revision or "",
            "",
            evidence_ids,
            "repository_identity_unavailable",
            (),
        )
    return (lines, location, repository, revision, source_path, evidence_ids, "", ())


def _parse_window(lines: Any, location: Any) -> tuple[Any, bytes] | tuple[None, str]:
    source_bytes = "\n".join(line.text for line in lines).encode("utf-8")
    try:
        tree = _p.parse_python(source_bytes)
    except Exception as exc:
        return (None, f"python_parser_unavailable:{type(exc).__name__}")
    if _p.has_syntax_error(tree.root_node):
        return (None, "python_syntax_error_or_incomplete_source_window")
    return (tree, source_bytes)


def resolve_python_mro_fact(
    evidence: Any,
    class_scope: str,
    *,
    language_version: object = None,
) -> CodeFactResolutionV2:
    """Linearize the MRO from in-window class definitions (C3)."""
    from investigation_agent_platform.application.analysis.python import literals as _lit
    from investigation_agent_platform.domain.provenance.models import SourceLocation

    ctx = _context(evidence)
    if ctx[0] is None:
        _, _, repository, revision, _, evidence_ids, reason, _ = ctx
        return CodeFactResolutionV2(
            status=CodeFactStatus.UNRESOLVED,
            repository=repository or "unknown",
            revision=revision or None,
            fact_category=JavaFactCategory.LANGUAGE_SEMANTIC_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            declaring_type=class_scope or None,
            field_name=class_scope or None,
            subject_identity=class_scope,
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            analysis_method="tree-sitter-range-v1",
            source_evidence_ids=tuple(e for e in evidence_ids if e is not None),
            declaration_location=SourceLocation(system="Git", identifier="unknown"),
            limitation_codes=(reason,),
            parser_version=_p.parser_version(),
        )
    lines, location, repository, revision, _, evidence_ids, _, _ = ctx
    version_label, version_assumed = _lit.resolve_language_version(language_version)
    version_assumptions = ("assumed_default_language_version",) if version_assumed else ()

    def unresolved(code: str) -> CodeFactResolutionV2:
        return CodeFactResolutionV2(
            status=CodeFactStatus.UNRESOLVED,
            repository=repository or "unknown",
            revision=revision or None,
            fact_category=JavaFactCategory.LANGUAGE_SEMANTIC_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            declaring_type=class_scope or None,
            field_name=class_scope or None,
            subject_identity=class_scope,
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            language_version=version_label,
            analysis_method="tree-sitter-range-v1",
            source_evidence_ids=tuple(e for e in evidence_ids if e is not None),
            declaration_location=SourceLocation(system="Git", identifier="unknown"),
            limitation_codes=(code,),
            parser_version=_p.parser_version(),
        )

    if not class_scope or not class_scope.strip():
        return unresolved("missing_class_scope")
    class_scope = class_scope.strip()
    parsed = _parse_window(lines, location)
    if parsed[0] is None:
        return unresolved(parsed[1])
    tree, source_bytes = parsed

    matches = list(_class_nodes(tree.root_node, source_bytes, class_scope))
    if len(matches) != 1:
        return unresolved("class_not_found" if not matches else "ambiguous_class_definition")
    cls = matches[0]

    def loc_of(node: Any) -> Any:
        return location.model_copy(
            update={
                "line_start": lines[0].number + node.start_point[0],
                "line_end": lines[0].number + node.end_point[0],
            }
        )

    # Collect every in-window class and its bases.
    all_classes: dict[str, Any] = {}
    for node in _p.walk(tree.root_node):
        if node.type == "class_definition":
            if node.parent is not None and node.parent.type == "decorated_definition":
                continue
            name = node.child_by_field_name("name")
            if name is not None:
                all_classes[_p.node_text(name, source_bytes).strip()] = node
    bases: dict[str, list[str]] = {}
    for name, node in all_classes.items():
        b = _base_names(node, source_bytes)
        if b is None:
            return CodeFactResolutionV2(
                status=CodeFactStatus.PARTIAL,
                repository=repository,
                revision=revision,
                fact_category=JavaFactCategory.LANGUAGE_SEMANTIC_FACT,
                semantic_stage=SemanticStage.DECLARATION,
                declaring_type=class_scope,
                field_name=class_scope or None,
                subject_identity=class_scope,
                value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
                language_version=version_label,
                analysis_method="tree-sitter-range-v1",
                source_evidence_ids=evidence_ids,
                declaration_location=loc_of(node),
                limitation_codes=("unparseable_base_list",),
                parser_version=_p.parser_version(),
            )
        bases[name] = b
    # Unknown (out-of-window) bases fail the static chain, but only when
    # reachable from the queried class — unrelated classes in the same
    # window must not poison each other's result.
    reachable: set[str] = set()
    stack = [class_scope]
    unknown_base: str | None = None
    while stack:
        current = stack.pop()
        if current in reachable or current == "object":
            continue
        reachable.add(current)
        for base in bases.get(current, []):
            short = base.rsplit(".", 1)[-1]
            if base == "object":
                continue
            if short in all_classes:
                stack.append(short)
            elif base in all_classes:
                stack.append(base)
            else:
                unknown_base = base
    if unknown_base is not None:
        return CodeFactResolutionV2(
            status=CodeFactStatus.PARTIAL,
            repository=repository,
            revision=revision,
            fact_category=JavaFactCategory.LANGUAGE_SEMANTIC_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            declaring_type=class_scope,
            field_name=class_scope or None,
            subject_identity=class_scope,
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            language_version=version_label,
            analysis_method="tree-sitter-range-v1",
            source_evidence_ids=evidence_ids,
            declaration_location=loc_of(cls),
            limitation_codes=("cross_module_base_class",),
            parser_version=_p.parser_version(),
        )
    chain = _c3(class_scope, bases)
    if chain is None:
        return CodeFactResolutionV2(
            status=CodeFactStatus.PARTIAL,
            repository=repository,
            revision=revision,
            fact_category=JavaFactCategory.LANGUAGE_SEMANTIC_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            declaring_type=class_scope,
            field_name=class_scope or None,
            subject_identity=class_scope,
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            language_version=version_label,
            analysis_method="tree-sitter-range-v1",
            source_evidence_ids=evidence_ids,
            declaration_location=loc_of(cls),
            limitation_codes=("mro_linearization_failed",),
            parser_version=_p.parser_version(),
        )
    return CodeFactResolutionV2(
        status=CodeFactStatus.RESOLVED,
        repository=repository,
        revision=revision,
        fact_category=JavaFactCategory.LANGUAGE_SEMANTIC_FACT,
        semantic_stage=SemanticStage.DECLARATION,
        declaring_type=class_scope,
        field_name=class_scope or None,
        subject_identity=class_scope,
        value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
        language_version=version_label,
        analysis_method="tree-sitter-range-v1",
        raw_initializer=" < ".join(chain),
        resolved_value=",".join(chain),
        structured_fact={"mro": chain},
        source_evidence_ids=evidence_ids,
        declaration_location=loc_of(cls),
        assumptions=version_assumptions,
        parser_version=_p.parser_version(),
    )


def resolve_python_method_owner_fact(
    evidence: Any,
    symbol: str,
    *,
    class_scope: str,
    language_version: object = None,
) -> CodeFactResolutionV2:
    """First MRO entry defining the method: the statically selected implementation."""
    from investigation_agent_platform.application.analysis.python import literals as _lit
    from investigation_agent_platform.domain.provenance.models import SourceLocation

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
        return CodeFactResolutionV2(
            status=CodeFactStatus.UNRESOLVED,
            repository=repository or "unknown",
            revision=revision or None,
            fact_category=JavaFactCategory.DECLARATION_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            declaring_type=class_scope or None,
            field_name=symbol,
            subject_identity=f"{class_scope}.{symbol}" if class_scope else symbol,
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
    if not class_scope or not class_scope.strip():
        return unresolved("missing_class_scope")
    class_scope = class_scope.strip()
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

    def loc_of(node: Any) -> Any:
        return location.model_copy(
            update={
                "line_start": lines[0].number + node.start_point[0],
                "line_end": lines[0].number + node.end_point[0],
            }
        )

    matches = list(_class_nodes(tree.root_node, source_bytes, class_scope))
    if len(matches) != 1:
        return unresolved("class_not_found" if not matches else "ambiguous_class_definition")
    all_classes: dict[str, Any] = {}
    for node in _p.walk(tree.root_node):
        if node.type == "class_definition":
            if node.parent is not None and node.parent.type == "decorated_definition":
                continue
            name = node.child_by_field_name("name")
            if name is not None:
                all_classes[_p.node_text(name, source_bytes).strip()] = node
    bases: dict[str, list[str]] = {}
    for name, node in all_classes.items():
        b = _base_names(node, source_bytes)
        if b is None:
            return unresolved("unparseable_base_list")
        bases[name] = b
    reachable: set[str] = set()
    stack = [class_scope]
    unknown_base: str | None = None
    while stack:
        current = stack.pop()
        if current in reachable or current == "object":
            continue
        reachable.add(current)
        for base in bases.get(current, []):
            short = base.rsplit(".", 1)[-1]
            if base == "object":
                continue
            if short in all_classes:
                stack.append(short)
            elif base in all_classes:
                stack.append(base)
            else:
                unknown_base = base
    if unknown_base is not None:
        return CodeFactResolutionV2(
            status=CodeFactStatus.PARTIAL,
            repository=repository,
            revision=revision,
            fact_category=JavaFactCategory.DECLARATION_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            declaring_type=class_scope,
            field_name=symbol,
            subject_identity=f"{class_scope}.{symbol}",
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            language_version=version_label,
            analysis_method="tree-sitter-range-v1",
            source_evidence_ids=evidence_ids,
            declaration_location=loc_of(matches[0]),
            limitation_codes=("cross_module_base_class",),
            assumptions=("runtime_receiver_unknown",),
            parser_version=_p.parser_version(),
        )
    chain = _c3(class_scope, bases)
    if chain is None:
        return unresolved("mro_linearization_failed")
    for owner in chain:
        if owner == "object":
            continue
        node = all_classes.get(owner) or all_classes.get(owner.rsplit(".", 1)[-1])
        if node is None:
            continue
        if _defines_method(node, source_bytes, symbol) is not None:
            short_owner = owner.rsplit(".", 1)[-1]
            return CodeFactResolutionV2(
                status=CodeFactStatus.RESOLVED,
                repository=repository,
                revision=revision,
                fact_category=JavaFactCategory.DECLARATION_FACT,
                semantic_stage=SemanticStage.DECLARATION,
                declaring_type=short_owner,
                field_name=symbol,
                subject_identity=f"{short_owner}.{symbol}",
                value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
                language_version=version_label,
                analysis_method="tree-sitter-range-v1",
                raw_initializer=f"{short_owner}.{symbol}",
                resolved_value=f"{short_owner}.{symbol}",
                structured_fact={"method_owner": short_owner, "mro": chain},
                source_evidence_ids=evidence_ids,
                declaration_location=loc_of(node),
                assumptions=(*version_assumptions, "runtime_receiver_unknown"),
                parser_version=_p.parser_version(),
            )
    return unresolved("method_not_found_in_mro")
