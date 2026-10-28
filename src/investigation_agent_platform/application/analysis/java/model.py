"""Java window code model (Q0 query layer).

Builds frozen, typed records from one parsed tree-sitter Java window. The
model is the shared "database" that query predicates (`query.py`) and future
resolver migrations consume. It never executes code and never leaves the
window: every record carries file path, line range, and revision.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.application.analysis.java import _parse as _p
from investigation_agent_platform.application.analysis.java import expressions as _expr
from investigation_agent_platform.application.analysis.java import literals as _lit
from investigation_agent_platform.application.analysis.java.method_facts import overload_key

SCHEMA_VERSION = "iap/java-model/v1"

_LITERAL_NODE_TYPES = frozenset(
    {
        "decimal_integer_literal",
        "hex_integer_literal",
        "octal_integer_literal",
        "binary_integer_literal",
        "decimal_floating_point_literal",
        "hex_floating_point_literal",
        "character_literal",
        "string_literal",
        "true",
        "false",
        "null_literal",
    }
)

_EVALUATED_STATIC_TYPE = {
    "int": "int",
    "long": "long",
    "float": "float",
    "double": "double",
    "char": "char",
    "string": "String",
    "boolean": "boolean",
    "null": "null",
}


class Span(BaseModel):
    """File-scoped location for a model record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file_path: str = Field(min_length=1, max_length=1024)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)


class LiteralValue(BaseModel):
    """A decoded literal with its evaluated kind."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1, max_length=64)
    value: str = Field(max_length=4096)
    span: Span


class ArgSketch(BaseModel):
    """One call argument: literal, bare name, or complex expression."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0)
    kind: str = Field(min_length=1, max_length=64)
    shape: str = Field(min_length=1, max_length=128)
    operator: str | None = Field(default=None, max_length=32)
    static_type: str = Field(min_length=1, max_length=512)
    text: str = Field(max_length=1024)
    literal: LiteralValue | None = None
    value_role: Literal["lvalue", "rvalue", "read_modify_write", "unknown"] = "rvalue"


class CallSite(BaseModel):
    """A method invocation or object-creation site."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_name: str = Field(min_length=1, max_length=512)
    arity: int = Field(ge=0)
    args: list[ArgSketch] = Field(default_factory=list)
    is_constructor: bool = False
    receiver: str = Field(default="", max_length=1024)
    # Scoped to the invocation's own target/receiver text only. A method
    # reference passed as an argument is tracked on the argument's shape, not
    # here; it must not mark the enclosing call as dynamically dispatched.
    has_method_reference: bool = False
    in_lambda: bool = False
    enclosing_method: str | None = Field(default=None, max_length=1024)
    enclosing_class: str | None = Field(default=None, max_length=1024)
    span: Span


class MethodDef(BaseModel):
    """A method or constructor declaration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=512)
    qualified_name: str = Field(min_length=1, max_length=1024)
    arity: int = Field(ge=0)
    param_types: list[str] = Field(default_factory=list)
    return_type: str | None = Field(default=None, max_length=512)
    enclosing_class: str | None = Field(default=None, max_length=1024)
    key: str = Field(min_length=1, max_length=1024)
    is_constructor: bool = False
    is_varargs: bool = False
    is_override: bool = False
    is_default: bool = False
    nearest_class: str | None = Field(default=None, max_length=1024)
    declared_in: str = Field(default="class_declaration", max_length=64)
    span: Span


class FieldDef(BaseModel):
    """A field declaration with evaluated initializer detail."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=512)
    field_type: str = Field(min_length=1, max_length=512)
    enclosing_class: str | None = Field(default=None, max_length=1024)
    declared_in: str = Field(default="class_declaration", max_length=64)
    is_static: bool = False
    has_declared_type: bool = True
    initializer_kind: str | None = Field(default=None, max_length=64)
    raw_initializer: str | None = Field(default=None, max_length=1024)
    evaluated_kind: str | None = Field(default=None, max_length=64)
    evaluated_value: str | float | bool | None = None
    evaluation_limitation: str | None = Field(default=None, max_length=128)
    span: Span


class LocalDef(BaseModel):
    """A local variable declaration with evaluated initializer detail."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=512)
    var_type: str = Field(min_length=1, max_length=512)
    enclosing_method: str | None = Field(default=None, max_length=1024)
    method_name: str = Field(default="", max_length=512)
    initializer_kind: str | None = Field(default=None, max_length=64)
    raw_initializer: str | None = Field(default=None, max_length=1024)
    evaluated_kind: str | None = Field(default=None, max_length=64)
    evaluated_value: str | float | bool | None = None
    evaluation_limitation: str | None = Field(default=None, max_length=128)
    span: Span


class ParamDef(BaseModel):
    """A formal or spread parameter declaration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=512)
    var_type: str = Field(min_length=1, max_length=512)
    enclosing_method: str | None = Field(default=None, max_length=1024)
    method_name: str = Field(default="", max_length=512)
    is_varargs: bool = False
    span: Span


class DataflowBinding(BaseModel):
    """A local or parameter binding in one class-qualified method scope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    method_identity: str = Field(min_length=1, max_length=2048)
    method_name: str = Field(min_length=1, max_length=512)
    symbol: str = Field(min_length=1, max_length=512)
    kind: Literal["local", "parameter"]
    has_initializer: bool = False
    span: Span


class DataflowEvent(BaseModel):
    """One definition, use, return, or alias-risk event in a method."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    method_identity: str = Field(min_length=1, max_length=2048)
    symbol: str = Field(min_length=1, max_length=512)
    kind: Literal[
        "value_use",
        "return_use",
        "assignment",
        "compound_assignment",
        "update",
        "read_modify_write",
        "method_argument",
        "early_return",
    ]
    value_role: Literal["lvalue", "rvalue", "read_modify_write", "unknown"] = "unknown"
    span: Span


class ValueStateRecord(BaseModel):
    """Conservative effective value state for one bounded use or binding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    method_identity: str = Field(min_length=1, max_length=2048)
    binding: str = Field(min_length=1, max_length=512)
    state: Literal["CONSTANT", "UNKNOWN", "KILLED", "AMBIGUOUS", "ALIASED", "OUT_OF_WINDOW"]
    kind: str | None = Field(default=None, max_length=64)
    value: str | None = Field(default=None, max_length=4096)
    source: Literal["literal", "computed", "propagated", "reassignment", "boundary", "unknown"] = "unknown"
    span: Span


