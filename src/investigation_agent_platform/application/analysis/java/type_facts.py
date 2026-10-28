# src/investigation_agent_platform/application/analysis/java/type_facts.py
"""Deterministic Java generics/lambda/record/sealed/pattern resolution (Phase 6, D6).

``resolve_java_type_fact`` reports statically established *declared* type and
advanced-language-feature shapes inside one revision-pinned source window as
``LANGUAGE_SEMANTIC_FACT``. It never claims execution, ordering, or
runtime-effective behavior.

Mandatory abstention defaults (D6):

- ``language_version`` is caller-attested only (normalized via
  ``literals.normalize_source_level``); unknown version plus version-gated
  syntax (records, sealed, patterns, switch expressions) yields
  ``unsupported_language_version`` rather than a guessed parse.
- Resolve only when every branch target (``permits`` type, pattern/case
  target type) is declared in-window (or implicitly available), and type
  erasure does not change the answer; otherwise abstain with explicit codes
  (``branch_target_outside_window``, ``type_erasure_changes_answer``).
- Lambdas resolve only when captures bind in-window and the lambda does not
  escape the analyzed scope; otherwise ``lambda_capture_outside_window`` or
  ``lambda_escapes_scope`` (PARTIAL). Method references resolve only to an
  in-window target, else ``method_reference_target_outside_window``.
- Generic bounds are recorded as declared text, never resolved.
- ``synchronized``/``volatile``/atomics/async markers always yield PARTIAL
  with ``nondeterministic_scheduling``/``requires_runtime_observation``/
  ``async_boundary``.
"""

from __future__ import annotations

import re
from typing import Any

from investigation_agent_platform.application.analysis.java import _parse as _p
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
from investigation_agent_platform.domain.provenance.models import SourceLocation

_DECLARATION_TYPES = frozenset(
    {
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "record_declaration",
        "annotation_type_declaration",
    }
)

# Minimum caller-attested source level per gated syntax (JLS release history).
_VERSION_GATES: dict[str, int] = {
    "record_declaration": 16,
    "compact_constructor_declaration": 16,
    "sealed": 17,
    "non-sealed": 17,
    "permits": 17,
    "switch_expression": 14,
    "pattern": 16,
    "type_pattern": 16,
    "guard": 16,
    "lambda_expression": 8,
    "method_reference": 8,
    "type_parameters": 5,
    "type_parameter": 5,
    "type_arguments": 5,
    "generic_type": 5,
    "wildcard": 5,
}

# Types implicitly available without an in-window declaration. Branch-target
# completeness only demands declarations for names outside this set.
_IMPLICIT_TYPES = frozenset(
    {
        "boolean",
        "byte",
        "short",
        "int",
        "long",
        "char",
        "float",
        "double",
        "void",
        "var",
        "Object",
        "String",
        "Integer",
        "Long",
        "Double",
        "Float",
        "Boolean",
        "Character",
        "Byte",
        "Short",
        "Number",
        "Class",
        "Enum",
        "Record",
        "Exception",
        "RuntimeException",
        "Error",
        "Throwable",
    }
)

_CONCURRENCY_PARTIAL = (
    "synchronized",
    "volatile ",
    "volatile\t",
    "Atomic",
    "CompletableFuture",
    "Executor",
    "new Thread",
    "Thread(",
    "Flux",
    "Mono<",
    "Mono.",
    "Flux.",
    "reactor.",
    ".subscribe(",
)

_ERASURE_INSTANCEOF_RE = re.compile(r"instanceof\s+[\w.$]+\s*<")
_ERASURE_CAST_RE = re.compile(r"\(\s*[\w.$]+\s*<[^;{}]*>\)")
_ERASURE_CATCH_RE = re.compile(r"catch\s*\(\s*[\w.$]+\s*<")

_AUTHORED_WORDS_BANNED = (
    "executes",
    "executed",
    "will run",
    "does run",
    "actually ran",
    "was executed",
    "observed execution",
    "takes effect",
    "is applied",
)


def _base_unresolved(
    repository: str,
    revision: str | None,
    subject: str,
    code: str,
    evidence_ids: tuple[Any, ...],
    language_version: str | None,
    *,
    partial: bool = False,
) -> CodeFactResolutionV2:
    return CodeFactResolutionV2(
        status=CodeFactStatus.PARTIAL if partial else CodeFactStatus.UNRESOLVED,
        repository=repository or "unknown",
        revision=revision or None,
        fact_category=JavaFactCategory.LANGUAGE_SEMANTIC_FACT,
        semantic_stage=SemanticStage.DECLARATION,
        declaring_type="unknown",
        subject_identity=subject or "unknown",
        value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
        language_version=language_version,
        analysis_method="tree-sitter-range-v1",
        source_evidence_ids=tuple(e for e in evidence_ids if e is not None),
        declaration_location=SourceLocation(system="Git", identifier="unknown"),
        limitation_codes=(code,) if "," not in code else tuple(code.split(",")),
        parser_version=_p.parser_version(),
    )


