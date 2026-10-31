# src/investigation_agent_platform/application/analysis/java/init_facts.py
"""Deterministic Java class-initialization and construction analysis (Phase 3, D4).

Class initialization (static field initializers and ``static`` blocks, in
textual order when the class is initialized) and per-instance construction
(instance field initializers, instance blocks, then the constructor body on
the non-delegating path) are modeled as SEPARATE traces with distinct
``SemanticStage`` values. A ``this(...)`` delegation runs first; the
delegating body continues after the delegated constructor returns.

Source order alone never proves execution: ``POST_CONSTRUCTION`` is reported
only for a fully bounded in-window constructor path. Anything else is
``PARTIAL`` with ``LATER_MUTABLE`` semantics and a named limitation.
Cross-file superclass chains, DI construction, and generated constructors
stay partial; enums, records, and compact constructors stay partial until
dedicated rules land. Nothing in this phase is ever ``RUNTIME_EFFECTIVE``.
"""

from __future__ import annotations

import re
from typing import Any

from investigation_agent_platform.application.analysis.failure import failure_code
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

_STATIC_RE = re.compile(r"\bstatic\b")

_TYPE_DECLARATIONS = frozenset(
    {
        "class_declaration",
        "enum_declaration",
        "record_declaration",
        "interface_declaration",
        "annotation_type_declaration",
    }
)

# Statements/expressions that make a bounded in-window trace claim impossible:
# calls, allocation, branching, loops, exception paths, synchronization, and
# deferred execution all require call/data-flow scope beyond Phase 3.
_OPAQUE_TYPES = frozenset(
    {
        "method_invocation",
        "object_creation_expression",
        "array_creation_expression",
        "lambda_expression",
        "method_reference",
        "switch_expression",
        "switch_statement",
        "if_statement",
        "for_statement",
        "enhanced_for_statement",
        "while_statement",
        "do_statement",
        "try_statement",
        "try_with_resources_statement",
        "synchronized_statement",
        "throw_statement",
        "assert_statement",
    }
)

# Container-managed construction: wiring lives outside any source window.
_DI_ANNOTATIONS = frozenset(
    {
        "Inject",
        "Autowired",
        "Resource",
        "Singleton",
        "Component",
        "Service",
        "Repository",
        "Controller",
        "RestController",
        "Configuration",
        "Bean",
        "Scope",
        "Named",
        "Provides",
        "ApplicationScoped",
        "RequestScoped",
        "SessionScoped",
        "ManagedBean",
    }
)

# Compile-time code generation: constructors may not exist in source.
_GENERATED_ANNOTATIONS = frozenset(
    {
        "Generated",
        "AllArgsConstructor",
        "NoArgsConstructor",
        "RequiredArgsConstructor",
        "Builder",
        "SuperBuilder",
        "Value",
        "Data",
    }
)

_ORDER_ASSUMPTION = "source_order_does_not_prove_execution"
_IMPLICIT_SUPER_ASSUMPTION = "implicit_super_object_assumed_bounded"


def constructor_chain_targets(constructor_node: Any, source: bytes) -> list[str]:
    """Return explicit this(...)/super(...) targets in textual order.

    Note: the D4 sketch iterates direct children, but the grammar nests the
    invocation under ``constructor_body``; this walks the subtree so the
    helper observes the actual node placement.
    """
    targets: list[str] = []
    for child in _p.walk(constructor_node):
        if child.type == "explicit_constructor_invocation":
            text = source[child.start_byte : child.end_byte].decode("utf-8", errors="replace")
            targets.append(" ".join(text.split()).removesuffix(";"))
    return targets


def initialization_order_kind(declaration_node: Any) -> str:
    """Classify by initialization domain; never mix class and object traces."""
    node_type = declaration_node.type
    if node_type == "static_initializer":
        return "class_initializer"
    if node_type == "block":
        return "instance_initializer"
    if node_type == "constructor_declaration":
        return "constructor_body"
    if node_type == "field_declaration":
        return "field_initializer"  # caller inspects modifiers for static/instance
    return "unsupported_initialization_node"


def _field_order_kind(declaration_node: Any, source: bytes) -> str:
    modifiers = next(
        (child for child in declaration_node.children if child.type == "modifiers"),
        None,
    )
    modifier_text = _p.node_text(modifiers, source) if modifiers is not None else ""
    if _STATIC_RE.search(modifier_text):
        return "static_field_initializer"
    return "instance_field_initializer"


