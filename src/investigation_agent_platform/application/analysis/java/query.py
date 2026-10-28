"""QL-style query predicates over the Java window code model.

Each function is a composable generator predicate: `all_calls` answers "show
me all calls", `named` narrows by target, `in_library` scopes by defining
package, `arg_is_literal` constrains by argument shape. Predicates compose by
nesting, mirroring QL `from/where` chains, and execute as typed Python over
frozen model records. Single-window models answer single-window questions;
multi-model helpers join definitions across windows for callee-side library
attribution. No execution, no I/O, no authority.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from investigation_agent_platform.application.analysis.java.model import (
    CallSite,
    JavaModel,
    LiteralValue,
    MethodDef,
)


def _as_list(models: JavaModel | Iterable[JavaModel]) -> list[JavaModel]:
    if isinstance(models, JavaModel):
        return [models]
    return list(models)


def all_calls(models: JavaModel | Iterable[JavaModel]) -> Iterator[CallSite]:
    """Yield every call site (invocations and constructions) in the models."""
    for model in _as_list(models):
        yield from model.calls


def all_methods(models: JavaModel | Iterable[JavaModel]) -> Iterator[MethodDef]:
    """Yield every method/constructor definition in the models."""
    for model in _as_list(models):
        yield from model.methods


def all_literals(models: JavaModel | Iterable[JavaModel]) -> Iterator[LiteralValue]:
    """Yield every decoded literal in the models."""
    for model in _as_list(models):
        yield from model.literals


def named(calls: Iterable[CallSite], name: str) -> Iterator[CallSite]:
    """Keep calls whose target name exactly equals `name`."""
    for call in calls:
        if call.target_name == name:
            yield call


def named_qualified(methods: Iterable[MethodDef], qualified: str) -> Iterator[MethodDef]:
    """Keep definitions whose qualified name exactly equals `qualified`."""
    for method in methods:
        if method.qualified_name == qualified:
            yield method


def with_arity(calls: Iterable[CallSite], arity: int) -> Iterator[CallSite]:
    """Keep calls with exactly `arity` arguments."""
    for call in calls:
        if call.arity == arity:
            yield call


def definitions_named(models: JavaModel | Iterable[JavaModel], name: str) -> Iterator[MethodDef]:
    """Yield definitions named `name` across the models."""
    for method in all_methods(models):
        if method.name == name:
            yield method


def in_library(
    calls: Iterable[CallSite],
    models: JavaModel | Iterable[JavaModel],
    library_prefix: str,
) -> Iterator[CallSite]:
    """Keep calls whose target is defined under `library_prefix`.

    Callee-side attribution: a call qualifies when a same-corpus definition
    with the same target name has a qualified name under the prefix. Calls
    whose target has no definition in the corpus are withheld, never guessed.
    Single-window callers cannot attribute out-of-window callees.
    """
    model_list = _as_list(models)
    for call in calls:
        for definition in definitions_named(model_list, call.target_name):
            if definition.qualified_name == library_prefix or definition.qualified_name.startswith(
                library_prefix + "."
            ):
                yield call
                break


def in_enclosing_package(calls: Iterable[CallSite], package_prefix: str) -> Iterator[CallSite]:
    """Keep calls whose enclosing class lives under `package_prefix` (caller side)."""
    for call in calls:
        if call.enclosing_class is not None and (
            call.enclosing_class == package_prefix
            or call.enclosing_class.startswith(package_prefix + ".")
        ):
            yield call


def arg_kind(call: CallSite, index: int) -> str | None:
    """Return the shape kind of argument `index`, or None when absent."""
    if 0 <= index < len(call.args):
        return call.args[index].kind
    return None


def arg_is_literal(call: CallSite, index: int) -> bool:
    """True when argument `index` is a decoded literal of any kind."""
    kind = arg_kind(call, index)
    return kind is not None and kind.startswith("value:")


def arg_not_literal(call: CallSite, index: int) -> bool:
    """True when argument `index` exists and is not a decoded literal."""
    return 0 <= index < len(call.args) and not arg_is_literal(call, index)


def arg_evaluates_to(call: CallSite, index: int, kind: str, value: str) -> bool:
    """True when argument `index` statically evaluates to the given kind/value."""
    if not 0 <= index < len(call.args):
        return False
    literal = call.args[index].literal
    return literal is not None and literal.kind == kind and literal.value == value


def takes_string_literal(call: CallSite) -> bool:
    """True when any argument is a decoded string literal."""
    return any(arg.literal is not None and arg.literal.kind == "string" for arg in call.args)


def definitions_in_package(
    methods: Iterable[MethodDef], package_prefix: str
) -> Iterator[MethodDef]:
    """Keep definitions whose qualified name lives under `package_prefix`."""
    for method in methods:
        if method.qualified_name == package_prefix or method.qualified_name.startswith(
            package_prefix + "."
        ):
            yield method


def calls_to_in_library(
    calls: Iterable[CallSite],
    models: JavaModel | Iterable[JavaModel],
    library_prefix: str,
    name: str,
) -> Iterator[CallSite]:
    """Composed form of the question 6 shape: named calls bound to a library."""
    return in_library(named(calls, name), models, library_prefix)


def methods_with_arity(methods: Iterable[MethodDef], arity: int) -> Iterator[MethodDef]:
    """Keep definitions with exactly `arity` parameters."""
    for method in methods:
        if method.arity == arity:
            yield method


def arg_shape(call: CallSite, index: int) -> str | None:
    """Return the tree-sitter shape of argument `index`, or None when absent."""
    if 0 <= index < len(call.args):
        return call.args[index].shape
    return None


def arg_operator(call: CallSite, index: int) -> str | None:
    """Return the operator of argument `index`, or None when not applicable."""
    if 0 <= index < len(call.args):
        return call.args[index].operator
    return None


def arg_static_type(call: CallSite, index: int) -> str | None:
    """Return the inferred static type of argument `index`, or None when absent."""
    if 0 <= index < len(call.args):
        return call.args[index].static_type
    return None


def arg_is_name(call: CallSite, index: int) -> bool:
    """True when argument `index` is a bare identifier reference."""
    return arg_kind(call, index) == "name"


def arg_is_complex(call: CallSite, index: int) -> bool:
    """True when argument `index` is a non-literal, non-name expression."""
    kind = arg_kind(call, index)
    return kind is not None and not kind.startswith("value:") and kind != "name"


def arg_has_limitation(call: CallSite, index: int) -> bool:
    """True when argument `index` carries a closed evaluation-limitation code."""
    kind = arg_kind(call, index)
    return kind is not None and kind.startswith("limitation:")


def calls_with_computed_arg(calls: Iterable[CallSite], index: int) -> Iterator[CallSite]:
    """Keep calls whose argument `index` is present but not statically evaluable."""
    for call in calls:
        if arg_not_literal(call, index):
            yield call


def is_string_arg(call: CallSite, index: int) -> bool:
    """True when argument `index` is a decoded string literal."""
    return arg_kind(call, index) == "value:string"


def is_int_arg(call: CallSite, index: int) -> bool:
    """True when argument `index` is a decoded int literal."""
    return arg_kind(call, index) == "value:int"


def is_boolean_arg(call: CallSite, index: int) -> bool:
    """True when argument `index` is a decoded boolean literal."""
    return arg_kind(call, index) == "value:boolean"


def is_null_arg(call: CallSite, index: int) -> bool:
    """True when argument `index` is the null literal."""
    return arg_kind(call, index) == "value:null"


def calls_with_receiver(calls: Iterable[CallSite], receiver: str) -> Iterator[CallSite]:
    """Keep calls invoked on the exact receiver expression text."""
    for call in calls:
        if call.receiver == receiver:
            yield call


def calls_in_lambda(calls: Iterable[CallSite]) -> Iterator[CallSite]:
    """Keep calls enclosed in a lambda body."""
    for call in calls:
        if call.in_lambda:
            yield call