def _ref(file_path: str, first_line: int, node: Any) -> str:
    start = first_line + node.start_point[0]
    end = first_line + node.end_point[0]
    return f"{file_path}:{start}-{end}"


def _simple_name(text: str) -> str:
    return text.split(".")[-1].strip().strip("[]")


def _declared_type_names(root: Any, source: bytes) -> set[str]:
    """Simple names of all type declarations visible in the window."""
    names: set[str] = set()
    for node in _p.walk(root):
        if node.type in _DECLARATION_TYPES:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                names.add(_p.node_text(name_node, source).strip())
    return names


def _type_parameters_in_scope(node: Any, source: bytes) -> set[str]:
    """Type-variable names declared by enclosing type_parameters clauses."""
    names: set[str] = set()
    current: Any = node
    while current is not None:
        if current.type in _DECLARATION_TYPES | {"method_declaration"}:
            for child in current.children:
                if child.type == "type_parameters":
                    for param in child.children:
                        if param.type == "type_parameter":
                            for grand in param.children:
                                if grand.type == "type_identifier":
                                    names.add(_p.node_text(grand, source).strip())
        current = current.parent
    return names


def _gated_features(subtree: Any) -> set[str]:
    """Version-gated syntax node types present under a declaration."""
    found: set[str] = set()
    for node in _p.walk(subtree):
        if node.type in _VERSION_GATES:
            found.add(node.type)
        if node.type == "instanceof_expression" and len(node.children) > 3:
            found.add("pattern")
    return found


def _permits_names(declaration: Any, source: bytes) -> list[str]:
    names: list[str] = []
    for node in _p.walk(declaration):
        if node.type != "permits":
            continue
        for descendant in _p.walk(node):
            if descendant.type == "type_identifier" and descendant.parent is not node:
                text = _p.node_text(descendant, source).strip()
                if text and all(c not in text for c in "<>"):
                    names.append(_simple_name(text))
    return names


def _pattern_target_types(subtree: Any, source: bytes) -> list[str]:
    """Named types in pattern/case-target position (branch targets)."""
    names: list[str] = []
    for node in _p.walk(subtree):
        if node.type == "type_pattern":
            for child in node.children:
                if child.type in {"type_identifier", "scoped_type_identifier", "generic_type"}:
                    names.append(_simple_name(_p.node_text(child, source)))
        elif node.type == "instanceof_expression" and len(node.children) > 3:
            for child in node.children:
                if child.type in {"type_identifier", "scoped_type_identifier"}:
                    names.append(_simple_name(_p.node_text(child, source)))
    return names


def _record_components(declaration: Any, source: bytes) -> list[dict[str, str]]:
    components: list[dict[str, str]] = []
    for child in declaration.children:
        if child.type != "formal_parameters":
            continue
        for param in child.children:
            if param.type != "formal_parameter":
                continue
            type_text = ""
            name_text = ""
            for grand in param.children:
                if grand.type == "identifier":
                    name_text = _p.node_text(grand, source).strip()
                elif "type" in grand.type:
                    type_text = " ".join(_p.node_text(grand, source).split())
            if name_text:
                components.append({"name": name_text, "type": type_text})
    return components


def _generic_params(declaration: Any, source: bytes) -> list[dict[str, str]]:
    params: list[dict[str, str]] = []
    for child in declaration.children:
        if child.type != "type_parameters":
            continue
        for param in child.children:
            if param.type != "type_parameter":
                continue
            text = " ".join(_p.node_text(param, source).split())
            name = ""
            for grand in param.children:
                if grand.type == "type_identifier":
                    name = _p.node_text(grand, source).strip()
                    break
            params.append({"name": name, "declared": text})
    return params


def _switch_arms(subtree: Any, source: bytes) -> list[dict[str, str]]:
    arms: list[dict[str, str]] = []
    for node in _p.walk(subtree):
        if node.type not in {"switch_rule", "switch_block_statement_group"}:
            continue
        label = next(
            (c for c in node.children if c.type == "switch_label"),
            None,
        )
        label_text = " ".join(_p.node_text(label, source).split()) if label is not None else "arm"
        arms.append({"label": label_text})
    return arms


def _lambda_params(lambda_node: Any, source: bytes) -> list[str]:
    names: list[str] = []
    for child in lambda_node.children:
        if child.type in {"formal_parameters", "inferred_parameters"}:
            for descendant in _p.walk(child):
                if descendant.type == "identifier" and descendant.parent is not None:
                    if descendant.parent.type in {
                        "formal_parameter",
                        "inferred_parameters",
                        "formal_parameters",
                    }:
                        names.append(_p.node_text(descendant, source).strip())
        elif child.type == "identifier":
            names.append(_p.node_text(child, source).strip())
    return names


def _lambda_body(lambda_node: Any) -> Any | None:
    seen_arrow = False
    for child in lambda_node.children:
        if child.type == "->":
            seen_arrow = True
            continue
        if seen_arrow:
            return child
    return None


