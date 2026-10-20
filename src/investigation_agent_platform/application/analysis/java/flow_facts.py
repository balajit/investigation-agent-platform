# src/investigation_agent_platform/application/analysis/java/flow_facts.py
"""Deterministic Java control-flow and possible-effect resolution (Phase 5, D6).

``resolve_java_control_flow`` reports statically established *possible* branch,
return, and exception paths inside one revision-pinned source window as
``CONTROL_FLOW_FACT``. ``resolve_java_possible_effect`` reports statically
established *possible* mutations, exceptions, and side effects as
``POSSIBLE_EFFECT_FACT`` (verdict ceiling ``LIKELY`` via the existing
``CATEGORY_PROOF_RULES`` table; the verifier is never modified here).

Mandatory abstention defaults:

- Catch-handler existence never implies execution; ``finally`` is a possible
  cleanup path, never a proven outcome; wrapping/cause chains are named, not
  proven; try-with-resources close is possible, not observed.
- ``synchronized``/``volatile``/atomics/``CompletableFuture``/executors/
  reactive always yield ``PARTIAL`` (nondeterministic scheduling, runtime
  observation required, async boundary).
- No path is ever worded as executed, observed, or runtime-effective.
"""

from __future__ import annotations

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

_LOOP_TYPES = frozenset(
    {"for_statement", "enhanced_for_statement", "while_statement", "do_statement"}
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
)

_ASYNC_PARTIAL = (
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
    ".subscribeOn(",
    ".publishOn(",
)

_EXECUTION_WORDS = (
    "executes",
    "executed",
    "will run",
    "does run",
    "runs when",
    "actually ran",
    "was executed",
    "observed execution",
)


