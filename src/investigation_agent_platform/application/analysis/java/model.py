"""Java window code model (Q0 query layer).

Builds frozen, typed records from one parsed tree-sitter Java window. The
model is the shared "database" that query predicates (`query.py`) and future
resolver migrations consume. It never executes code and never leaves the
window: every record carries file path, line range, and revision.
"""

from __future__ import annotations

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
    span: Span


class FieldDef(BaseModel):
    """A field declaration with optional evaluated initializer kind."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=512)
    field_type: str = Field(min_length=1, max_length=512)
    enclosing_class: str | None = Field(default=None, max_length=1024)
    initializer_kind: str | None = Field(default=None, max_length=64)
    span: Span


class LocalDef(BaseModel):
    """A local variable declaration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=512)
    var_type: str = Field(min_length=1, max_length=512)
    enclosing_method: str | None = Field(default=None, max_length=1024)
    initializer_kind: str | None = Field(default=None, max_length=64)
    span: Span


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


def _initializer_kind(node: Any, source: bytes, level: int | None) -> str | None:
    evaluated, limitation = _expr.evaluate(node, source, level)
    if evaluated is not None:
        return f"value:{evaluated[0]}"
    if limitation is not None:
        return f"limitation:{limitation}"
    return "complex"


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
    if node.type in _LITERAL_NODE_TYPES:
        evaluated, _ = _expr.evaluate(node, source, level)
        if evaluated is not None:
            kind, payload = evaluated
            return ArgSketch(
                index=index,
                kind=f"value:{kind}",
                shape=shape,
                operator=operator,
                static_type=_EVALUATED_STATIC_TYPE.get(kind, static_type),
                text=text[:1024],
                literal=LiteralValue(kind=kind, value=str(payload), span=_span(node, file_path)),
            )
        return ArgSketch(
            index=index,
            kind="literal:undecodable",
            shape=shape,
            operator=operator,
            static_type=static_type,
            text=text[:1024],
        )
    if node.type == "identifier":
        return ArgSketch(
            index=index,
            kind="name",
            shape=shape,
            operator=operator,
            static_type=static_type,
            text=text[:1024],
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
        )
    if limitation is not None and limitation != "unsupported_initializer_expression":
        return ArgSketch(
            index=index,
            kind=f"limitation:{limitation}",
            shape=shape,
            operator=operator,
            static_type=static_type,
            text=text[:1024],
        )
    return ArgSketch(
        index=index,
        kind="complex",
        shape=shape,
        operator=operator,
        static_type=static_type,
        text=text[:1024],
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
    for node in _p.walk(root):
        if node.type == "import_declaration":
            imports.append(_p.node_text(node, source).strip())

    classes: list[str] = []
    methods: list[MethodDef] = []
    calls: list[CallSite] = []
    literals: list[LiteralValue] = []
    fields: list[FieldDef] = []
    locals_: list[LocalDef] = []

    class_stack: list[str] = []
    method_stack: list[str] = []
    lambda_depth = 0

    def enclosing_class() -> str | None:
        return class_stack[-1] if class_stack else None

    def enclosing_method() -> str | None:
        return method_stack[-1] if method_stack else None

    def visit(node: Any) -> None:
        nonlocal lambda_depth
        node_type = node.type
        if node_type in {
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "record_declaration",
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
                for child in node.children:
                    visit(child)
                class_stack.pop()
            return
        if node_type == "lambda_expression":
            lambda_depth += 1
            for child in node.children:
                visit(child)
            lambda_depth -= 1
            return
        if node_type in {"method_declaration", "constructor_declaration"}:
            is_ctor = node_type == "constructor_declaration"
            name_node = node.child_by_field_name("name")
            name = _p.node_text(name_node, source).strip() if name_node is not None else ""
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
                    span=_span(node, file_path),
                )
            )
            if qualified:
                method_stack.append(qualified)
                for child in node.children:
                    visit(child)
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
            for child in node.children:
                if child.type != "variable_declarator":
                    continue
                name_node = child.child_by_field_name("name")
                if name_node is None:
                    continue
                type_node = node.child_by_field_name("type")
                value_node = child.child_by_field_name("value")
                fields.append(
                    FieldDef(
                        name=_p.node_text(name_node, source).strip(),
                        field_type=(
                            _p.node_text(type_node, source).strip()
                            if type_node is not None
                            else "unknown"
                        ),
                        enclosing_class=enclosing_class(),
                        initializer_kind=(
                            _initializer_kind(value_node, source, level)
                            if value_node is not None
                            else None
                        ),
                        span=_span(child, file_path),
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
                locals_.append(
                    LocalDef(
                        name=_p.node_text(name_node, source).strip(),
                        var_type=(
                            _p.node_text(type_node, source).strip()
                            if type_node is not None
                            else "unknown"
                        ),
                        enclosing_method=enclosing_method(),
                        initializer_kind=(
                            _initializer_kind(value_node, source, level)
                            if value_node is not None
                            else None
                        ),
                        span=_span(child, file_path),
                    )
                )
            for child in node.children:
                visit(child)
            return
        for child in node.children:
            visit(child)

    for top in root.children:
        visit(top)

    return JavaModel(
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
    )