class BarrierFact(BaseModel):
    """A possible source-level flow barrier, never an execution claim."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    method_identity: str = Field(min_length=1, max_length=2048)
    kind: Literal["GUARD", "VALIDATION_CALL", "REASSIGNMENT_KILL", "ALIAS_ESCAPE", "UNKNOWN_BARRIER"]
    subject: str | None = Field(default=None, max_length=512)
    target_name: str | None = Field(default=None, max_length=512)
    limitation: str | None = Field(default=None, max_length=128)
    span: Span


class TypeArgument(BaseModel):
    """One source-level Java generic argument."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["explicit_type", "wildcard_unbounded", "wildcard_extends", "wildcard_super"]
    type: "TypeSyntax | None" = None
    bound_kind: Literal["extends", "super"] | None = None
    text: str = Field(max_length=1024)
    span: Span


class TypeSegment(BaseModel):
    """One qualified/member segment of a source type."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=512)
    arguments: list[TypeArgument] = Field(default_factory=list)
    text: str = Field(max_length=1024)
    span: Span


class TypeSyntax(BaseModel):
    """Recursive, syntax-preserving Java type representation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[
        "named",
        "parameterized",
        "wildcard",
        "array",
        "annotated",
        "primitive",
        "void",
        "inferred",
        "unknown",
    ]
    text: str = Field(max_length=4096)
    name: str | None = Field(default=None, max_length=1024)
    segments: list[TypeSegment] = Field(default_factory=list)
    arguments: list[TypeArgument] = Field(default_factory=list)
    element: "TypeSyntax | None" = None
    dimensions: int = Field(default=0, ge=0)
    annotations: list[str] = Field(default_factory=list)
    resolution: Literal["UNRESOLVED"] = "UNRESOLVED"
    span: Span


class TypeParameterDecl(BaseModel):
    """A formal class, interface, method, or constructor type parameter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: str = Field(min_length=1, max_length=2048)
    name: str = Field(min_length=1, max_length=512)
    owner_identity: str = Field(min_length=1, max_length=2048)
    owner_kind: Literal["class", "interface", "enum", "record", "method", "constructor"]
    bounds: list[TypeSyntax] = Field(default_factory=list)
    annotations: list[str] = Field(default_factory=list)
    span: Span


class GenericTypeUse(BaseModel):
    """One source-level parameterized type or diamond occurrence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: str = Field(min_length=1, max_length=2048)
    owner_identity: str = Field(min_length=1, max_length=2048)
    context: str = Field(min_length=1, max_length=64)
    syntax_kind: Literal["parameterized", "diamond", "array_parameterized"]
    syntax: TypeSyntax
    inferred_arguments: bool = False
    resolution: Literal["UNRESOLVED"] = "UNRESOLVED"
    span: Span


class ReturnSite(BaseModel):
    """One return statement inside a guard branch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value_shape: str = Field(default="", max_length=128)
    value_text: str = Field(default="", max_length=1024)
    raw_text: str = Field(min_length=1, max_length=4096)
    span: Span


class GuardBranch(BaseModel):
    """One if-statement with its direct-consequence literal returns.

    Mirrors the resolver's branch walk: descends into blocks but not into
    nested if subtrees; the else branch is never entered. Nested
    if-statements are recorded as their own guard branches.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enclosing_method: str | None = Field(default=None, max_length=1024)
    method_name: str = Field(default="", max_length=512)
    method_key: str = Field(default="", max_length=1024)
    method_identity: str = Field(default="", max_length=2048)
    guard_text: str = Field(default="", max_length=4096)
    returns: list[ReturnSite] = Field(default_factory=list)
    span: Span


class JavaFeature(BaseModel):
    """One declaration feature, with resolver-facing structured detail."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1, max_length=128)
    detail: str = Field(max_length=4096)
    note: str = Field(max_length=4096)
    span: Span
    arms: list[dict[str, str]] = Field(default_factory=list)


class TypeDeclaration(BaseModel):
    """A type declaration and the syntax facts needed by semantic queries."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=512)
    qualified_name: str = Field(min_length=1, max_length=1024)
    declaration_type: str = Field(min_length=1, max_length=128)
    gated_features: list[str] = Field(default_factory=list)
    permits: list[str] = Field(default_factory=list)
    generic_params: list[dict[str, str]] = Field(default_factory=list)
    record_components: list[dict[str, str]] = Field(default_factory=list)
    has_generics: bool = False
    type_variables: list[str] = Field(default_factory=list)
    span: Span