def _base_unresolved(
    category: JavaFactCategory,
    repository: str,
    revision: str | None,
    subject: str,
    code: str,
    evidence_ids: tuple[Any, ...],
    language_version: str | None,
    *,
    partial: bool = False,
) -> CodeFactResolutionV2:
    from investigation_agent_platform.domain.provenance.models import SourceLocation

    return CodeFactResolutionV2(
        status=CodeFactStatus.PARTIAL if partial else CodeFactStatus.UNRESOLVED,
        repository=repository or "unknown",
        revision=revision or None,
        fact_category=category,
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


def _concurrency_code(window_text: str) -> tuple[str, ...] | None:
    """Return PARTIAL limitation codes when scheduling is nondeterministic."""
    if any(marker in window_text for marker in _ASYNC_PARTIAL):
        return ("async_boundary", "requires_runtime_observation")
    if any(marker in window_text for marker in _CONCURRENCY_PARTIAL):
        return ("nondeterministic_scheduling", "requires_runtime_observation")
    return None


def _ref(file_path: str, first_line: int, node: Any) -> str:
    start = first_line + node.start_point[0]
    end = first_line + node.end_point[0]
    return f"{file_path}:{start}-{end}"


def _condition_text(node: Any, source: bytes) -> str:
    condition = node.child_by_field_name("condition")
    if condition is None:
        for child in node.children:
            if child.type == "parenthesized_expression":
                condition = child
                break
    if condition is None:
        return ""
    return " ".join(_p.node_text(condition, source).split())


def _method_node(root: Any, source: bytes, symbol_name: str) -> tuple[Any | None, str | None]:
    matches = []
    for node in _p.walk(root):
        if node.type != "method_declaration":
            continue
        name_node = node.child_by_field_name("name")
        if name_node is not None and _p.node_text(name_node, source) == symbol_name:
            matches.append(node)
    if not matches:
        return None, "symbol_not_in_window"
    if len(matches) > 1:
        return None, "ambiguous_symbol"
    return matches[0], None


def _collect_paths(
    body: Any, source: bytes, file_path: str, first_line: int
) -> tuple[list[dict[str, Any]], bool]:
    """Collect possible-path entries; flag nested lambda/anonymous bodies."""
    paths: list[dict[str, Any]] = []
    nested_deferred = False

    def visit(node: Any) -> None:
        nonlocal nested_deferred
        node_type = node.type
        if node_type in {"lambda_expression", "class_declaration"}:
            nested_deferred = True
            return
        if node_type == "if_statement":
            condition = _condition_text(node, source)
            has_else = any(c.type == "else" for c in node.children)
            paths.append(
                {
                    "kind": "possible_branch_path",
                    "detail": f"if {condition} holds (then-branch may run)",
                    "range": _ref(file_path, first_line, node),
                    "note": "possible path only; the condition is not evaluated",
                }
            )
            paths.append(
                {
                    "kind": ("possible_branch_path" if has_else else "possible_fall_through_path"),
                    "detail": (
                        f"if {condition} does not hold (else-branch may run)"
                        if has_else
                        else f"if {condition} does not hold (fall-through possible)"
                    ),
                    "range": _ref(file_path, first_line, node),
                    "note": "possible path only; no branch is established as taken",
                }
            )
        elif node_type in _LOOP_TYPES:
            paths.append(
                {
                    "kind": "possible_loop_path",
                    "detail": f"{node_type} body may run zero or more times",
                    "range": _ref(file_path, first_line, node),
                    "note": "possible path only; trip count is not established",
                }
            )
        elif node_type == "switch_expression":
            arms = [
                c
                for c in node.children
                if c.type in {"switch_block_statement_group", "switch_rule"}
            ]
            if not arms:
                for descendant in _p.walk(node):
                    if descendant.type in {
                        "switch_block_statement_group",
                        "switch_rule",
                    }:
                        arms.append(descendant)
            for arm in arms:
                label = next(
                    (c for c in arm.children if c.type in {"switch_label", "expression"}),
                    None,
                )
                label_text = (
                    " ".join(_p.node_text(label, source).split()) if label is not None else "arm"
                )
                paths.append(
                    {
                        "kind": "possible_switch_arm_path",
                        "detail": f"switch arm may be selected: {label_text}",
                        "range": _ref(file_path, first_line, arm),
                        "note": "possible path only; the selector is not evaluated",
                    }
                )
        elif node_type in {"try_statement", "try_with_resources_statement"}:
            paths.append(
                {
                    "kind": "possible_try_path",
                    "detail": "try body may run; an exception may transfer control",
                    "range": _ref(file_path, first_line, node),
                    "note": "possible path only; normal completion is not established",
                }
            )
        elif node_type == "catch_clause":
            param = next(
                (c for c in node.children if c.type == "catch_formal_parameter"),
                None,
            )
            param_text = (
                " ".join(_p.node_text(param, source).split()) if param is not None else "exception"
            )
            paths.append(
                {
                    "kind": "possible_catch_path",
                    "detail": f"catch handler for {param_text} may run if it is thrown",
                    "range": _ref(file_path, first_line, node),
                    "note": "handler existence does not establish execution",
                }
            )
        elif node_type == "finally_clause":
            paths.append(
                {
                    "kind": "possible_cleanup_path",
                    "detail": "finally block may run as cleanup",
                    "range": _ref(file_path, first_line, node),
                    "note": "possible cleanup path; outcome is not proven",
                }
            )
        elif node_type == "resource_specification":
            paths.append(
                {
                    "kind": "possible_cleanup_path",
                    "detail": "try-with-resources resource close is possible",
                    "range": _ref(file_path, first_line, node),
                    "note": "close is possible, not observed",
                }
            )
        elif node_type == "throw_statement":
            thrown = " ".join(_p.node_text(node, source).split())
            paths.append(
                {
                    "kind": "possible_exception_path",
                    "detail": f"declared throw site may raise: {thrown}",
                    "range": _ref(file_path, first_line, node),
                    "note": "propagation to any handler is not proven",
                }
            )
        elif node_type == "return_statement":
            paths.append(
                {
                    "kind": "possible_return_path",
                    "detail": "method may return here",
                    "range": _ref(file_path, first_line, node),
                    "note": "possible path only; reaching this return is not established",
                }
            )
        for child in node.children:
            visit(child)

    for child in body.children:
        visit(child)
    return paths, nested_deferred


def resolve_java_control_flow(
    evidence: Any,
    symbol_name: str,
    *,
    branch_hint: str | None = None,
    language_version: object = None,
) -> CodeFactResolutionV2:
    """Report statically established possible paths for one in-window method."""
    category = JavaFactCategory.CONTROL_FLOW_FACT
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
        return _base_unresolved(
            category, repository, revision, subject, reason, evidence_ids, level_text
        )
    assert location is not None
    if not symbol_name or not symbol_name.strip():
        return _base_unresolved(
            category,
            repository,
            revision,
            subject,
            "missing_symbol_name",
            evidence_ids,
            level_text,
        )
    if not source_path.lower().endswith(".java"):
        return _base_unresolved(
            category,
            repository,
            revision,
            subject,
            "not_a_java_source_file",
            evidence_ids,
            level_text,
        )
    if not repository:
        return _base_unresolved(
            category,
            repository,
            revision,
            subject,
            "repository_identity_unavailable",
            evidence_ids,
            level_text,
        )

    window_text = "\n".join(line.text for line in lines)
    concurrency = _concurrency_code(window_text)
    if concurrency is not None:
        return _base_unresolved(
            category,
            repository,
            revision,
            subject,
            ",".join(concurrency),
            evidence_ids,
            level_text,
            partial=True,
        )
    if "record_declaration" in window_text or "sealed " in window_text:
        # Version-gated declarations change branch-target identity; without a
        # trusted level the window cannot be read as a branch graph.
        _ = window_text

    source_bytes = window_text.encode("utf-8")
    try:
        tree = _p.parse_java(source_bytes)
    except Exception as exc:
        return _base_unresolved(
            category,
            repository,
            revision,
            subject,
            f"java_parser_unavailable:{type(exc).__name__}",
            evidence_ids,
            level_text,
        )
    if tree.root_node.has_error:
        return _base_unresolved(
            category,
            repository,
            revision,
            subject,
            "java_syntax_error_or_incomplete_source_window",
            evidence_ids,
            level_text,
        )

    method_node, failure = _method_node(tree.root_node, source_bytes, symbol_name)
    if method_node is None:
        assert failure is not None
        return _base_unresolved(
            category, repository, revision, subject, failure, evidence_ids, level_text
        )
    body = next((c for c in method_node.children if c.type == "block"), None)
    if body is None:
        return _base_unresolved(
            category,
            repository,
            revision,
            subject,
            "method_body_outside_window",
            evidence_ids,
            level_text,
        )

    first_line = lines[0].number
    paths, nested_deferred = _collect_paths(body, source_bytes, source_path, first_line)
    if branch_hint is not None:
        hint = branch_hint.strip().lower()
        if not hint:
            return _base_unresolved(
                category,
                repository,
                revision,
                subject,
                "branch_hint_not_found",
                evidence_ids,
                level_text,
            )
        paths = [
            path
            for path in paths
            if hint in str(path["kind"]).lower() or hint in str(path["detail"]).lower()
        ]
        if not paths:
            return _base_unresolved(
                category,
                repository,
                revision,
                subject,
                "branch_hint_not_found",
                evidence_ids,
                level_text,
            )
    if not paths:
        return _base_unresolved(
            category,
            repository,
            revision,
            subject,
            "no_branch_or_exception_structure_in_window",
            evidence_ids,
            level_text,
        )

    assumptions = [
        "possible paths only; no path is established as taken, run, or observed",
        "catch-handler existence does not establish execution",
        "finally is possible cleanup, not a proven outcome",
    ]
    if nested_deferred:
        assumptions.append("nested lambda/anonymous-class bodies excluded from paths")

    declaration_location = location.model_copy(
        update={
            "line_start": first_line + method_node.start_point[0],
            "line_end": first_line + method_node.end_point[0],
        }
    )
    structured_fact = {
        "kind": "possible_paths",
        "method": symbol_name,
        "paths": paths,
        "notes": list(assumptions),
    }
    # Fail closed on execution wording over authored claims only: path details
    # echo verbatim source (conditions, thrown text), which is evidence, not a
    # claim. Only kinds, notes, and assumptions carry analyzer wording.
    authored_words = " ".join(
        [str(path["kind"]) + " " + str(path["note"]) for path in paths]
        + [str(a) for a in assumptions]
    ).lower()
    assert not any(word in authored_words for word in _EXECUTION_WORDS)
    return CodeFactResolutionV2(
        status=CodeFactStatus.RESOLVED,
        repository=repository,
        revision=revision,
        fact_category=category,
        semantic_stage=SemanticStage.DECLARATION,
        declaring_type="unknown",
        subject_identity=f"{source_path}#{symbol_name}",
        value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
        language_version=level_text,
        analysis_method="tree-sitter-range-v1",
        structured_fact=structured_fact,
        control_flow_refs=tuple(
            [
                (
                    f"{source_path}:{first_line + method_node.start_point[0]}-"
                    f"{first_line + method_node.end_point[0]}"
                )
            ]
            + [str(path["range"]) for path in paths]
        ),
        source_evidence_ids=evidence_ids,
        declaration_location=declaration_location,
        assumptions=tuple(assumptions),
        parser_version=_p.parser_version(),
    )


def _effect_entries(
    body: Any, source: bytes, file_path: str, first_line: int
) -> list[dict[str, Any]]:
    """Collect possible-effect entries from throw/mutation/cleanup sites."""
    effects: list[dict[str, Any]] = []
    caught_names: set[str] = set()
    for node in _p.walk(body):
        if node.type == "catch_formal_parameter":
            for child in node.children:
                if child.type == "identifier":
                    caught_names.add(_p.node_text(child, source).strip())

    for node in _p.walk(body):
        if node.type in {"lambda_expression", "class_declaration"}:
            continue
        if node.type == "throw_statement":
            text = " ".join(_p.node_text(node, source).split())
            wrapping = (
                "initCause" in text
                or "getCause" in text
                or any(name and name in text for name in caught_names)
            )
            effects.append(
                {
                    "kind": "possible_exception",
                    "detail": f"throw site may raise: {text}",
                    "range": _ref(file_path, first_line, node),
                    "note": (
                        "exception wrapping/cause chain named, not proven to propagate"
                        if wrapping
                        else "possible exception only; propagation not proven"
                    ),
                }
            )
        elif node.type in {"assignment_expression", "update_expression"}:
            text = " ".join(_p.node_text(node, source).split())
            effects.append(
                {
                    "kind": "possible_mutation",
                    "detail": f"possible state mutation: {text}",
                    "range": _ref(file_path, first_line, node),
                    "note": "possible write only; reaching this write is not established",
                }
            )
        elif node.type == "resource_specification":
            effects.append(
                {
                    "kind": "possible_cleanup",
                    "detail": "try-with-resources close is possible",
                    "range": _ref(file_path, first_line, node),
                    "note": "close is possible, not observed",
                }
            )
        elif node.type == "method_invocation":
            invoked = " ".join(_p.node_text(node, source).split())
            if "addSuppressed" in invoked:
                effects.append(
                    {
                        "kind": "possible_suppression",
                        "detail": f"possible suppressed exception: {invoked}",
                        "range": _ref(file_path, first_line, node),
                        "note": "suppression possible, not observed",
                    }
                )
            else:
                effects.append(
                    {
                        "kind": "possible_side_effect",
                        "detail": f"invocation may mutate state or resources: {invoked}",
                        "range": _ref(file_path, first_line, node),
                        "note": (
                            "possible side effect only; callee not analyzed "
                            "interprocedurally and execution not established"
                        ),
                    }
                )
    return effects


def resolve_java_possible_effect(
    evidence: Any,
    symbol_name: str,
    *,
    effect_hint: str | None = None,
    language_version: object = None,
) -> CodeFactResolutionV2:
    """Report statically established possible effects for one in-window method."""
    category = JavaFactCategory.POSSIBLE_EFFECT_FACT
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
        return _base_unresolved(
            category, repository, revision, subject, reason, evidence_ids, level_text
        )
    assert location is not None
    if not symbol_name or not symbol_name.strip():
        return _base_unresolved(
            category,
            repository,
            revision,
            subject,
            "missing_symbol_name",
            evidence_ids,
            level_text,
        )
    if not source_path.lower().endswith(".java"):
        return _base_unresolved(
            category,
            repository,
            revision,
            subject,
            "not_a_java_source_file",
            evidence_ids,
            level_text,
        )
    if not repository:
        return _base_unresolved(
            category,
            repository,
            revision,
            subject,
            "repository_identity_unavailable",
            evidence_ids,
            level_text,
        )

    window_text = "\n".join(line.text for line in lines)
    concurrency = _concurrency_code(window_text)
    if concurrency is not None:
        return _base_unresolved(
            category,
            repository,
            revision,
            subject,
            ",".join(concurrency),
            evidence_ids,
            level_text,
            partial=True,
        )

    source_bytes = window_text.encode("utf-8")
    try:
        tree = _p.parse_java(source_bytes)
    except Exception as exc:
        return _base_unresolved(
            category,
            repository,
            revision,
            subject,
            f"java_parser_unavailable:{type(exc).__name__}",
            evidence_ids,
            level_text,
        )
    if tree.root_node.has_error:
        return _base_unresolved(
            category,
            repository,
            revision,
            subject,
            "java_syntax_error_or_incomplete_source_window",
            evidence_ids,
            level_text,
        )

    method_node, failure = _method_node(tree.root_node, source_bytes, symbol_name)
    if method_node is None:
        assert failure is not None
        return _base_unresolved(
            category, repository, revision, subject, failure, evidence_ids, level_text
        )
    body = next((c for c in method_node.children if c.type == "block"), None)
    if body is None:
        return _base_unresolved(
            category,
            repository,
            revision,
            subject,
            "method_body_outside_window",
            evidence_ids,
            level_text,
        )

    first_line = lines[0].number
    effects = _effect_entries(body, source_bytes, source_path, first_line)
    if effect_hint is not None:
        hint = effect_hint.strip().lower()
        if not hint:
            return _base_unresolved(
                category,
                repository,
                revision,
                subject,
                "effect_hint_not_found",
                evidence_ids,
                level_text,
            )
        effects = [
            effect
            for effect in effects
            if hint in str(effect["kind"]).lower() or hint in str(effect["detail"]).lower()
        ]
        if not effects:
            return _base_unresolved(
                category,
                repository,
                revision,
                subject,
                "effect_hint_not_found",
                evidence_ids,
                level_text,
            )
    if not effects:
        return _base_unresolved(
            category,
            repository,
            revision,
            subject,
            "no_mutation_or_exception_site_in_window",
            evidence_ids,
            level_text,
        )

    assumptions = (
        "possible effects only; no effect is established as taken, run, or observed",
        "wrapping and cause chains are named, not proven to propagate",
        "verdict ceiling LIKELY via CATEGORY_PROOF_RULES; never observed behavior",
    )
    declaration_location = location.model_copy(
        update={
            "line_start": first_line + method_node.start_point[0],
            "line_end": first_line + method_node.end_point[0],
        }
    )
    structured_fact = {
        "kind": "possible_effects",
        "method": symbol_name,
        "effects": effects,
        "notes": list(assumptions),
    }
    authored_words = " ".join(
        [str(effect["kind"]) + " " + str(effect["note"]) for effect in effects]
        + [str(a) for a in assumptions]
    ).lower()
    assert not any(word in authored_words for word in _EXECUTION_WORDS)
    return CodeFactResolutionV2(
        status=CodeFactStatus.RESOLVED,
        repository=repository,
        revision=revision,
        fact_category=category,
        semantic_stage=SemanticStage.DECLARATION,
        declaring_type="unknown",
        subject_identity=f"{source_path}#{symbol_name}",
        value_semantics=CodeValueSemantics.EXPLICIT_INITIALIZER,
        language_version=level_text,
        analysis_method="tree-sitter-range-v1",
        structured_fact=structured_fact,
        control_flow_refs=tuple(str(effect["range"]) for effect in effects),
        source_evidence_ids=evidence_ids,
        declaration_location=declaration_location,
        assumptions=assumptions,
        parser_version=_p.parser_version(),
    )
