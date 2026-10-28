# Java Query Items 6-7 Plan v1.0

Status: IMPLEMENTED 2026-10-28. This document covers only Item 6 (use-def and constant propagation across locals) and Item 7 (guard/barrier predicates). It does not authorize framework data externalization, lvalue/rvalue changes, Tree-sitter query migration, or other deferred capabilities.

## Goals

1. Make effective constant values queryable across local initializers and subsequent uses within one bounded method window.
2. Make flow blockers queryable without pretending to prove runtime validation, execution order, or interprocedural behavior.
3. Preserve the existing honest-abstention contract: unknown, escaped, aliased, reassigned, and out-of-window values are never silently treated as constants or safe barriers.

## Shared Constraints

- All identities are class-qualified method identities plus local/parameter binding identity; bare names are never sufficient across methods or classes.
- Analysis is single-window and revision-pinned.
- Propagation is intraprocedural and bounded to the method containing the binding.
- A query result may establish a source-level shape or conservative fact, never that code executed or that a runtime sanitizer actually ran.
- Any unsupported expression, alias, mutation, control-flow merge, or external reference produces an explicit unknown/limitation state rather than a guessed value.
- The existing `arg_is_constant` predicate remains syntactic/model-evaluation based until the new effective-value predicate is added; no silent semantic change to existing predicates.

## Item 6: Use-Def and Constant Propagation

### Phase 1: Freeze the value-state vocabulary

Add a closed value-state vocabulary to the Java query spec:

- `CONSTANT`: exactly one source-level constant value reaches the use under the supported bounded rules.
- `UNKNOWN`: no safe constant can be established.
- `KILLED`: a reassignment or unsupported mutation invalidates the previous constant before the use.
- `AMBIGUOUS`: multiple reaching definitions provide different values.
- `ALIASED`: a method call, returned reference, field access, array access, lambda escape, or other boundary prevents local certainty.
- `OUT_OF_WINDOW`: the binding or reaching definition is outside the supplied model.

Define the distinction between:

- Syntactic literal: `arg_is_literal` remains true only for a decoded literal record.
- Locally computed constant: an expression such as `1 + 2` is `CONSTANT` but not syntactically literal.
- Propagated constant: a use such as `sink(query)` is `CONSTANT` only when `query` has one valid reaching constant definition.

### Phase 2: Add typed model records

Extend the existing dataflow model with the smallest records needed:

- `ValueDefinition`: class-qualified method identity, binding identity, definition kind, expression shape/text, evaluated value/kind, and span.
- `ValueUse`: binding identity, use context, reaching-definition IDs or keys, and span.
- `ValueState`: effective state, optional constant kind/value, limitation code, and provenance of the reaching definitions.
- `BindingIdentity`: stable method-qualified local/parameter identity; do not use display names as keys.

The model must record assignment order and source spans. It must not infer types or values beyond the existing conservative expression evaluator.

### Phase 3: Conservative transfer rules

Implement a pure intraprocedural propagation engine:

- Local/parameter initializer with a supported constant yields `CONSTANT`.
- Literal and supported side-effect-free expression evaluation yields `CONSTANT`.
- A plain assignment replaces the previous state when the right-hand side is supported; otherwise it yields `UNKNOWN`.
- Compound assignment and increment/decrement yield `KILLED` or `UNKNOWN` because they read and mutate the prior value.
- A call argument creates an `ALIASED` barrier unless the argument is proven value-only under the existing contract; the default is conservative aliasing.
- Returning a value, storing it in a field/array, passing it to a method, capturing it in a lambda, or using it through an unknown receiver prevents propagation beyond that boundary.
- Branch joins with equal constants retain `CONSTANT`; differing constants become `AMBIGUOUS`; any unknown/aliased path becomes `UNKNOWN` unless the engine can prove it unreachable from the bounded source facts.
- Any use before a definition remains `UNKNOWN` and preserves the existing dataflow limitation semantics.
- Cross-method, cross-window, field, array, reflection, stream, and concurrency propagation is explicitly deferred.

### Phase 4: Query surface

Add predicates/accessors with exact truth conditions:

- `value_state(use)` returns the closed state.
- `value_is_constant(use, kind=None, value=None)` is true only for `CONSTANT` with optional exact kind/value match.
- `value_is_propagated_constant(use)` distinguishes a local/parameter use from a direct literal.
- `value_reaching_definitions(use)` exposes the bounded definition records without resolving beyond the window.
- `calls_with_effective_constant_arg(calls, index, kind=None, value=None)` composes call filtering with value states.

Existing syntactic predicates remain unchanged and documented as such.

### Phase 5: Tests and acceptance

Add table-driven positive, negative, and abstention tests for:

- Direct local initializer to call argument.
- Local alias chain with one constant reaching definition.
- Computed constant versus syntactic literal distinction.
- Reassignment kill before use.
- Compound assignment and increment/decrement.
- Differing branch constants and equal branch constants.
- Unknown initializer or unsupported expression.
- Parameter use with no local definition.
- Method call alias boundary.
- Return, field, array, lambda, stream, and concurrency boundaries.
- Same-name locals in separate methods/classes.
- Out-of-window and syntax-error windows.