def _enclosing_method_or_field(node: Any) -> Any | None:
    current: Any = node.parent
    while current is not None:
        if current.type in {
            "method_declaration",
            "field_declaration",
            "compact_constructor_declaration",
        }:
            return current
        current = current.parent
    return None


def _enclosing_type(node: Any) -> Any | None:
    current: Any = node.parent
    while current is not None:
        if current.type in _DECLARATION_TYPES:
            return current
        current = current.parent
    return None


def _method_scope_names(scope: Any, source: bytes) -> tuple[set[str], set[str]]:
    """Return (parameter names, local names) declared in a method-like scope."""
    params: set[str] = set()
    locals_: set[str] = set()
    for child in scope.children:
        if child.type == "formal_parameters":
            for param in child.children:
                if param.type != "formal_parameter":
                    continue
                for grand in param.children:
                    if grand.type == "identifier":
                        params.add(_p.node_text(grand, source).strip())
    for node in _p.walk(scope):
        if node.type == "variable_declarator":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                locals_.add(_p.node_text(name_node, source).strip())
    return params, locals_


def _field_names(type_node: Any, source: bytes) -> set[str]:
    names: set[str] = set()
    body = next((c for c in type_node.children if c.type == "class_body"), None)
    if body is None:
        return names
    for child in body.children:
        if child.type != "field_declaration":
            continue
        for declarator in child.children:
            if declarator.type != "variable_declarator":
                continue
            name_node = declarator.child_by_field_name("name")
            if name_node is not None:
                names.add(_p.node_text(name_node, source).strip())
    return names


def _lambda_free_identifiers(lambda_node: Any, source: bytes) -> set[str]:
    """Identifiers used in a lambda body that are not locally bound.

    Node identity uses source spans: tree-sitter hands out a fresh Python
    wrapper per access, so ``id()`` comparison never matches.
    """
    body = _lambda_body(lambda_node)
    if body is None:
        return set()
    bound: set[tuple[int, int]] = set()
    for node in _p.walk(body):
        if node.type == "variable_declarator":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                bound.add(_span(name_node))
        elif node.type == "formal_parameter" or node.type == "type_pattern":
            for grand in node.children:
                if grand.type == "identifier":
                    bound.add(_span(grand))
        elif node.type == "instanceof_expression" and len(node.children) > 3:
            bound.add(_span(node.children[-1]))
        elif node.type == "method_invocation":
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                bound.add(_span(name_node))
            obj = node.child_by_field_name("object")
            if obj is not None:
                for descendant in _p.walk(obj):
                    if descendant.type == "identifier":
                        bound.add(_span(descendant))
        elif node.type == "switch_label":
            for descendant in _p.walk(node):
                if descendant.type == "identifier":
                    bound.add(_span(descendant))
    params = set(_lambda_params(lambda_node, source))
    free: set[str] = set()
    for node in _p.walk(body):
        if node.type != "identifier" or _span(node) in bound:
            continue
        text = _p.node_text(node, source).strip()
        if text and text not in params:
            free.add(text)
    return free


def _lambda_escapes(lambda_node: Any) -> bool:
    """True when the lambda crosses the analyzed scope boundary."""
    current: Any = lambda_node.parent
    while current is not None:
        if current.type in {"argument_list", "return_statement"}:
            return True
        if current.type in {
            "method_declaration",
            "field_declaration",
            "compact_constructor_declaration",
        }:
            return current.type == "field_declaration"
        current = current.parent
    return True


def _method_reference_target(ref_node: Any, source: bytes) -> str:
    return " ".join(_p.node_text(ref_node, source).split())


def _find_declarations(root: Any, source: bytes, symbol_name: str) -> tuple[list[Any], str | None]:
    primary = []
    ctors = []
    for node in _p.walk(root):
        name_node = node.child_by_field_name("name")
        if name_node is None or _p.node_text(name_node, source) != symbol_name:
            continue
        if node.type in _DECLARATION_TYPES | {"method_declaration"}:
            primary.append(node)
        elif node.type in {"constructor_declaration", "compact_constructor_declaration"}:
            ctors.append(node)
    if primary:
        if len(primary) > 1:
            return [], "ambiguous_symbol"
        return primary, None
    if len(ctors) > 1:
        return [], "ambiguous_symbol"
    if not ctors:
        return [], "symbol_not_in_window"
    return ctors, None


def _span(node: Any) -> tuple[int, int]:
    return (node.start_byte, node.end_byte)


