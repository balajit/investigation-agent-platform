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


def calls_with_computed_arg(calls: Iterable[CallSite], index: int) -> Iterator[CallSite]:
    """Keep calls whose argument `index` is present but not statically evaluable."""
    for call in calls:
        if arg_not_literal(call, index):
            yield call