def _annotation_names(modifiers_node: Any, source: bytes) -> list[str]:
    names: list[str] = []
    for child in modifiers_node.children:
        if child.type not in {"marker_annotation", "annotation"}:
            continue
        for grandchild in child.children:
            if grandchild.type in {"identifier", "scoped_identifier", "type_identifier"}:
                text = _p.node_text(grandchild, source).strip()
                names.append(text.split(".")[-1])
    return names


def _modifiers_of(node: Any, source: bytes) -> tuple[str, list[str]]:
    modifiers = next((child for child in node.children if child.type == "modifiers"), None)
    if modifiers is None:
        return ("", [])
    return (_p.node_text(modifiers, source), _annotation_names(modifiers, source))


def _is_opaque(node: Any) -> bool:
    return any(descendant.type in _OPAQUE_TYPES for descendant in _p.walk(node))


def _argument_arity(argument_list: Any) -> int:
    return sum(1 for child in argument_list.children if child.type not in {"(", ")", ","})


def _parameter_arity(formal_parameters: Any) -> int:
    return sum(
        1
        for child in formal_parameters.children
        if child.type in {"formal_parameter", "spread_parameter"}
    )


def _constructor_arity(constructor_node: Any) -> int | None:
    params = next(
        (child for child in constructor_node.children if child.type == "formal_parameters"),
        None,
    )
    if params is None:
        return None
    return _parameter_arity(params)


def _later_this_mutations(
    body_children: list[Any], instance_field_names: set[str], source_bytes: bytes
) -> list[str]:
    """Fields assigned via ``this.x`` in method bodies (post-construction writes).

    Only ``this``-qualified assignments count: bare names may be locals, and
    setter calls require call-graph scope beyond Phase 3.
    """
    mutated: list[str] = []
    for child in body_children:
        if child.type != "method_declaration":
            continue
        for node in _p.walk(child):
            if node.type != "assignment_expression":
                continue
            left = node.child_by_field_name("left")
            if left is None or left.type != "field_access":
                continue
            obj = left.child_by_field_name("object")
            field = left.child_by_field_name("field")
            if obj is None or field is None:
                continue
            if _p.node_text(obj, source_bytes) != "this":
                continue
            name = _p.node_text(field, source_bytes)
            if name in instance_field_names and name not in mutated:
                mutated.append(name)
    return mutated


def _constructor_label(constructor_node: Any, source: bytes) -> str:
    name = constructor_node.child_by_field_name("name")
    label = _p.node_text(name, source).strip() if name is not None else "unknown"
    params = next(
        (child for child in constructor_node.children if child.type == "formal_parameters"),
        None,
    )
    params_text = _p.node_text(params, source).strip() if params is not None else "()"
    return f"{label}{params_text}"


def _unresolved(
    repository: str,
    revision: str | None,
    subject: str,
    code: str,
    evidence_ids: tuple[Any, ...],
    language_version: str | None,
    exc: BaseException | None = None,
) -> CodeFactResolutionV2:
    from investigation_agent_platform.domain.provenance.models import SourceLocation

    return CodeFactResolutionV2(
        status=CodeFactStatus.UNRESOLVED,
        repository=repository or "unknown",
        revision=revision or None,
        fact_category=JavaFactCategory.LANGUAGE_SEMANTIC_FACT,
        semantic_stage=SemanticStage.DECLARATION,
        declaring_type="unknown",
        subject_identity=subject or "unknown",
        value_semantics=CodeValueSemantics.LATER_MUTABLE,
        language_version=language_version,
        analysis_method="tree-sitter-range-v1",
        source_evidence_ids=tuple(e for e in evidence_ids if e is not None),
        declaration_location=SourceLocation(system="Git", identifier="unknown"),
        limitation_codes=(code,),
        assumptions=(_ORDER_ASSUMPTION,)
        if exc is None
        else (_ORDER_ASSUMPTION, f"exception_type={type(exc).__name__}"),
        parser_version=_p.parser_version(),
    )