def build_type_analysis_snapshots(
    root: Any,
    source: bytes,
    *,
    file_path: str,
    language_version: int | None,
) -> list[Any]:
    """Build typed feature snapshots from the already parsed Java tree."""
    from investigation_agent_platform.application.analysis.java import model as _model

    declared_names = _declared_type_names(root, source)
    snapshots: list[Any] = []
    for declaration in _p.walk(root):
        if declaration.type not in _DECLARATION_TYPES:
            continue
        name_node = declaration.child_by_field_name("name")
        if name_node is None:
            continue
        name = _p.node_text(name_node, source).strip()
        enclosing = _enclosing_type(declaration)
        qualified = name
        if enclosing is not None:
            parent_name = enclosing.child_by_field_name("name")
            if parent_name is not None:
                qualified = f"{_p.node_text(parent_name, source).strip()}.{name}"
        gated = sorted(_gated_features(declaration))
        permits = _permits_names(declaration, source)
        type_vars = sorted(_type_parameters_in_scope(declaration, source))
        generic_params = _generic_params(declaration, source)
        pattern_types = _pattern_target_types(declaration, source)
        missing_permits = [
            item for item in permits if item not in declared_names and item not in _IMPLICIT_TYPES
        ]
        missing_targets = [
            item
            for item in pattern_types
            if item not in declared_names and item not in type_vars and item not in _IMPLICIT_TYPES
        ]
        features: list[Any] = []
        partial_codes: list[str] = []
        span = _model.Span(
            file_path=file_path,
            line_start=declaration.start_point[0] + 1,
            line_end=declaration.end_point[0] + 1,
        )
        ref = lambda node: _model.Span(
            file_path=file_path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
        )

        def feature(
            kind: str,
            detail: str,
            note: str,
            node: Any,
            arms=None,
            *,
            _features=features,
            _ref=ref,
        ) -> None:
            _features.append(
                _model.JavaFeature(
                    kind=kind,
                    detail=detail,
                    note=note,
                    span=_ref(node),
                    arms=arms or [],
                )
            )

        if declaration.type == "record_declaration":
            feature(
                "record_declaration",
                f"record {name} declares components {_record_components(declaration, source)}",
                "components recorded as declared; construction not established",
                declaration,
            )
        if any(node.type == "compact_constructor_declaration" for node in _p.walk(declaration)):
            feature(
                "compact_constructor",
                f"record {name} declares a compact constructor",
                "declared validation only; execution not established",
                declaration,
            )
        modifiers = [node for node in declaration.children if node.type == "modifiers"]
        modifier_text = " ".join(_p.node_text(node, source) for node in modifiers)
        if "sealed" in modifier_text.split() or "non-sealed" in modifier_text:
            feature(
                "sealed_hierarchy",
                f"sealed declaration {name} permits {permits}",
                "permits list recorded as declared; exhaustiveness not proven",
                declaration,
            )
        has_generics = bool(generic_params) or any(
            node.type in {"generic_type", "wildcard", "type_arguments"}
            for node in _p.walk(declaration)
        )
        if has_generics:
            feature(
                "generic_declaration",
                f"{name} declares type parameters {generic_params}",
                "bounds recorded as declared text, never resolved; erasure-insensitive shape only",
                declaration,
            )

        subtree_text = _p.node_text(declaration, source)
        erasure_sensitive = bool(
            _ERASURE_INSTANCEOF_RE.search(subtree_text)
            or _ERASURE_CAST_RE.search(subtree_text)
            or _ERASURE_CATCH_RE.search(subtree_text)
        )
        if erasure_sensitive:
            partial_codes.append("type_erasure_changes_answer")
        if language_version is None or any(
            language_version < _VERSION_GATES[item] for item in gated
        ):
            partial_codes.append("unsupported_language_version")
        if missing_permits or missing_targets:
            partial_codes.append("branch_target_outside_window")

        for node in _p.walk(declaration):
            if node.type == "lambda_expression":
                free = _lambda_free_identifiers(node, source)
                scope = _enclosing_method_or_field(node)
                known: set[str] = {"this"}
                if scope is not None:
                    params, locals_ = _method_scope_names(scope, source)
                    known |= params | locals_
                type_node = _enclosing_type(node)
                if type_node is not None:
                    known |= _field_names(type_node, source)
                unknown = sorted(item for item in free if item not in known)
                if unknown:
                    partial_codes.append("lambda_capture_outside_window")
                    detail = f"lambda with unresolvable captures {unknown}"
                    note = "capture binding requires out-of-window scope"
                elif _lambda_escapes(node):
                    partial_codes.append("lambda_escapes_scope")
                    detail = (
                        f"lambda with in-window captures {sorted(free)} crosses the analyzed scope"
                    )
                    note = "possible shape only; invocation and ordering not established"
                else:
                    detail = f"lambda with in-window captures {sorted(free)}"
                    note = "possible shape only; invocation and ordering not established"
                feature("lambda_shape", detail, note, node)
            elif node.type == "method_reference":
                target = _method_reference_target(node, source)
                receiver, member = target.split("::", 1) if "::" in target else (target, "")
                in_window = receiver.strip() in {item.rsplit(".", 1)[-1] for item in declared_names}
                if receiver.strip() in {"this", "super"}:
                    type_node = _enclosing_type(node)
                    members = {
                        _p.node_text(item.child_by_field_name("name"), source)
                        for item in _p.walk(type_node)
                        if type_node is not None
                        and item.type
                        in {
                            "method_declaration",
                            "constructor_declaration",
                            "compact_constructor_declaration",
                        }
                        and item.child_by_field_name("name") is not None
                    }
                    in_window = member.strip() in members
                if in_window:
                    feature(
                        "method_reference_shape",
                        f"method reference targets in-window {target}",
                        "target named, not proven to execute",
                        node,
                    )
                else:
                    partial_codes.append("method_reference_target_outside_window")
                    feature(
                        "method_reference_shape",
                        f"method reference {target} has no in-window target",
                        "target declaration outside the window",
                        node,
                    )
        if any(node.type == "switch_expression" for node in _p.walk(declaration)):
            arms = _switch_arms(declaration, source)
            feature(
                "switch_expression_shape",
                f"switch expression declares {len(arms)} arms ({'default present' if any('default' in arm['label'] for arm in arms) else 'no default arm'})",
                "possible arm selection only; the selector is not evaluated",
                declaration,
                arms,
            )
        if any(node.type == "type_pattern" for node in _p.walk(declaration)) or any(
            node.type == "instanceof_expression" and len(node.children) > 3
            for node in _p.walk(declaration)
        ):
            feature(
                "pattern_matching_shape",
                f"patterns bind target types {pattern_types or 'none named'}",
                "every pattern target type is declared in-window; match outcome not established",
                declaration,
            )

        snapshots.append(
            _model.JavaAnalysisSnapshot(
                declaration=_model.TypeDeclaration(
                    name=name,
                    qualified_name=qualified,
                    declaration_type=declaration.type,
                    gated_features=gated,
                    permits=permits,
                    generic_params=generic_params,
                    record_components=_record_components(declaration, source),
                    has_generics=has_generics,
                    type_variables=type_vars,
                    span=span,
                ),
                member_names=[
                    _p.node_text(node.child_by_field_name("name"), source).strip()
                    for node in _p.walk(declaration)
                    if node.type in {"method_declaration", "constructor_declaration"}
                    and node.child_by_field_name("name") is not None
                ],
                features=features,
                pattern_types=pattern_types,
                missing_permits=missing_permits,
                missing_pattern_targets=missing_targets,
                partial_codes=list(dict.fromkeys(partial_codes)),
                erasure_sensitive=erasure_sensitive,
            )
        )
    return snapshots