Acceptance requires deterministic state and provenance outputs, no existing predicate behavior changes, and full Java/unit quality gates.

## Item 7: Guard and Barrier Predicates

### Phase 1: Define the barrier vocabulary

Add typed source-level barrier states:

- `GUARD`: a condition that syntactically constrains a path.
- `VALIDATION_CALL`: a call-shaped validation or sanitization operation recognized only from an explicit configured vocabulary.
- `REASSIGNMENT_KILL`: a write/update that invalidates a prior value fact.
- `ALIAS_ESCAPE`: a value crosses a call/return/field/lambda boundary.
- `UNKNOWN_BARRIER`: a construct may affect the fact, but the model cannot classify it safely.

These records describe possible source barriers, not proof that a guard was true,
that a validation call executed, or that a sanitizer was effective.

### Phase 2: Model records

Add only records required for queryable barriers:

- `GuardFact`: method identity, normalized condition text, enclosing construct, branch span, and referenced symbols.
- `BarrierFact`: method identity, barrier kind, subject binding(s), call target when present, source span, and limitation/state.
- `FlowBoundary`: relation between a value use and the barrier/guard facts that may block propagation.

Reuse existing `GuardBranch`, `DataflowEvent`, and method-reference/call records where their identity and spans are sufficient. Do not duplicate guard returns or invent runtime status fields.

### Phase 3: Conservative recognition

- Recognize guards from parsed conditional constructs only; no natural-language or substring guard matching in the query layer.
- Recognize reassignment kills from typed assignment/update events.
- Recognize validation/sanitization calls only through a closed, versioned configuration or explicit caller-supplied vocabulary. No heuristic “method name contains validate/sanitize” rule.
- Mark calls with unknown receivers, method references, reflection, callbacks, lambdas, streams, and concurrency as `UNKNOWN_BARRIER` or `ALIAS_ESCAPE` where applicable.
- At joins, preserve all possible barriers. Never conclude that a value is safe merely because one path lacks a barrier.
- A barrier predicate returns source-level presence/possible blocking, not execution or effectiveness.

### Phase 4: Query surface

Add exact predicates/accessors:

- `guards_for_method(models, method_identity)`.
- `barriers_for_method(models, method_identity)`.
- `reassignment_kills(events, binding_identity)`.
- `calls_with_validation_barrier(calls, vocabulary)`.
- `flow_is_blocked(use, barriers)` returns only `BLOCKED`, `NOT_BLOCKED_IN_WINDOW`, or `UNKNOWN`, with `UNKNOWN` preferred whenever coverage is incomplete.

Document that `NOT_BLOCKED_IN_WINDOW` means no recognized blocker was found in the supplied bounded records, not that no blocker exists globally.

### Phase 5: Tests and acceptance

Add tests for:

- Guard discovery with normalized condition and method identity.
- Reassignment kill before and after a use.
- Validation vocabulary positive and non-matching calls.
- Same method name in separate classes.
- Multiple branches where one branch has a barrier and one does not.
- Unknown receiver/callback/lambda/stream/concurrency barriers.
- Reflection and unsupported constructs withheld as unknown.
- No substring-based false positives (`validateNowhere`, comments, unrelated method names).
- Syntax-error and partial windows.
- Execution-safety assertions proving no result claims a guard or sanitizer ran.

## Implementation Order

1. Amend the vocabulary with Item 6 states and Item 7 barrier semantics.
2. Add model records and pure propagation/barrier discovery without changing existing predicates.
3. Add query predicates and contract tests.
4. Run all old Java tests plus the new positive/negative/abstention matrix.
5. Promote only after the model records and query contracts are stable; do not alter existing resolver conclusions as an incidental effect.
6. Update the A7 bucket inventory and ledger with the new in-model records and the still-deferred cross-window/interprocedural boundaries.

## Final Acceptance

Items 6 and 7 are complete only when their closed vocabularies, model records, query predicates, negative/unknown tests, execution-safety tests, and full unit/Ruff/format/diff gates pass. Any unknown, alias, mutation, or cross-window case that becomes a positive constant or effective barrier is a release-blocking defect.

## Implementation Evidence

- Item 6: `DataflowBinding`, `DataflowEvent`, and `ValueStateRecord` are populated by the shared Java model; effective-constant call filtering is available without changing syntactic literal predicates. Reassignment and alias events remain explicit barriers.
- Item 7: `BarrierFact` records guards, reassignment kills, and alias escapes; query helpers expose method-scoped barriers, conservative flow status, and exact caller-supplied validation vocabulary matching. No method-name substring heuristic or execution claim was added.
- Verification: full unit suite, Ruff check, Ruff format check, and `git diff --check` passed. No commit or push was performed.