def _partial(
    *,
    repository: str,
    revision: str,
    qualified: str,
    code: str,
    stage: SemanticStage,
    evidence_ids: tuple[Any, ...],
    declaration_location: Any,
    language_version: str | None,
    structured_fact: dict[str, Any] | None,
    extra_codes: tuple[str, ...] = (),
) -> CodeFactResolutionV2:
    return CodeFactResolutionV2(
        status=CodeFactStatus.PARTIAL,
        repository=repository,
        revision=revision,
        fact_category=JavaFactCategory.LANGUAGE_SEMANTIC_FACT,
        semantic_stage=stage,
        declaring_type=qualified,
        subject_identity=qualified,
        value_semantics=CodeValueSemantics.LATER_MUTABLE,
        language_version=language_version,
        analysis_method="tree-sitter-range-v1",
        structured_fact=structured_fact,
        source_evidence_ids=evidence_ids,
        declaration_location=declaration_location,
        limitation_codes=(code, *extra_codes),
        assumptions=(_ORDER_ASSUMPTION,),
        parser_version=_p.parser_version(),
    )


def resolve_java_init_fact(
    evidence: Any,
    symbol_name: str,
    *,
    declaring_type: str | None = None,
    language_version: object = None,
) -> CodeFactResolutionV2:
    """Resolve class-initialization and construction traces for a named type.

    Reads no files and follows no references; the only source of facts is the
    supplied revision-pinned range-read evidence. ``language_version`` is a
    caller-attested trusted source level; ``None`` means unknown.
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
            failure_code(exc),
            evidence_ids,
            level_text,
            exc=exc,
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
    matches: list[tuple[Any, str, str]] = []
    for node in _p.walk(tree.root_node):
        if node.type not in _TYPE_DECLARATIONS:
            continue
        name_node = node.child_by_field_name("name")
        if name_node is None or _p.node_text(name_node, source_bytes) != symbol_name:
            continue
        if node.type == "class_declaration":
            qualified = _p.qualified_class_name(node, source_bytes, package)
        else:
            qualified = f"{package}.{symbol_name}" if package else symbol_name
        matches.append((node, node.type, qualified))
    if declaring_type:
        matches = [item for item in matches if _p.matches_declaring_type(item[2], declaring_type)]
    if len(matches) != 1:
        return _unresolved(
            repository,
            revision,
            subject,
            "type_not_found" if not matches else "ambiguous_type_declaration",
            evidence_ids,
            level_text,
        )

    type_node, kind, qualified = matches[0]
    first_line = lines[0].number
    declaration_location = location.model_copy(
        update={
            "line_start": first_line + type_node.start_point[0],
            "line_end": first_line + type_node.end_point[0],
        }
    )

    def partial(
        code: str,
        stage: SemanticStage,
        structured_fact: dict[str, Any] | None = None,
        extra_codes: tuple[str, ...] = (),
    ) -> CodeFactResolutionV2:
        return _partial(
            repository=repository,
            revision=revision,
            qualified=qualified,
            code=code,
            stage=stage,
            evidence_ids=evidence_ids,
            declaration_location=declaration_location,
            language_version=level_text,
            structured_fact=structured_fact,
            extra_codes=extra_codes,
        )

    if kind == "enum_declaration":
        return partial(
            "enum_constants_require_dedicated_rule",
            SemanticStage.CLASS_INITIALIZATION,
            {"subject": qualified, "type_kind": kind},
        )
    if kind == "record_declaration":
        codes: tuple[str, ...] = ()
        for descendant in _p.walk(type_node):
            if descendant.type == "compact_constructor_declaration":
                codes = ("compact_constructor_requires_dedicated_rule",)
                break
        return partial(
            "record_components_require_dedicated_rule",
            SemanticStage.CONSTRUCTOR_BODY,
            {"subject": qualified, "type_kind": kind},
            extra_codes=codes,
        )
    if kind != "class_declaration":
        return partial(
            "unsupported_type_kind_for_initialization",
            SemanticStage.DECLARATION,
            {"subject": qualified, "type_kind": kind},
        )

    _, class_annotations = _modifiers_of(type_node, source_bytes)
    superclass = next((child for child in type_node.children if child.type == "superclass"), None)
    if superclass is not None:
        return partial(
            "requires_cross_file_scope",
            SemanticStage.INSTANCE_INITIALIZATION,
            {
                "subject": qualified,
                "superclass": _p.node_text(superclass, source_bytes).strip(),
            },
        )
    if any(name in _DI_ANNOTATIONS for name in class_annotations):
        return partial(
            "requires_cross_file_scope",
            SemanticStage.INSTANCE_INITIALIZATION,
            {"subject": qualified, "reason": "container_managed_construction"},
        )
    if any(name in _GENERATED_ANNOTATIONS for name in class_annotations):
        return partial(
            "requires_generated_source",
            SemanticStage.CONSTRUCTOR_BODY,
            {"subject": qualified, "reason": "generated_constructor"},
        )

    class_body = next((child for child in type_node.children if child.type == "class_body"), None)
    body_children = list(class_body.children) if class_body is not None else []
    static_steps: list[dict[str, Any]] = []
    instance_steps: list[dict[str, Any]] = []
    constructors: list[Any] = []
    step_order = 0

    def line_of(node: Any) -> int:
        return first_line + node.start_point[0]

    for child in body_children:
        child_kind = initialization_order_kind(child)
        if child_kind == "class_initializer":
            if _is_opaque(child):
                return partial(
                    "static_initializer_not_bounded_in_window",
                    SemanticStage.CLASS_INITIALIZATION,
                    {"subject": qualified},
                )
            static_steps.append(
                {
                    "kind": "class_initializer",
                    "order": step_order,
                    "line": line_of(child),
                    "text": _p.node_text(child, source_bytes).strip()[:500],
                }
            )
            step_order += 1
        elif child_kind == "instance_initializer":
            if _is_opaque(child):
                return partial(
                    "instance_initializer_not_bounded_in_window",
                    SemanticStage.INSTANCE_INITIALIZATION,
                    {"subject": qualified},
                )
            instance_steps.append(
                {
                    "kind": "instance_initializer",
                    "order": step_order,
                    "line": line_of(child),
                    "text": _p.node_text(child, source_bytes).strip()[:500],
                }
            )
            step_order += 1
        elif child_kind == "field_initializer":
            value_node = next(
                (
                    grandchild
                    for grandchild in child.children
                    if grandchild.type == "variable_declarator"
                ),
                None,
            )
            value_child = (
                value_node.child_by_field_name("value") if value_node is not None else None
            )
            if value_child is not None and _is_opaque(value_child):
                unbounded_code = (
                    "static_initializer_not_bounded_in_window"
                    if _field_order_kind(child, source_bytes) == "static_field_initializer"
                    else "instance_initializer_not_bounded_in_window"
                )
                unbounded_stage = (
                    SemanticStage.CLASS_INITIALIZATION
                    if unbounded_code.startswith("static_")
                    else SemanticStage.INSTANCE_INITIALIZATION
                )
                return partial(unbounded_code, unbounded_stage, {"subject": qualified})
            names = [
                _p.node_text(declarator.child_by_field_name("name"), source_bytes)
                for declarator in child.children
                if declarator.type == "variable_declarator"
                and declarator.child_by_field_name("name") is not None
            ]
            step = {
                "kind": _field_order_kind(child, source_bytes),
                "order": step_order,
                "line": line_of(child),
                "names": names,
                "text": _p.node_text(child, source_bytes).strip()[:500],
            }
            if step["kind"] == "static_field_initializer":
                static_steps.append(step)
            else:
                instance_steps.append(step)
            step_order += 1
        elif child_kind == "constructor_body":
            constructors.append(child)

    for constructor in constructors:
        _, ctor_annotations = _modifiers_of(constructor, source_bytes)
        if any(name in _DI_ANNOTATIONS for name in ctor_annotations):
            return partial(
                "requires_cross_file_scope",
                SemanticStage.INSTANCE_INITIALIZATION,
                {"subject": qualified, "reason": "container_managed_construction"},
            )
        if any(name in _GENERATED_ANNOTATIONS for name in ctor_annotations):
            return partial(
                "requires_generated_source",
                SemanticStage.CONSTRUCTOR_BODY,
                {"subject": qualified, "reason": "generated_constructor"},
            )

    arities = [_constructor_arity(constructor) for constructor in constructors]
    chain_targets: list[str] = []
    delegations: dict[int, int | None] = {}
    constructor_infos: list[dict[str, Any]] = []
    for index, constructor in enumerate(constructors):
        targets = constructor_chain_targets(constructor, source_bytes)
        chain_targets.extend(targets)
        explicit = [
            descendant
            for descendant in _p.walk(constructor)
            if descendant.type == "explicit_constructor_invocation"
        ]
        delegate_to: int | None = None
        if explicit:
            invocation = explicit[0]
            keyword = invocation.children[0].type if invocation.children else ""
            argument_list = next(
                (child for child in invocation.children if child.type == "argument_list"),
                None,
            )
            arity = _argument_arity(argument_list) if argument_list is not None else 0
            if keyword == "this":
                peers = [
                    peer
                    for peer, peer_arity in enumerate(arities)
                    if peer != index and peer_arity == arity
                ]
                if not peers:
                    return partial(
                        "constructor_delegation_target_outside_window",
                        SemanticStage.CONSTRUCTOR_BODY,
                        {"subject": qualified, "chain_targets": chain_targets},
                    )
                if len(peers) > 1:
                    return partial(
                        "ambiguous_constructor_delegation",
                        SemanticStage.CONSTRUCTOR_BODY,
                        {"subject": qualified, "chain_targets": chain_targets},
                    )
                delegate_to = peers[0]
            elif keyword == "super":
                if arity != 0:
                    return partial(
                        "requires_cross_file_scope",
                        SemanticStage.INSTANCE_INITIALIZATION,
                        {"subject": qualified, "chain_targets": chain_targets},
                    )
        delegations[index] = delegate_to
        constructor_infos.append(
            {
                "signature": _constructor_label(constructor, source_bytes),
                "semantic_stage": SemanticStage.CONSTRUCTOR_BODY.value,
                "chain_targets": targets,
                "line": line_of(constructor),
            }
        )

    visited_global: set[int] = set()
    for start in range(len(constructors)):
        seen: set[int] = set()
        current: int | None = start
        while current is not None:
            if current in seen:
                return partial(
                    "constructor_delegation_cycle",
                    SemanticStage.CONSTRUCTOR_BODY,
                    {"subject": qualified, "chain_targets": chain_targets},
                )
            seen.add(current)
            visited_global.add(current)
            current = delegations.get(current)

    for constructor in constructors:
        if _is_opaque(constructor):
            return partial(
                "constructor_body_not_bounded_in_window",
                SemanticStage.CONSTRUCTOR_BODY,
                {"subject": qualified, "chain_targets": chain_targets},
            )

    _ = visited_global
    structured_fact: dict[str, Any] = {
        "subject": qualified,
        "class_initialization_trace": {
            "semantic_stage": SemanticStage.CLASS_INITIALIZATION.value,
            "steps": static_steps,
        },
        "instance_construction_trace": {
            "semantic_stage": SemanticStage.INSTANCE_INITIALIZATION.value,
            "instance_steps": instance_steps,
            "constructors": constructor_infos,
            "constructor_chain_targets": chain_targets,
        },
        "execution_note": _ORDER_ASSUMPTION,
    }

    # Multiple constructors mean the entry point is call-site dependent:
    # textual order cannot select which path executes. The traces stay
    # observable, but no POST_CONSTRUCTION value is claimed.
    if len(constructors) > 1:
        return partial(
            "constructor_entry_requires_call_site_scope",
            SemanticStage.CONSTRUCTOR_BODY,
            structured_fact,
        )

    instance_field_names = {name for step in instance_steps for name in step.get("names", [])}
    later_mutated = _later_this_mutations(body_children, instance_field_names, source_bytes)
    if later_mutated:
        return partial(
            "later_field_mutation_in_window",
            SemanticStage.LATER_MUTATION,
            {**structured_fact, "later_mutated_fields": later_mutated},
        )

    assumptions: tuple[str, ...] = (_ORDER_ASSUMPTION,)
    if superclass is None:
        assumptions = (_ORDER_ASSUMPTION, _IMPLICIT_SUPER_ASSUMPTION)
    return CodeFactResolutionV2(
        status=CodeFactStatus.RESOLVED,
        repository=repository,
        revision=revision,
        fact_category=JavaFactCategory.LANGUAGE_SEMANTIC_FACT,
        semantic_stage=SemanticStage.POST_CONSTRUCTION,
        declaring_type=qualified,
        subject_identity=qualified,
        value_semantics=CodeValueSemantics.POST_CONSTRUCTION,
        language_version=level_text,
        analysis_method="tree-sitter-range-v1",
        structured_fact=structured_fact,
        source_evidence_ids=evidence_ids,
        declaration_location=declaration_location,
        assumptions=assumptions,
        parser_version=_p.parser_version(),
    )