class JavaAnalysisSnapshot(BaseModel):
    """Complete typed analysis snapshot for one declaration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    declaration: TypeDeclaration
    member_names: list[str] = Field(default_factory=list)
    features: list[JavaFeature] = Field(default_factory=list)
    pattern_types: list[str] = Field(default_factory=list)
    missing_permits: list[str] = Field(default_factory=list)
    missing_pattern_targets: list[str] = Field(default_factory=list)
    partial_codes: list[str] = Field(default_factory=list)
    erasure_sensitive: bool = False


class JavaModel(BaseModel):
    """Frozen code model for one revision-pinned Java window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["iap/java-model/v1"] = SCHEMA_VERSION
    parser_version: str = Field(min_length=1, max_length=128)
    has_syntax_error: bool = False
    repository: str = Field(min_length=1, max_length=512)
    revision: str = Field(min_length=1, max_length=256)
    file_path: str = Field(min_length=1, max_length=1024)
    package: str = Field(default="", max_length=1024)
    imports: list[str] = Field(default_factory=list)
    classes: list[str] = Field(default_factory=list)
    methods: list[MethodDef] = Field(default_factory=list)
    calls: list[CallSite] = Field(default_factory=list)
    literals: list[LiteralValue] = Field(default_factory=list)
    fields: list[FieldDef] = Field(default_factory=list)
    locals: list[LocalDef] = Field(default_factory=list)
    params: list[ParamDef] = Field(default_factory=list)
    guard_returns: list[GuardBranch] = Field(default_factory=list)
    dataflow_bindings: list[DataflowBinding] = Field(default_factory=list)
    dataflow_events: list[DataflowEvent] = Field(default_factory=list)
    value_states: list[ValueStateRecord] = Field(default_factory=list)
    barriers: list[BarrierFact] = Field(default_factory=list)
    type_parameters: list[TypeParameterDecl] = Field(default_factory=list)
    generic_type_uses: list[GenericTypeUse] = Field(default_factory=list)
    type_declarations: list[TypeDeclaration] = Field(default_factory=list)
    analysis_snapshots: list[JavaAnalysisSnapshot] = Field(default_factory=list)
    analysis_limitation_codes: list[str] = Field(default_factory=list)


def _tree_has_error(root: Any) -> bool:
    """Mirror the shared syntax-error check for Java windows."""
    if getattr(root, "has_error", False):
        return True
    for node in _p.walk(root):
        if node.type == "ERROR" or getattr(node, "is_missing", False):
            return True
    return False


def _span(node: Any, file_path: str) -> Span:
    return Span(
        file_path=file_path,
        line_start=int(node.start_point[0]) + 1,
        line_end=int(node.end_point[0]) + 1,
    )


def _type_annotations(node: Any, source: bytes) -> list[str]:
    return [
        _p.node_text(child, source).strip()
        for child in node.children
        if child.type in {"marker_annotation", "annotation"}
    ]


def _type_syntax(node: Any, source: bytes, file_path: str) -> TypeSyntax:
    text = " ".join(_p.node_text(node, source).split())
    span = _span(node, file_path)
    if node.type == "wildcard":
        named = [child for child in node.children if child.is_named]
        bound = next((child for child in named if child.type != "marker_annotation"), None)
        operator = next((child.type for child in node.children if child.type in {"extends", "super"}), None)
        return TypeSyntax(
            kind="wildcard",
            text=text,
            arguments=[],
            annotations=_type_annotations(node, source),
            span=span,
            name=None,
            element=_type_syntax(bound, source, file_path) if bound is not None else None,
            dimensions=0,
            resolution="UNRESOLVED",
        )
    if node.type == "array_type":
        element = node.child_by_field_name("element")
        dimensions = node.child_by_field_name("dimensions")
        return TypeSyntax(
            kind="array",
            text=text,
            name=None,
            element=_type_syntax(element, source, file_path) if element is not None else None,
            dimensions=_p.node_text(dimensions, source).count("[") if dimensions is not None else 0,
            span=span,
        )
    if node.type == "generic_type":
        base = next((child for child in node.children if child.type != "type_arguments"), None)
        args_node = node.child_by_field_name("type_arguments") or next(
            (child for child in node.children if child.type == "type_arguments"), None
        )
        args = _type_arguments(args_node, source, file_path) if args_node is not None else []
        segment = TypeSegment(
            name=_p.node_text(base, source).strip() if base is not None else text,
            arguments=args,
            text=_p.node_text(base, source).strip() if base is not None else text,
            span=_span(base, file_path) if base is not None else span,
        )
        return TypeSyntax(kind="parameterized", text=text, name=segment.name, segments=[segment], arguments=args, span=span)
    if node.type in {"integral_type", "floating_point_type", "boolean_type", "void_type"}:
        return TypeSyntax(kind="primitive" if node.type != "void_type" else "void", text=text, name=text, span=span)
    if node.type == "annotated_type":
        inner = next((child for child in node.children if child.is_named and child.type not in {"marker_annotation", "annotation"}), None)
        return TypeSyntax(
            kind="annotated", text=text, name=None,
            annotations=_type_annotations(node, source),
            element=_type_syntax(inner, source, file_path) if inner is not None else None,
            span=span,
        )
    return TypeSyntax(kind="named", text=text, name=text, span=span)


def _type_arguments(node: Any, source: bytes, file_path: str) -> list[TypeArgument]:
    if node is None:
        return []
    arguments: list[TypeArgument] = []
    for child in node.children:
        if not child.is_named:
            continue
        if child.type == "wildcard":
            bound_kind = next((item.type for item in child.children if item.type in {"extends", "super"}), None)
            arguments.append(
                TypeArgument(
                    kind=(f"wildcard_{bound_kind}" if bound_kind else "wildcard_unbounded"),
                    type=next((_type_syntax(item, source, file_path) for item in child.children if item.is_named and item.type != "marker_annotation"), None),
                    bound_kind=bound_kind,
                    text=_p.node_text(child, source).strip(),
                    span=_span(child, file_path),
                )
            )
        else:
            arguments.append(
                TypeArgument(
                    kind="explicit_type",
                    type=_type_syntax(child, source, file_path),
                    text=_p.node_text(child, source).strip(),
                    span=_span(child, file_path),
                )
            )
    return arguments