def _resolve_java_type_fact_legacy(
    evidence: Any,
    symbol_name: str,
    *,
    feature_hint: str | None = None,
    language_version: object = None,
) -> CodeFactResolutionV2:
    """Report declared type-feature shapes for one in-window symbol."""
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
        return _base_unresolved(repository, revision, subject, reason, evidence_ids, level_text)
    assert location is not None
    if not symbol_name or not symbol_name.strip():
        return _base_unresolved(
            repository, revision, subject, "missing_symbol_name", evidence_ids, level_text
        )
    if not source_path.lower().endswith(".java"):
        return _base_unresolved(
            repository, revision, subject, "not_a_java_source_file", evidence_ids, level_text
        )
    if not repository:
        return _base_unresolved(
            repository,
            revision,
            subject,
            "repository_identity_unavailable",
            evidence_ids,
            level_text,
        )

    window_text = "\n".join(line.text for line in lines)
    if any(marker in window_text for marker in _CONCURRENCY_PARTIAL):
        codes = (
            ("async_boundary", "requires_runtime_observation")
            if any(m in window_text for m in ("CompletableFuture", "Executor", "Flux", "Mono"))
            else ("nondeterministic_scheduling", "requires_runtime_observation")
        )
        return _base_unresolved(
            repository, revision, subject, ",".join(codes), evidence_ids, level_text, partial=True
        )

    source_bytes = window_text.encode("utf-8")
    try:
        tree = _p.parse_java(source_bytes)
    except Exception as exc:
        return _base_unresolved(
            repository,
            revision,
            subject,
            f"java_parser_unavailable:{type(exc).__name__}",
            evidence_ids,
            level_text,
        )
    if tree.root_node.has_error:
        return _base_unresolved(
            repository,
            revision,
            subject,
            "java_syntax_error_or_incomplete_source_window",
            evidence_ids,
            level_text,
        )

    declarations, failure = _find_declarations(tree.root_node, source_bytes, symbol_name)
    if not declarations:
        assert failure is not None
        return _base_unresolved(repository, revision, subject, failure, evidence_ids, level_text)
    declaration = declarations[0]
    first_line = lines[0].number
    subtree_text = _p.node_text(declaration, source_bytes)

    gated = _gated_features(declaration)
    unsupported = [g for g in gated if level is None or level < _VERSION_GATES[g]]
    if unsupported:
        return _base_unresolved(
            repository, revision, subject, "unsupported_language_version", evidence_ids, level_text
        )

    if (
        _ERASURE_INSTANCEOF_RE.search(subtree_text)
        or _ERASURE_CAST_RE.search(subtree_text)
        or _ERASURE_CATCH_RE.search(subtree_text)
    ):
        return _base_unresolved(
            repository, revision, subject, "type_erasure_changes_answer", evidence_ids, level_text
        )

    declared_names = _declared_type_names(tree.root_node, source_bytes)
    type_vars = _type_parameters_in_scope(declaration, source_bytes)

    permits = _permits_names(declaration, source_bytes)
    missing_permits = [
        name for name in permits if name not in declared_names and name not in _IMPLICIT_TYPES
    ]
    pattern_types = _pattern_target_types(declaration, source_bytes)
    missing_targets = [
        name
        for name in pattern_types
        if name not in declared_names and name not in type_vars and name not in _IMPLICIT_TYPES
    ]
    if missing_permits or missing_targets:
        return _base_unresolved(
            repository,
            revision,
            subject,
            "branch_target_outside_window",
            evidence_ids,
            level_text,
            partial=True,
        )

    features: list[dict[str, Any]] = []
    partial_codes: list[str] = []

    if declaration.type == "record_declaration":
        features.append(
            {
                "kind": "record_declaration",
                "detail": f"record {symbol_name} declares components "
                f"{_record_components(declaration, source_bytes)}",
                "range": _ref(source_path, first_line, declaration),
                "note": "components recorded as declared; construction not established",
            }
        )
    if any(child.type == "compact_constructor_declaration" for child in _p.walk(declaration)):
        features.append(
            {
                "kind": "compact_constructor",
                "detail": f"record {symbol_name} declares a compact constructor",
                "range": _ref(source_path, first_line, declaration),
                "note": "declared validation only; execution not established",
            }
        )
    sealed_mods = [n for n in declaration.children if n.type == "modifiers"]
    sealed_text = " ".join(_p.node_text(m, source_bytes) for m in sealed_mods)
    if "sealed" in sealed_text.split() or "non-sealed" in sealed_text:
        features.append(
            {
                "kind": "sealed_hierarchy",
                "detail": f"sealed declaration {symbol_name} permits {permits}",
                "range": _ref(source_path, first_line, declaration),
                "note": "permits list recorded as declared; exhaustiveness not proven",
            }
        )
    generic_params = _generic_params(declaration, source_bytes)
    has_generics = bool(generic_params) or any(
        n.type in {"generic_type", "wildcard", "type_arguments"} for n in _p.walk(declaration)
    )
    if has_generics:
        features.append(
            {
                "kind": "generic_declaration",
                "detail": f"{symbol_name} declares type parameters {generic_params}",
                "range": _ref(source_path, first_line, declaration),
                "note": "bounds recorded as declared text, never resolved; "
                "erasure-insensitive shape only",
            }
        )

    for node in _p.walk(declaration):
        if node.type == "lambda_expression":
            free = _lambda_free_identifiers(node, source_bytes)
            scope = _enclosing_method_or_field(node)
            known: set[str] = {"this"}
            if scope is not None:
                params, locals_ = _method_scope_names(scope, source_bytes)
                known |= params | locals_
            type_node = _enclosing_type(node)
            if type_node is not None:
                known |= _field_names(type_node, source_bytes)
            unknown = sorted(n for n in free if n not in known)
            if unknown:
                partial_codes.append("lambda_capture_outside_window")
                features.append(
                    {
                        "kind": "lambda_shape",
                        "detail": f"lambda with unresolvable captures {unknown}",
                        "range": _ref(source_path, first_line, node),
                        "note": "capture binding requires out-of-window scope",
                    }
                )
            elif _lambda_escapes(node):
                partial_codes.append("lambda_escapes_scope")
                features.append(
                    {
                        "kind": "lambda_shape",
                        "detail": f"lambda with in-window captures {sorted(free)} "
                        "crosses the analyzed scope",
                        "range": _ref(source_path, first_line, node),
                        "note": "possible shape only; invocation and ordering not established",
                    }
                )
            else:
                features.append(
                    {
                        "kind": "lambda_shape",
                        "detail": f"lambda with in-window captures {sorted(free)}",
                        "range": _ref(source_path, first_line, node),
                        "note": "possible shape only; invocation and ordering not established",
                    }
                )
        elif node.type == "method_reference":
            target = _method_reference_target(node, source_bytes)
            receiver = target.split("::")[0].strip() if "::" in target else target
            member = target.split("::")[1].strip() if "::" in target else ""
            in_window = False
            if receiver in {"this", "super"}:
                type_node = _enclosing_type(node)
                if type_node is not None:
                    members = {
                        _p.node_text(m.child_by_field_name("name"), source_bytes)
                        for m in _p.walk(type_node)
                        if m.type
                        in {
                            "method_declaration",
                            "compact_constructor_declaration",
                            "constructor_declaration",
                        }
                        and m.child_by_field_name("name") is not None
                    }
                    in_window = (
                        member == "new"
                        and _p.node_text(
                            type_node.child_by_field_name("name"), source_bytes
                        ).strip()
                        != ""
                    ) or member in members
            elif _simple_name(receiver) in declared_names:
                in_window = True
            if in_window:
                features.append(
                    {
                        "kind": "method_reference_shape",
                        "detail": f"method reference targets in-window {target}",
                        "range": _ref(source_path, first_line, node),
                        "note": "target named, not proven to execute",
                    }
                )
            else:
                partial_codes.append("method_reference_target_outside_window")
                features.append(
                    {
                        "kind": "method_reference_shape",
                        "detail": f"method reference {target} has no in-window target",
                        "range": _ref(source_path, first_line, node),
                        "note": "target declaration outside the window",
                    }
                )

    if any(n.type == "switch_expression" for n in _p.walk(declaration)):
        arms = _switch_arms(declaration, source_bytes)
        has_default = any("default" in arm["label"] for arm in arms)
        features.append(
            {
                "kind": "switch_expression_shape",
                "detail": f"switch expression declares {len(arms)} arms "
                f"({'default present' if has_default else 'no default arm'})",
                "range": _ref(source_path, first_line, declaration),
                "note": "possible arm selection only; the selector is not evaluated",
                "arms": arms,
            }
        )
    if any(n.type in {"type_pattern", "pattern"} for n in _p.walk(declaration)) or any(
        n.type == "instanceof_expression" and len(n.children) > 3 for n in _p.walk(declaration)
    ):
        features.append(
            {
                "kind": "pattern_matching_shape",
                "detail": f"patterns bind target types {pattern_types or 'none named'}",
                "range": _ref(source_path, first_line, declaration),
                "note": "every pattern target type is declared in-window; "
                "match outcome not established",
            }
        )

    if feature_hint is not None:
        hint = feature_hint.strip().lower()
        if not hint:
            return _base_unresolved(
                repository, revision, subject, "feature_hint_not_found", evidence_ids, level_text
            )
        features = [
            f
            for f in features
            if hint in str(f["kind"]).lower() or hint in str(f["detail"]).lower()
        ]
        if not features:
            return _base_unresolved(
                repository, revision, subject, "feature_hint_not_found", evidence_ids, level_text
            )
    if not features:
        return _base_unresolved(
            repository,
            revision,
            subject,
            "no_supported_type_feature_in_window",
            evidence_ids,
            level_text,
        )

    assumptions = [
        "declared shapes only; no shape is established as taken, run, or observed",
        "generic bounds recorded as declared text, never resolved",
        "language_version is caller-attested, not build authority",
    ]
    structured_fact = {
        "kind": "type_features",
        "symbol": symbol_name,
        "features": features,
        "notes": list(assumptions),
    }
    authored = " ".join([f["kind"] + " " + f["note"] for f in features] + assumptions).lower()
    assert not any(word in authored for word in _AUTHORED_WORDS_BANNED)

    declaration_location = location.model_copy(
        update={
            "line_start": first_line + declaration.start_point[0],
            "line_end": first_line + declaration.end_point[0],
        }
    )
    refs = [_ref(source_path, first_line, declaration)]
    refs.extend(str(f["range"]) for f in features if f["range"] != refs[0])
    if partial_codes:
        return CodeFactResolutionV2(
            status=CodeFactStatus.PARTIAL,
            repository=repository,
            revision=revision,
            fact_category=JavaFactCategory.LANGUAGE_SEMANTIC_FACT,
            semantic_stage=SemanticStage.DECLARATION,
            declaring_type="unknown",
            subject_identity=f"{source_path}#{symbol_name}",
            value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
            language_version=level_text,
            analysis_method="tree-sitter-range-v1",
            structured_fact=structured_fact,
            control_flow_refs=tuple(refs),
            source_evidence_ids=evidence_ids,
            declaration_location=declaration_location,
            limitation_codes=tuple(dict.fromkeys(partial_codes)),
            assumptions=tuple(assumptions),
            parser_version=_p.parser_version(),
        )
    return CodeFactResolutionV2(
        status=CodeFactStatus.RESOLVED,
        repository=repository,
        revision=revision,
        fact_category=JavaFactCategory.LANGUAGE_SEMANTIC_FACT,
        semantic_stage=SemanticStage.DECLARATION,
        declaring_type="unknown",
        subject_identity=f"{source_path}#{symbol_name}",
        value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
        language_version=level_text,
        analysis_method="tree-sitter-range-v1",
        structured_fact=structured_fact,
        control_flow_refs=tuple(refs),
        source_evidence_ids=evidence_ids,
        declaration_location=declaration_location,
        assumptions=tuple(assumptions),
        parser_version=_p.parser_version(),
    )


