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
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.application.analysis.java.model import (
    CallSite,
    DataflowBinding,
    DataflowEvent,
    BarrierFact,
    ValueStateRecord,
    FieldDef,
    GuardBranch,
    JavaModel,
    LiteralValue,
    LocalDef,
    MethodDef,
    ParamDef,
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
    """Keep calls whose target resolves to a definition under `library_prefix`.

    Attribution rule: same target name AND same arity AND the definition's
    qualified name under the prefix. Name-only matches are withheld: a
    same-named definition with a different arity never attributes. Residual
    risk is limited to same-name/same-arity collisions across packages, which
    the corpus, not the predicate, must rule out.
    """
    model_list = _as_list(models)
    for call in calls:
        for definition in definitions_named(model_list, call.target_name):
            if definition.arity != call.arity:
                continue
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
    """True when argument `index` is syntactically a literal.

    Uses the decoded literal record, not the evaluated kind: a computed
    constant such as `1 + 2` evaluates to an int but is not a literal.
    """
    return 0 <= index < len(call.args) and call.args[index].literal is not None


def arg_is_constant(call: CallSite, index: int) -> bool:
    """True when argument `index` statically evaluates to a constant.

    Covers both syntactic literals and computed constants (e.g. `-5`,
    `1 + 2`). Use this for "known value" questions and `arg_is_literal`
    for "written as a literal" questions.
    """
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
    """True when argument `index` is a string literal (not a computed string)."""
    return (
        0 <= index < len(call.args)
        and call.args[index].literal is not None
        and call.args[index].literal.kind == "string"
    )


def is_int_arg(call: CallSite, index: int) -> bool:
    """True when argument `index` is an int literal (not a computed int)."""
    return (
        0 <= index < len(call.args)
        and call.args[index].literal is not None
        and call.args[index].literal.kind == "int"
    )


def is_boolean_arg(call: CallSite, index: int) -> bool:
    """True when argument `index` is a boolean literal."""
    return (
        0 <= index < len(call.args)
        and call.args[index].literal is not None
        and call.args[index].literal.kind == "boolean"
    )


def is_null_arg(call: CallSite, index: int) -> bool:
    """True when argument `index` is the null literal."""
    return (
        0 <= index < len(call.args)
        and call.args[index].literal is not None
        and call.args[index].literal.kind == "null"
    )


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


def all_fields(models: JavaModel | Iterable[JavaModel]) -> Iterator[FieldDef]:
    """Yield every field declaration in the models."""
    for model in _as_list(models):
        yield from model.fields


def all_locals(models: JavaModel | Iterable[JavaModel]) -> Iterator[LocalDef]:
    """Yield every local variable declaration in the models."""
    for model in _as_list(models):
        yield from model.locals


def all_params(models: JavaModel | Iterable[JavaModel]) -> Iterator[ParamDef]:
    """Yield every formal/spread parameter in the models."""
    for model in _as_list(models):
        yield from model.params


def all_dataflow_bindings(
    models: JavaModel | Iterable[JavaModel],
) -> Iterator[DataflowBinding]:
    """Yield bounded local/parameter binding records."""
    for model in _as_list(models):
        yield from model.dataflow_bindings


def all_dataflow_events(models: JavaModel | Iterable[JavaModel]) -> Iterator[DataflowEvent]:
    """Yield bounded dataflow events."""
    for model in _as_list(models):
        yield from model.dataflow_events


def events_with_value_role(
    events: Iterable[DataflowEvent], role: str
) -> Iterator[DataflowEvent]:
    """Yield events with the exact lvalue/rvalue role."""
    for event in events:
        if event.value_role == role:
            yield event


def all_value_states(models: JavaModel | Iterable[JavaModel]) -> Iterator[ValueStateRecord]:
    """Yield conservative effective-value records."""
    for model in _as_list(models):
        yield from model.value_states


def all_barriers(models: JavaModel | Iterable[JavaModel]) -> Iterator[BarrierFact]:
    """Yield source-level possible flow barriers."""
    for model in _as_list(models):
        yield from model.barriers


def value_is_constant(state: ValueStateRecord, kind: str | None = None, value: str | None = None) -> bool:
    """True only for a bounded constant with optional exact kind/value filters."""
    return (
        state.state == "CONSTANT"
        and (kind is None or state.kind == kind)
        and (value is None or state.value == value)
    )


def value_is_propagated_constant(state: ValueStateRecord) -> bool:
    """True for a constant originating from a computed or propagated value."""
    return value_is_constant(state) and state.source in {"computed", "propagated"}


def calls_with_effective_constant_arg(
    calls: Iterable[CallSite],
    states: Iterable[ValueStateRecord],
    index: int,
    kind: str | None = None,
    value: str | None = None,
) -> Iterator[CallSite]:
    """Yield calls whose named argument has a matching bounded value state."""
    state_by_binding = {
        state.binding: state for state in states if value_is_constant(state, kind, value)
    }
    for call in calls:
        if not 0 <= index < len(call.args):
            continue
        argument = call.args[index]
        if argument.kind == "name" and argument.text in state_by_binding:
            yield call


def guards_for_method(
    models: JavaModel | Iterable[JavaModel], method_identity: str
) -> Iterator[BarrierFact]:
    """Yield guard barriers for one class-qualified method identity."""
    for barrier in all_barriers(models):
        if barrier.method_identity == method_identity and barrier.kind == "GUARD":
            yield barrier


def barriers_for_method(
    models: JavaModel | Iterable[JavaModel], method_identity: str
) -> Iterator[BarrierFact]:
    """Yield all possible barriers for one class-qualified method identity."""
    for barrier in all_barriers(models):
        if barrier.method_identity == method_identity:
            yield barrier


def reassignment_kills(
    events: Iterable[DataflowEvent], binding: str
) -> Iterator[DataflowEvent]:
    """Yield assignment/update events that kill a binding's prior value."""
    for event in events:
        if event.symbol == binding and event.kind in {"assignment", "compound_assignment", "update"}:
            yield event


def flow_is_blocked(
    state: ValueStateRecord, barriers: Iterable[BarrierFact]
) -> str:
    """Return BLOCKED, NOT_BLOCKED_IN_WINDOW, or UNKNOWN conservatively."""
    relevant = [barrier for barrier in barriers if barrier.subject in {None, state.binding}]
    if any(barrier.kind in {"ALIAS_ESCAPE", "UNKNOWN_BARRIER"} for barrier in relevant):
        return "UNKNOWN"
    if any(barrier.kind in {"REASSIGNMENT_KILL", "VALIDATION_CALL", "GUARD"} for barrier in relevant):
        return "BLOCKED"
    return "NOT_BLOCKED_IN_WINDOW"


def calls_with_validation_barrier(
    calls: Iterable[CallSite], vocabulary: Iterable[str]
) -> Iterator[CallSite]:
    """Yield calls whose exact target name is in an explicit vocabulary."""
    names = frozenset(name for name in vocabulary if name)
    for call in calls:
        if call.target_name in names:
            yield call


def locals_named(locals_: Iterable[LocalDef], name: str) -> Iterator[LocalDef]:
    """Keep locals with exactly `name`."""
    for local in locals_:
        if local.name == name:
            yield local


def params_named(params: Iterable[ParamDef], name: str) -> Iterator[ParamDef]:
    """Keep parameters with exactly `name`."""
    for param in params:
        if param.name == name:
            yield param


def guard_branches(models: JavaModel | Iterable[JavaModel]) -> Iterator[GuardBranch]:
    """Yield every recorded guard branch in the models."""
    for model in _as_list(models):
        yield from model.guard_returns


def guard_branches_for_method(
    models: JavaModel | Iterable[JavaModel], method_key: str
) -> Iterator[GuardBranch]:
    """Yield guard branches belonging to the method with `method_key`."""
    for branch in guard_branches(models):
        if branch.method_key == method_key:
            yield branch


class TargetCandidate(BaseModel):
    """One same-named definition considered for a call site.

    Uncertainty as data: every same-named definition is listed with a status,
    never silently dropped. `param_types` and the stable overload `key`
    distinguish overloads that share a name and arity (e.g. `fmt(float)` vs
    `fmt(double)`); consumers must key on `key`, never on qualified name
    alone. MATCH means arity-compatible; ARITY_MISMATCH records the counts.
    Resolution phases and specificity remain the authoritative resolver's job.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    qualified_name: str = Field(min_length=1, max_length=1024)
    arity: int = Field(ge=0)
    param_types: list[str] = Field(default_factory=list)
    key: str = Field(min_length=1, max_length=1024)
    status: Literal["MATCH", "ARITY_MISMATCH"] = "MATCH"
    reason: str = Field(min_length=1, max_length=256)
    is_varargs: bool = False


def candidate_targets(
    call: CallSite, models: JavaModel | Iterable[JavaModel]
) -> list[TargetCandidate]:
    """List every same-named definition for `call`, with match status.

    Exact-arity definitions match with reason `exact-arity`. Varargs
    definitions match when the call supplies at least the fixed parameters
    (reason `varargs-compatible`). All other same-named definitions are
    listed as ARITY_MISMATCH with both counts. No definition in the corpus
    yields an empty list: withheld, never guessed.
    """
    found: list[TargetCandidate] = []
    for definition in definitions_named(models, call.target_name):
        if definition.arity == call.arity:
            found.append(
                TargetCandidate(
                    qualified_name=definition.qualified_name,
                    arity=definition.arity,
                    param_types=list(definition.param_types),
                    key=definition.key,
                    status="MATCH",
                    reason="exact-arity",
                    is_varargs=definition.is_varargs,
                )
            )
        elif definition.is_varargs and call.arity >= definition.arity - 1:
            found.append(
                TargetCandidate(
                    qualified_name=definition.qualified_name,
                    arity=definition.arity,
                    param_types=list(definition.param_types),
                    key=definition.key,
                    status="MATCH",
                    reason="varargs-compatible",
                    is_varargs=True,
                )
            )
        else:
            found.append(
                TargetCandidate(
                    qualified_name=definition.qualified_name,
                    arity=definition.arity,
                    param_types=list(definition.param_types),
                    key=definition.key,
                    status="ARITY_MISMATCH",
                    reason=f"arity-mismatch: call {call.arity}, def {definition.arity}",
                    is_varargs=definition.is_varargs,
                )
            )
    found.sort(key=lambda candidate: (candidate.status != "MATCH", candidate.qualified_name))
    return found