def _build_generic_records(
    root: Any, source: bytes, file_path: str, methods: list[MethodDef]
) -> tuple[list[TypeParameterDecl], list[GenericTypeUse]]:
    parameters: list[TypeParameterDecl] = []
    uses: list[GenericTypeUse] = []
    method_by_span = {(method.span.line_start, method.span.line_end): method for method in methods}

    def owner_for(node: Any) -> tuple[str, str]:
        current = node.parent
        while current is not None:
            if current.type in {"method_declaration", "constructor_declaration", "compact_constructor_declaration"}:
                name_node = current.child_by_field_name("name")
                name = _p.node_text(name_node, source).strip() if name_node is not None else "constructor"
                return name, "method" if current.type == "method_declaration" else "constructor"
            if current.type in {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration"}:
                name_node = current.child_by_field_name("name")
                return (_p.node_text(name_node, source).strip() if name_node is not None else "unknown"), current.type.replace("_declaration", "")
            current = current.parent
        return "window", "class"

    for node in _p.walk(root):
        if node.type == "type_parameters":
            owner_name, owner_kind = owner_for(node)
            for index, parameter in enumerate(child for child in node.children if child.type == "type_parameter"):
                name_node = next((child for child in parameter.children if child.type == "type_identifier"), None)
                if name_node is None:
                    continue
                bounds_node = parameter.child_by_field_name("type_bound") or next((child for child in parameter.children if child.type == "type_bound"), None)
                bounds = [_type_syntax(child, source, file_path) for child in bounds_node.children if child.is_named] if bounds_node is not None else []
                parameters.append(TypeParameterDecl(
                    identity=f"{owner_name}#{_p.node_text(name_node, source)}@{index}",
                    name=_p.node_text(name_node, source).strip(),
                    owner_identity=owner_name,
                    owner_kind=owner_kind if owner_kind in {"class", "interface", "enum", "record", "method", "constructor"} else "class",
                    bounds=bounds,
                    annotations=_type_annotations(parameter, source),
                    span=_span(parameter, file_path),
                ))
        if node.type == "generic_type":
            owner_name, _ = owner_for(node)
            syntax = _type_syntax(node, source, file_path)
            uses.append(GenericTypeUse(
                identity=f"{owner_name}@{node.start_byte}:{node.end_byte}",
                owner_identity=owner_name,
                context="other",
                syntax_kind="parameterized",
                syntax=syntax,
                span=_span(node, file_path),
            ))
        if node.type == "object_creation_expression":
            type_node = node.child_by_field_name("type")
            if type_node is not None and type_node.type == "generic_type":
                args = type_node.child_by_field_name("type_arguments") or next((child for child in type_node.children if child.type == "type_arguments"), None)
                if args is not None and not any(child.is_named for child in args.children):
                    owner_name, _ = owner_for(node)
                    uses.append(GenericTypeUse(
                        identity=f"{owner_name}@{node.start_byte}:{node.end_byte}",
                        owner_identity=owner_name,
                        context="instance_creation",
                        syntax_kind="diamond",
                        syntax=_type_syntax(type_node, source, file_path),
                        inferred_arguments=True,
                        span=_span(node, file_path),
                    ))
    return parameters, uses


def _normalize_guard(text: str | None) -> str:
    """Collapse whitespace for guard-condition comparison."""
    return re.sub(r"\s+", " ", text or "").strip()


def _initializer_detail(
    node: Any, source: bytes, level: int | None
) -> tuple[str | None, str | None, str | float | bool | None, str | None]:
    """Evaluate an initializer once: raw text, kind, payload, limitation.

    Returns (raw, kind, value, limitation) where exactly one of value /
    limitation is meaningful; kind is None only when there is no value node.
    """
    raw = _p.node_text(node, source).strip()
    evaluated, limitation = _expr.evaluate(node, source, level)
    if evaluated is not None:
        kind, payload = evaluated
        return raw, f"value:{kind}", payload, None
    if limitation is not None:
        return raw, f"limitation:{limitation}", None, limitation
    return raw, "complex", None, None


def coerce_evaluated(kind: str | None, value: str | float | bool | None) -> tuple[str, Any] | None:
    """Rebuild the typed evaluated tuple from stored model fields.

    Inverse of the detail extractor for resolver consumption: restores the
    (kind, payload) tuples `format_for_field_type` expects. `kind` is the
    short evaluated kind stored on field/local records (e.g. "int",
    "string"); limitation/complex records carry kind None here.
    """
    if kind is None:
        return None
    if kind in {"int", "long"}:
        return (kind, int(str(value)))
    if kind in {"float", "double"}:
        return (kind, float(str(value)))
    if kind == "char":
        return (kind, int(str(value)))
    if kind == "string":
        return (kind, str(value))
    if kind == "boolean":
        return (kind, str(value) == "True")
    if kind == "null":
        return (kind, None)
    return None


def _branch_returns(consequence: Any, source: bytes, file_path: str) -> list[ReturnSite]:
    """Collect return statements of one guard branch.

    Faithful port of the resolver branch walk: a bare return consequence
    yields itself; otherwise descend into blocks but never into nested
    if-statement subtrees and never into the else branch.
    """
    found: list[Any] = []
    if consequence.type == "return_statement":
        found.append(consequence)
    else:
        stack = [consequence]
        while stack:
            current = stack.pop()
            for child in current.children:
                if child.type == "if_statement":
                    continue
                if child.type == "return_statement":
                    found.append(child)
                elif child.is_named:
                    stack.append(child)
    sites: list[ReturnSite] = []
    for ret in found:
        value_node: Any | None = None
        for child in ret.children:
            if child.is_named:
                value_node = child
                break
        sites.append(
            ReturnSite(
                value_shape=value_node.type if value_node is not None else "",
                value_text=_p.node_text(value_node, source).strip()
                if value_node is not None
                else "",
                raw_text=_p.node_text(ret, source).strip()[:4096] or "return",
                span=_span(ret, file_path),
            )
        )
    return sites


def _same_node(left: Any, right: Any) -> bool:
    return (
        left.type == right.type
        and left.start_byte == right.start_byte
        and left.end_byte == right.end_byte
    )


def _build_dataflow_records(
    root: Any, source: bytes, file_path: str, methods: list[MethodDef]
) -> tuple[list[DataflowBinding], list[DataflowEvent]]:
    """Record bounded binding/events needed by the dataflow resolver."""
    bindings: list[DataflowBinding] = []
    events: list[DataflowEvent] = []
    captures = _p.query_captures(
        root,
        source,
        """
        (method_declaration) @method
        (constructor_declaration) @method
        (compact_constructor_declaration) @method
        """,
    )
    method_nodes = sorted(
        captures.get("method", []), key=lambda node: (node.start_byte, node.end_byte)
    )
    for method_node, method in zip(method_nodes, methods):
        if method.qualified_name == "":
            continue
        identity = f"{method.qualified_name}#{method.key}"
        params_node = method_node.child_by_field_name("parameters")
        if params_node is not None:
            for param in params_node.children:
                if param.type not in {"formal_parameter", "spread_parameter"}:
                    continue
                name_node = param.child_by_field_name("name")
                if name_node is None and param.type == "spread_parameter":
                    name_node = next(
                        (child for child in param.children if child.type == "identifier"), None
                    )
                if name_node is not None:
                    bindings.append(
                        DataflowBinding(
                            method_identity=identity,
                            method_name=method.name,
                            symbol=_p.node_text(name_node, source).strip(),
                            kind="parameter",
                            has_initializer=False,
                            span=_span(param, file_path),
                        )
                    )
        for node in _p.walk(method_node):
            if node.type == "method_declaration" and not _same_node(node, method_node):
                continue
            if node.type == "variable_declarator":
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    bindings.append(
                        DataflowBinding(
                            method_identity=identity,
                            method_name=method.name,
                            symbol=_p.node_text(name_node, source).strip(),
                            kind="local",
                            has_initializer=node.child_by_field_name("value") is not None,
                            span=_span(node, file_path),
                        )
                    )
            if node.type == "return_statement":
                events.append(
                    DataflowEvent(
                        method_identity=identity,
                        symbol=method.name,
                        kind="early_return",
                        value_role="rvalue",
                        span=_span(node, file_path),
                    )
                )
            if node.type == "assignment_expression":
                left = node.child_by_field_name("left")
                if left is not None and left.type == "identifier":
                    operator = next((child.type for child in node.children if not child.is_named), "=")
                    kind = "assignment" if operator == "=" else "compound_assignment"
                    symbol = _p.node_text(left, source).strip()
                    events.append(
                        DataflowEvent(
                            method_identity=identity,
                        symbol=symbol,
                        kind=kind,
                        value_role="lvalue" if kind == "assignment" else "read_modify_write",
                            span=_span(left, file_path),
                        )
                    )
                    if kind == "compound_assignment":
                        events.append(
                            DataflowEvent(
                                method_identity=identity,
                                symbol=symbol,
                                kind="read_modify_write",
                                value_role="read_modify_write",
                                span=_span(left, file_path),
                            )
                        )
            if node.type == "update_expression":
                for child in node.children:
                    if child.type == "identifier":
                        span = _span(child, file_path)
                        symbol = _p.node_text(child, source).strip()
                        events.extend(
                            [
                                DataflowEvent(
                                    method_identity=identity,
                                    symbol=symbol,
                                    kind="update",
                                    value_role="read_modify_write",
                                    span=span,
                                ),
                                DataflowEvent(
                                    method_identity=identity,
                                    symbol=symbol,
                                    kind="read_modify_write",
                                    value_role="read_modify_write",
                                    span=span,
                                ),
                            ]
                        )
            if node.type == "method_invocation":
                args = node.child_by_field_name("arguments")
                if args is not None:
                    for child in args.children:
                        if child.is_named:
                            for descendant in _p.walk(child):
                                if descendant.type == "identifier":
                                    events.append(
                                        DataflowEvent(
                                            method_identity=identity,
                                            symbol=_p.node_text(descendant, source).strip(),
                                            kind="method_argument",
                                            value_role="rvalue",
                                            span=_span(descendant, file_path),
                                        )
                                    )
            if node.type == "identifier":
                parent = node.parent
                if parent is not None and parent.type in {
                    "variable_declarator",
                    "formal_parameter",
                    "method_invocation",
                }:
                    continue
                events.append(
                    DataflowEvent(
                        method_identity=identity,
                        symbol=_p.node_text(node, source).strip(),
                        kind="value_use",
                        value_role="rvalue",
                        span=_span(node, file_path),
                    )
                )
    return bindings, events


def _build_value_and_barrier_records(
    methods: list[MethodDef],
    locals_: list[LocalDef],
    params: list[ParamDef],
    bindings: list[DataflowBinding],
    events: list[DataflowEvent],
    guards: list[GuardBranch],
    file_path: str,
) -> tuple[list[ValueStateRecord], list[BarrierFact]]:
    states: list[ValueStateRecord] = []
    barriers: list[BarrierFact] = []
    method_by_name = {method.name: method for method in methods}
    for binding in bindings:
        method = method_by_name.get(binding.method_name)
        if method is None:
            continue
        local = next(
            (item for item in locals_ if item.name == binding.symbol and item.method_name == binding.method_name),
            None,
        )
        if local is not None and local.evaluated_kind is not None:
            states.append(
                ValueStateRecord(
                    method_identity=binding.method_identity,
                    binding=binding.symbol,
                    state="CONSTANT",
                    kind=local.evaluated_kind,
                    value=None if local.evaluated_value is None else str(local.evaluated_value),
                    source="computed" if local.initializer_kind != f"value:{local.evaluated_kind}" else "literal",
                    span=binding.span,
                )
            )
        elif binding.kind == "parameter":
            states.append(
                ValueStateRecord(
                    method_identity=binding.method_identity,
                    binding=binding.symbol,
                    state="UNKNOWN",
                    source="unknown",
                    span=binding.span,
                )
            )
    for event in events:
        if event.kind in {"assignment", "compound_assignment", "update"}:
            barriers.append(
                BarrierFact(
                    method_identity=event.method_identity,
                    kind="REASSIGNMENT_KILL",
                    subject=event.symbol,
                    limitation="reassignment_kills_prior_value",
                    span=event.span,
                )
            )
        elif event.kind == "method_argument":
            barriers.append(
                BarrierFact(
                    method_identity=event.method_identity,
                    kind="ALIAS_ESCAPE",
                    subject=event.symbol,
                    limitation="interprocedural_aliasing",
                    span=event.span,
                )
            )
    for guard in guards:
        barriers.append(
            BarrierFact(
                method_identity=guard.method_identity,
                kind="GUARD",
                subject=guard.guard_text,
                span=guard.span,
            )
        )
    return states, barriers


def literal_return_value(shape: str, text: str) -> str | None:
    """Unquote a simple literal return expression; None when not a literal.

    Text-form twin of the resolver-side literal check, operating on recorded
    shape + text instead of tree nodes.
    """
    if not shape:
        return None
    if shape == "string_literal":
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
            return text[1:-1]
        return None
    if shape in {"true", "false", "null"}:
        return text
    if shape in {
        "decimal_integer_literal",
        "hex_integer_literal",
        "octal_integer_literal",
        "binary_integer_literal",
        "decimal_floating_point_literal",
        "hex_floating_point_literal",
        "character_literal",
    }:
        return text
    if shape == "unary_expression" and re.fullmatch(r"[-+][^-+].*", text or ""):
        return text
    return None


def infer_static_type(node: Any, source: bytes) -> str:
    """Name the static type of an argument expression, or "unknown".

    Mirrors the resolver-side convention: literals and object creations name
    their type; names, accesses, invocations, and compound expressions stay
    unknown until a stronger analysis exists.
    """
    kind = node.type
    text = _p.node_text(node, source).strip()
    if kind == "decimal_integer_literal":
        return "long" if text.endswith(("l", "L")) else "int"
    if kind == "decimal_floating_point_literal":
        return "float" if text.endswith(("f", "F")) else "double"
    if kind == "string_literal":
        return "String"
    if kind == "character_literal":
        return "char"
    if kind in {"true", "false"}:
        return "boolean"
    if kind == "null_literal":
        return "null"
    if kind == "object_creation_expression":
        type_node = node.child_by_field_name("type")
        return _p.node_text(type_node, source).strip() if type_node is not None else "unknown"
    return "unknown"


def _operator_of(node: Any) -> str | None:
    if node.type == "binary_expression":
        return next((c.type for c in node.children if not c.is_named), None)
    if node.type == "unary_expression":
        return next(
            (c.type for c in node.children if c.type in {"+", "-", "!", "~"}),
            None,
        )
    if node.type == "ternary_expression":
        return "ternary"
    if node.type == "cast_expression":
        return "cast"
    return None


def _arg_sketch(
    index: int, node: Any, source: bytes, level: int | None, file_path: str
) -> ArgSketch:
    text = _p.node_text(node, source).strip()
    shape = node.type
    operator = _operator_of(node)
    static_type = infer_static_type(node, source)
    value_role = "rvalue"
    if node.type in _LITERAL_NODE_TYPES:
        evaluated, _ = _expr.evaluate(node, source, level)
        if evaluated is not None:
            kind, payload = evaluated
            static = _EVALUATED_STATIC_TYPE.get(kind, static_type)
            if node.type == "decimal_floating_point_literal" and text.rstrip().endswith(("f", "F")):
                # The evaluator labels every decimal float double, but the
                # suffix is authoritative for the static type (JLS 3.10.2).
                # Without this, 1.0f resolves as double and loses float
                # overloads in call-target resolution.
                static = "float"
            return ArgSketch(
                index=index,
                kind=f"value:{kind}",
                shape=shape,
                operator=operator,
                static_type=static,
                text=text[:1024],
                literal=LiteralValue(kind=kind, value=str(payload), span=_span(node, file_path)),
                value_role=value_role,
            )
        return ArgSketch(
            index=index,
            kind="literal:undecodable",
            shape=shape,
            operator=operator,
            static_type=static_type,
            text=text[:1024],
            value_role=value_role,
        )
    if node.type == "identifier":
        return ArgSketch(
            index=index,
            kind="name",
            shape=shape,
            operator=operator,
            static_type=static_type,
            text=text[:1024],
            value_role=value_role,
        )
    evaluated, limitation = _expr.evaluate(node, source, level)
    if evaluated is not None:
        return ArgSketch(
            index=index,
            kind=f"value:{evaluated[0]}",
            shape=shape,
            operator=operator,
            static_type=static_type,
            text=text[:1024],
            value_role=value_role,
        )
    if limitation is not None and limitation != "unsupported_initializer_expression":
        return ArgSketch(
            index=index,
            kind=f"limitation:{limitation}",
            shape=shape,
            operator=operator,
            static_type=static_type,
            text=text[:1024],
            value_role=value_role,
        )
    return ArgSketch(
        index=index,
        kind="complex",
        shape=shape,
        operator=operator,
        static_type=static_type,
        text=text[:1024],
        value_role=value_role,
    )


def build_java_model(
    source: bytes,
    *,
    file_path: str,
    repository: str,
    revision: str,
    language_version: object = None,
) -> JavaModel:
    """Build the frozen code model for one Java window.

    Raises the parser exception on unparseable input, mirroring sibling
    resolvers. Windows with syntax errors still produce a model; erroneous
    regions simply contribute no records.
    """
    tree = _p.parse_java(source)
    root = tree.root_node
    level = _lit.normalize_source_level(language_version)
    package = _p.package_name(root, source)
    imports: list[str] = []
    import_captures = _p.query_captures(root, source, "(import_declaration) @import")
    imports.extend(_p.node_text(node, source).strip() for node in import_captures.get("import", []))

    classes: list[str] = []
    methods: list[MethodDef] = []
    calls: list[CallSite] = []
    literals: list[LiteralValue] = []
    fields: list[FieldDef] = []
    locals_: list[LocalDef] = []
    params: list[ParamDef] = []
    guard_returns: list[GuardBranch] = []

    class_stack: list[str] = []
    method_stack: list[str] = []
    bare_stack: list[str] = []
    container_types: list[str] = []
    class_only: list[str] = []
    lambda_depth = 0

    def enclosing_class() -> str | None:
        return class_stack[-1] if class_stack else None

    def enclosing_method() -> str | None:
        return method_stack[-1] if method_stack else None

    def enclosing_bare() -> str:
        return bare_stack[-1] if bare_stack else ""

    def enclosing_container_type() -> str:
        return container_types[-1] if container_types else ""

    def nearest_class() -> str | None:
        return class_only[-1] if class_only else None

    def visit(node: Any) -> None:
        nonlocal lambda_depth
        node_type = node.type
        if node_type in {
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "record_declaration",
            "annotation_type_declaration",
        }:
            name_node = node.child_by_field_name("name")
            if name_node is None:
                return
            name = _p.node_text(name_node, source).strip()
            parent = enclosing_class()
            if parent:
                qualified = f"{parent}.{name}"
            elif package:
                qualified = f"{package}.{name}"
            else:
                qualified = name
            if qualified:
                classes.append(qualified)
                class_stack.append(qualified)
                container_types.append(node_type)
                if node_type == "class_declaration":
                    class_only.append(name)
                for child in node.children:
                    visit(child)
                class_stack.pop()
                container_types.pop()
                if node_type == "class_declaration":
                    class_only.pop()
            return
        if node_type == "lambda_expression":
            lambda_depth += 1
            for child in node.children:
                visit(child)
            lambda_depth -= 1
            return
        if node_type in {
            "method_declaration",
            "constructor_declaration",
            "compact_constructor_declaration",
        }:
            is_ctor = node_type in {"constructor_declaration", "compact_constructor_declaration"}
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                name = _p.node_text(name_node, source).strip()
            elif is_ctor and enclosing_class() is not None:
                name = enclosing_class().rsplit(".", 1)[-1]  # type: ignore[union-attr]
            else:
                name = ""
            params_node = node.child_by_field_name("parameters")
            param_types: list[str] = []
            is_varargs = False
            if params_node is not None:
                for param in params_node.children:
                    if param.type == "formal_parameter":
                        type_node = param.child_by_field_name("type")
                        if type_node is not None:
                            param_types.append(_p.node_text(type_node, source).strip())
                    elif param.type == "spread_parameter":
                        is_varargs = True
                        inner = next(
                            (
                                c
                                for c in param.children
                                if "type" in c.type or c.type == "type_identifier"
                            ),
                            None,
                        )
                        param_types.append(
                            _p.node_text(inner, source).strip() if inner is not None else "unknown"
                        )
            type_node = node.child_by_field_name("type")
            return_type = _p.node_text(type_node, source).strip() if type_node is not None else None
            modifiers = next((c for c in node.children if c.type == "modifiers"), None)
            mod_text = _p.node_text(modifiers, source) if modifiers is not None else ""
            is_override = False
            if modifiers is not None:
                for mod in modifiers.children:
                    if mod.type in {"marker_annotation", "annotation"}:
                        annotation_name = mod.child_by_field_name("name")
                        if (
                            annotation_name is not None
                            and _p.node_text(annotation_name, source).strip() == "Override"
                        ):
                            is_override = True
            cls = enclosing_class()
            qualified = f"{cls}.{name}" if cls and name else name
            key = overload_key(name, len(param_types), param_types)
            methods.append(
                MethodDef(
                    name=name,
                    qualified_name=qualified,
                    arity=len(param_types),
                    param_types=param_types,
                    return_type=return_type,
                    enclosing_class=cls,
                    key=key,
                    is_constructor=is_ctor,
                    is_varargs=is_varargs,
                    is_override=is_override,
                    is_default="default" in mod_text.split(),
                    nearest_class=nearest_class(),
                    declared_in=enclosing_container_type() or "class_declaration",
                    span=_span(node, file_path),
                )
            )
            if qualified:
                method_stack.append(qualified)
                bare_stack.append(name)
                for sub in _p.walk(node):
                    if sub.type != "if_statement":
                        continue
                    consequence = sub.child_by_field_name("consequence")
                    if consequence is None:
                        continue
                    cond = sub.child_by_field_name("condition")
                    guard_returns.append(
                        GuardBranch(
                            enclosing_method=qualified,
                            method_name=name,
                            method_key=key,
                            method_identity=f"{qualified}#{key}",
                            guard_text=_normalize_guard(
                                _p.node_text(cond, source) if cond is not None else ""
                            ),
                            returns=_branch_returns(consequence, source, file_path),
                            span=_span(sub, file_path),
                        )
                    )
                for child in node.children:
                    visit(child)
                bare_stack.pop()
                method_stack.pop()
            return
        if node_type == "method_invocation":
            name_node = node.child_by_field_name("name")
            target = _p.node_text(name_node, source).strip() if name_node is not None else ""
            args_node = node.child_by_field_name("arguments")
            args: list[ArgSketch] = []
            if args_node is not None:
                for index, child in enumerate([c for c in args_node.children if c.is_named]):
                    args.append(_arg_sketch(index, child, source, level, file_path))
            object_node = node.child_by_field_name("object")
            receiver = _p.node_text(object_node, source).strip() if object_node is not None else ""
            if target:
                calls.append(
                    CallSite(
                        target_name=target,
                        arity=len(args),
                        args=args,
                        receiver=receiver,
                        has_method_reference="::" in target or "::" in receiver,
                        in_lambda=lambda_depth > 0,
                        enclosing_method=enclosing_method(),
                        enclosing_class=enclosing_class(),
                        span=_span(node, file_path),
                    )
                )
            for child in node.children:
                visit(child)
            return
        if node_type == "object_creation_expression":
            type_node = node.child_by_field_name("type")
            target = _p.node_text(type_node, source).strip() if type_node is not None else ""
            args_node = node.child_by_field_name("arguments")
            args = []
            if args_node is not None:
                for index, child in enumerate([c for c in args_node.children if c.is_named]):
                    args.append(_arg_sketch(index, child, source, level, file_path))
            if target:
                calls.append(
                    CallSite(
                        target_name=target,
                        arity=len(args),
                        args=args,
                        is_constructor=True,
                        has_method_reference="::" in target,
                        in_lambda=lambda_depth > 0,
                        enclosing_method=enclosing_method(),
                        enclosing_class=enclosing_class(),
                        span=_span(node, file_path),
                    )
                )
            for child in node.children:
                visit(child)
            return
        if node_type in _LITERAL_NODE_TYPES:
            evaluated, _ = _expr.evaluate(node, source, level)
            if evaluated is not None:
                kind, payload = evaluated
                literals.append(
                    LiteralValue(kind=kind, value=str(payload), span=_span(node, file_path))
                )
            for child in node.children:
                visit(child)
            return
        if node_type == "field_declaration":
            modifiers = next((c for c in node.children if c.type == "modifiers"), None)
            mod_text = _p.node_text(modifiers, source) if modifiers is not None else ""
            for child in node.children:
                if child.type != "variable_declarator":
                    continue
                name_node = child.child_by_field_name("name")
                if name_node is None:
                    continue
                type_node = node.child_by_field_name("type")
                value_node = child.child_by_field_name("value")
                raw, kind, payload, limitation = (
                    _initializer_detail(value_node, source, level)
                    if value_node is not None
                    else (None, None, None, None)
                )
                fields.append(
                    FieldDef(
                        name=_p.node_text(name_node, source).strip(),
                        field_type=(
                            _p.node_text(type_node, source).strip()
                            if type_node is not None
                            else "unknown"
                        ),
                        enclosing_class=enclosing_class(),
                        declared_in=enclosing_container_type() or "class_declaration",
                        is_static="static" in mod_text.split(),
                        has_declared_type=type_node is not None,
                        initializer_kind=kind,
                        raw_initializer=raw,
                        evaluated_kind=kind.split(":", 1)[1]
                        if kind and kind.startswith("value:")
                        else None,
                        evaluated_value=(
                            str(payload) if payload is not None and kind != "value:null" else None
                        ),
                        evaluation_limitation=limitation,
                        span=_span(node, file_path),
                    )
                )
            for child in node.children:
                visit(child)
            return
        if node_type == "local_variable_declaration":
            for child in node.children:
                if child.type != "variable_declarator":
                    continue
                name_node = child.child_by_field_name("name")
                if name_node is None:
                    continue
                type_node = node.child_by_field_name("type")
                value_node = child.child_by_field_name("value")
                raw, kind, payload, limitation = (
                    _initializer_detail(value_node, source, level)
                    if value_node is not None
                    else (None, None, None, None)
                )
                locals_.append(
                    LocalDef(
                        name=_p.node_text(name_node, source).strip(),
                        var_type=(
                            _p.node_text(type_node, source).strip()
                            if type_node is not None
                            else "unknown"
                        ),
                        enclosing_method=enclosing_method(),
                        method_name=enclosing_bare(),
                        initializer_kind=kind,
                        raw_initializer=raw,
                        evaluated_kind=kind.split(":", 1)[1]
                        if kind and kind.startswith("value:")
                        else None,
                        evaluated_value=(
                            str(payload) if payload is not None and kind != "value:null" else None
                        ),
                        evaluation_limitation=limitation,
                        span=_span(node, file_path),
                    )
                )
            for child in node.children:
                visit(child)
            return
        if node_type in {"formal_parameter", "spread_parameter"}:
            name_node = node.child_by_field_name("name")
            if name_node is None and node_type == "spread_parameter":
                for declarator_child in node.children:
                    if declarator_child.type == "variable_declarator":
                        name_node = declarator_child.child_by_field_name("name")
                        break
            if name_node is None:
                return
            type_node = node.child_by_field_name("type")
            params.append(
                ParamDef(
                    name=_p.node_text(name_node, source).strip(),
                    var_type=(
                        _p.node_text(type_node, source).strip()
                        if type_node is not None
                        else "unknown"
                    ),
                    enclosing_method=enclosing_method(),
                    method_name=enclosing_bare(),
                    is_varargs=node_type == "spread_parameter",
                    span=_span(node, file_path),
                )
            )
            for child in node.children:
                visit(child)
            return
        for child in node.children:
            visit(child)

    for top in root.children:
        visit(top)

    dataflow_bindings, dataflow_events = _build_dataflow_records(
        root, source, file_path, methods
    )
    value_states, barriers = _build_value_and_barrier_records(
        methods, locals_, params, dataflow_bindings, dataflow_events, guard_returns, file_path
    )
    type_parameters, generic_type_uses = _build_generic_records(root, source, file_path, methods)

    model = JavaModel(
        parser_version=_p.parser_version(),
        has_syntax_error=_tree_has_error(root),
        repository=repository,
        revision=revision,
        file_path=file_path,
        package=package,
        imports=imports,
        classes=classes,
        methods=methods,
        calls=calls,
        literals=literals,
        fields=fields,
        locals=locals_,
        params=params,
        guard_returns=guard_returns,
        dataflow_bindings=dataflow_bindings,
        dataflow_events=dataflow_events,
        value_states=value_states,
        barriers=barriers,
        type_parameters=type_parameters,
        generic_type_uses=generic_type_uses,
    )
    # The advanced feature pass consumes this same parsed tree.  The import is
    # local to keep the model/query layer independent from fact assembly.
    from investigation_agent_platform.application.analysis.java.type_facts import (
        build_type_analysis_snapshots,
    )

    snapshots = build_type_analysis_snapshots(
        root,
        source,
        file_path=file_path,
        language_version=level,
    )
    return model.model_copy(
        update={
            "type_declarations": [snapshot.declaration for snapshot in snapshots],
            "analysis_snapshots": snapshots,
            "analysis_limitation_codes": sorted(
                {code for snapshot in snapshots for code in snapshot.partial_codes}
            ),
        }
    )