def resolve_java_type_fact(
    evidence: Any,
    symbol_name: str,
    *,
    feature_hint: str | None = None,
    language_version: object = None,
) -> CodeFactResolutionV2:
    """Resolve type features from the shared Java model snapshot."""
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
        return _base_unresolved(repository, revision, subject, reason, evidence_ids, level_text)
    assert location is not None
    if not symbol_name or not symbol_name.strip():
        return _base_unresolved(
            repository, revision, subject, "missing_symbol_name", evidence_ids, level_text
        )
    if not source_path.lower().endswith(".java"):
        return _base_unresolved(
            repository, revision, subject, "not_a_java_source_file", evidence_ids, level_text
        )
    if not repository:
        return _base_unresolved(
            repository,
            revision,
            subject,
            "repository_identity_unavailable",
            evidence_ids,
            level_text,
        )
    window_text = "\n".join(line.text for line in lines)
    if any(marker in window_text for marker in _CONCURRENCY_PARTIAL):
        codes = (
            ("async_boundary", "requires_runtime_observation")
            if any(
                marker in window_text
                for marker in ("CompletableFuture", "Executor", "Flux", "Mono")
            )
            else ("nondeterministic_scheduling", "requires_runtime_observation")
        )
        return _base_unresolved(
            repository, revision, subject, ",".join(codes), evidence_ids, level_text, partial=True
        )
    # Function-local to match the resolver-wide convention (method_facts <-> model would cycle if hoisted).
    from investigation_agent_platform.application.analysis.java import model as _model

    try:
        code_model = _model.build_java_model(
            window_text.encode("utf-8"),
            file_path=source_path,
            repository=repository,
            revision=revision,
            language_version=level,
        )
    except Exception as exc:
        return _base_unresolved(
            repository,
            revision,
            subject,
            f"java_parser_unavailable:{type(exc).__name__}",
            evidence_ids,
            level_text,
        )
    if code_model.has_syntax_error:
        return _base_unresolved(
            repository,
            revision,
            subject,
            "java_syntax_error_or_incomplete_source_window",
            evidence_ids,
            level_text,
        )
    snapshots = [
        item for item in code_model.analysis_snapshots if item.declaration.name == symbol_name
    ]
    if not snapshots:
        snapshots = [
            item for item in code_model.analysis_snapshots if symbol_name in item.member_names
        ]
    if not snapshots:
        return _base_unresolved(
            repository, revision, subject, "symbol_not_in_window", evidence_ids, level_text
        )
    if len(snapshots) > 1:
        return _base_unresolved(
            repository, revision, subject, "ambiguous_symbol", evidence_ids, level_text
        )
    snapshot = snapshots[0]
    codes = list(snapshot.partial_codes)
    if "unsupported_language_version" in codes:
        return _base_unresolved(
            repository, revision, subject, "unsupported_language_version", evidence_ids, level_text
        )
    if "type_erasure_changes_answer" in codes:
        return _base_unresolved(
            repository, revision, subject, "type_erasure_changes_answer", evidence_ids, level_text
        )
    first_line = lines[0].number
    features = [
        {
            "kind": item.kind,
            "detail": item.detail,
            "range": f"{item.span.file_path}:{item.span.line_start + first_line - 1}-{item.span.line_end + first_line - 1}",
            "note": item.note,
            **({"arms": item.arms} if item.arms else {}),
        }
        for item in snapshot.features
    ]
    if feature_hint is not None:
        hint = feature_hint.strip().lower()
        features = [
            item
            for item in features
            if hint in item["kind"].lower() or hint in item["detail"].lower()
        ]
        if not features:
            return _base_unresolved(
                repository, revision, subject, "feature_hint_not_found", evidence_ids, level_text
            )
    if not features:
        return _base_unresolved(
            repository,
            revision,
            subject,
            "no_supported_type_feature_in_window",
            evidence_ids,
            level_text,
        )
    assumptions = [
        "declared shapes only; no shape is established as taken, run, or observed",
        "generic bounds recorded as declared text, never resolved",
        "language_version is caller-attested, not build authority",
    ]
    structured_fact = {
        "kind": "type_features",
        "symbol": symbol_name,
        "features": features,
        "notes": assumptions,
    }
    refs = [
        f"{source_path}:{snapshot.declaration.span.line_start + first_line - 1}-{snapshot.declaration.span.line_end + first_line - 1}"
    ]
    refs.extend(item["range"] for item in features if item["range"] != refs[0])
    declaration_location = location.model_copy(
        update={
            "line_start": snapshot.declaration.span.line_start + first_line - 1,
            "line_end": snapshot.declaration.span.line_end + first_line - 1,
        }
    )
    status = CodeFactStatus.PARTIAL if codes else CodeFactStatus.RESOLVED
    return CodeFactResolutionV2(
        status=status,
        repository=repository,
        revision=revision,
        fact_category=JavaFactCategory.LANGUAGE_SEMANTIC_FACT,
        semantic_stage=SemanticStage.DECLARATION,
        declaring_type="unknown",
        subject_identity=f"{source_path}#{symbol_name}",
        value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
        language_version=level_text,
        analysis_method="tree-sitter-range-v1",
        structured_fact=structured_fact,
        control_flow_refs=tuple(refs),
        source_evidence_ids=evidence_ids,
        declaration_location=declaration_location,
        limitation_codes=tuple(dict.fromkeys(codes)),
        assumptions=tuple(assumptions),
        parser_version=code_model.parser_version,
    )
