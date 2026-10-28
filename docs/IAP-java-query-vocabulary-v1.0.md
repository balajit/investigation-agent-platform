# Java Query Predicate Vocabulary v1.0 — CLOSED SPEC

Status: ACCEPTED for the Q0–Q4 query layer on `iap-java-query`. Every predicate
below has exact truth conditions. Adding a predicate requires amending this
document first; no ad-hoc predicates in `query.py`.

## Closed kind vocabulary (`ArgSketch.kind`)

| Kind | Meaning |
| --- | --- |
| `value:{int,long,float,double,char,string,boolean,null}` | Statically evaluated to a constant of that kind. Includes computed constants (`-5`, `1 + 2`): evaluation success, not syntactic literalness |
| `name` | Bare identifier reference. Not evaluable by the model |
| `complex` | Present, structured, but neither evaluable nor a bare name |
| `literal:undecodable` | Syntactically a literal the evaluator cannot decode |
| `limitation:{code}` | Evaluation hit a closed limitation code (see expressions evaluator) |

`shape` is the tree-sitter node type of the argument (open vocabulary tracking
the grammar version in `parser_version`). `operator` is set only for
`binary_expression` (the operator token), `unary_expression`, `ternary`, and
`cast`. `static_type` is the syntax-derived type where available, else the
evaluated-kind type for decoded literals, else `unknown`.

## Predicate truth table (argument predicates)

| Call shape | `arg_is_literal` | `arg_is_constant` | `arg_is_name` | `arg_is_complex` | `arg_has_limitation` |
| --- | --- | --- | --- | --- | --- |
| String/int/... literal | true | true | false | false | false |
| Computed constant (`-5`) | **false** | true | false | false | false |
| Bare name (`s`) | false | false | true | false | false |
| Complex (`s == null ? "d" : "e"`, `flag && true` when unevaluable) | false | false | false | true | false |
| Limitation case | false | false | false | depends | true |
| Missing index | false (None-safe) | false | false | false | false |

`arg_evaluates_to(kind, value)` requires the decoded literal record to match
both kind and value. Computed constants are intentionally excluded: use
`arg_is_constant` plus `text` inspection for those.

## Call/definition predicates

- `named` / `named_qualified`: exact string equality, no prefix/fuzzy matching.
- `with_arity` / `methods_with_arity`: exact count equality.
- `in_enclosing_package`: caller-side scope over the enclosing qualified name.
- `in_library`: callee-side attribution. Rule: same target name AND same arity
  AND definition qualified under the prefix. Residual: same-name/same-arity
  collisions across packages are admitted as ambiguous — the corpus, not the
  predicate, must rule them out.
- `candidate_targets` (uncertainty as data): every same-named definition is
  listed with a status, never silently dropped. Statuses: `MATCH` (exact
  arity, reason `exact-arity`; or varargs-compatible, reason
  `varargs-compatible` when `call.arity >= def.arity - 1`), or
  `ARITY_MISMATCH` (reason records expected vs definition arity). Each
  record carries `param_types` and the stable overload `key`
  (`name/arity(type,...)`): overloads distinguishable only by parameter
  types (e.g. `fmt(float)` vs `fmt(double)`) are distinct records, never
  duplicates. Consumers key on `key`; qualified name alone is not an
  identity. Callers filter; the predicate does not decide.
- `has_method_reference`: scoped to the invocation's own target/receiver text
  only. A method reference passed as an argument is tracked on the argument's
  `shape`, never on the enclosing call.
- `in_lambda`: lexical enclosure in a lambda body (any depth).
- `has_syntax_error`: model built over an erroneous window; records present
  are still well-formed, coverage is not guaranteed.

## Absence semantics (honest abstention)

No record ≠ negative claim. An empty `named(...)` result means "no matching
record in these windows," never "no such call exists." Predicates are total
over records; anything unrepresentable is a `limitation:*` kind or an absent
record, never a silent false.

## Outcome and Coverage Semantics

The query layer exposes records; resolver outcomes describe how much the
bounded analysis establishes. These states are not runtime claims.

| State | Normative meaning | Permitted inference |
| --- | --- | --- |
| `RESOLVED` | A matching in-window record satisfies the predicate's exact conditions and the required bounded evidence is present. | The stated declaration or shape is established for this window. |
| `PARTIAL` | A relevant record exists, but coverage or a required relationship is incomplete or explicitly limited. | The record and limitation may be surfaced; it must not be promoted to a complete positive claim. |
| `UNRESOLVED` | No authoritative matching record or safe decision is available in the analyzed scope. | None about global source absence or runtime behavior. |
| Missing record | The supplied model/windows contain no matching record. | Only that the record was not observed in the supplied scope. |
| Syntax-error window | Records may be present, but parser coverage is incomplete. | Never infer absence from the incomplete window. |
| `limitation:*` | A closed evaluator or analysis boundary prevented a stronger result. | Preserve the limitation; it is not a negative predicate result. |

Boolean predicates remain local record predicates. `false` means the supplied
record does not satisfy that predicate, or the requested index is absent; it
does not mean the source globally lacks the construct. A `PARTIAL` or
syntax-error model must not be treated as a complete negative result.

Candidate sets use the same rule: an empty `candidate_targets` result means no
same-named definition record exists in the supplied models, while multiple
records are surfaced without overload resolution. `in_library` may withhold a
call when attribution is ambiguous; it must not guess from a same-name match.

## Use-Def and Barrier Semantics

Effective-value states currently produced by the bounded builder are
intraprocedural and revision-pinned: `CONSTANT` means one supported source-level
value reaches the record, and `UNKNOWN` means no safe value is established.
Killed, ambiguous, aliased, and out-of-window transfer states are withdrawn
until a definition/use transfer engine produces and tests them. A computed
constant is distinct from a syntactic literal; existing `arg_is_constant` and
`arg_is_literal` semantics do not change.

Barrier records are source-level possibilities only: `GUARD`,
`REASSIGNMENT_KILL`, and `ALIAS_ESCAPE`. Validation-call and unknown-barrier
records are withdrawn until their producers and truth conditions exist. They
never establish execution, validation effectiveness, or runtime safety.
`flow_is_blocked` returns `BLOCKED`, `UNKNOWN`, or
`NOT_BLOCKED_IN_WINDOW`; the last value means only that no recognized blocker
was found in the supplied bounded records.

## Buckets (A7 closure)

- **In-model (queryable):** compilation unit package/imports; classes (5
  declaration kinds incl. annotation types, nesting); methods/constructors
  (name, arity, param types, return, overload key, varargs/override/default
  flags, nearest class); call sites (target, receiver, arity, per-arg
  sketch with shape/operator/static-type, lambda/method-ref flags,
   enclosing scope and method identity); decoded literals (kind, value, location);
   fields/locals (name, type, enclosing scope, static/instance,
   declared-type presence, raw initializer, evaluated kind/value/limitation);
   parameters (name, type, enclosing method, varargs flag); guard branches
   (class-qualified method identity, normalized guard text, per-return
   shape/value/raw text); dataflow bindings/events; conservative value states;
   source-level guard, alias, and reassignment barrier facts; all flags and
   per-arg shapes above. Dataflow events carry `lvalue`, `rvalue`, or
   `read_modify_write` roles; unknown roles remain explicit.
- **Model-field inventory:** `schema_version`, `parser_version`,
  `has_syntax_error`, `repository`, `revision`, `file_path`, `package`,
  `imports`, `classes`, `methods`, `calls`, `literals`, `fields`, `locals`,
  `params`, `guard_returns`, `dataflow_bindings`, `dataflow_events`,
  `value_states`, `barriers`, `type_declarations`, `analysis_snapshots`, and
  `analysis_limitation_codes` are model metadata or queryable records; their
  presence does not establish semantic attribution or runtime behavior.
- **Public query catalog:** `all_calls`, `all_methods`, `all_literals`,
  `named`, `named_qualified`, `with_arity`, `definitions_named`, `in_library`,
  `in_enclosing_package`, `arg_kind`, `arg_is_literal`, `arg_is_constant`,
  `arg_not_literal`, `arg_evaluates_to`, `takes_string_literal`,
  `definitions_in_package`, `calls_to_in_library`, `methods_with_arity`,
  `arg_shape`, `arg_operator`, `arg_static_type`, `arg_is_name`,
  `arg_is_complex`, `arg_has_limitation`, `calls_with_computed_arg`,
  `is_string_arg`, `is_int_arg`, `is_boolean_arg`, `is_null_arg`,
  `calls_with_receiver`, `calls_in_lambda`, `all_fields`, `all_locals`,
  `all_params`, `all_dataflow_bindings`, `all_dataflow_events`,
  `events_with_value_role`, `all_value_states`, `all_barriers`,
  `calls_with_effective_constant_arg`, `value_is_constant`,
  `value_is_propagated_constant`, `guards_for_method`, `barriers_for_method`,
  `reassignment_kills`, `flow_is_blocked`, `calls_with_validation_barrier`,
  `locals_named`, `params_named`, `guard_branches`,
  `guard_branches_for_method`, `all_type_parameters`, `type_parameters_for`,
  `all_generic_type_uses`, `generic_type_uses_for`, `type_uses_named`,
  `type_uses_wildcard`, `type_uses_diamond`, and `candidate_targets` are
  public query operations with local-record semantics.
- **Framework data:** boundary recognition is loaded from the versioned
  `iap/java-framework-boundaries/v1` catalog. Catalog entries are exact textual
  markers and are not runtime authority.
- **Matcher substrate:** declaration, package/import, and field matcher paths
  use the grammar-pinned `tree-sitter-query-v1` substrate. Context-sensitive
  scope and semantic-transfer walks remain explicit where capture-only matching
   cannot preserve their identity and abstention rules.
- **Generic syntax:** type-parameter declarations and generic type uses are
  source records with `UNRESOLVED` semantics. Wildcards preserve `?`,
  `? extends`, and `? super`; diamond preserves inference syntax. No record
  claims rawness, bounds validity, substitution, capture conversion, erasure,
  or compiler attribution.
- **Annotated (carried, not queryable):** source `text` snippets on args and
  the `key`/overload identity (display/diagnostic use, never proof).
- **Out-of-model (not established by this window model):** repository-wide or
  cross-window call edges; runtime behavior, scheduling, and visibility;
  framework/version authority; constructor or initialization ordering across
  declarations; whole-program aliasing and interprocedural use-def facts.
- **Deferred (named, each with its promoting question — the question whose
  "yes" moves the item into the model):** cross-window call graph ("all
  callers of X in this repo"); constructor-chain/init-order relations
  ("what initializes field F before use"); framework boundary facts as
  queryable entities ("which framework endpoints does this window expose?");
  generics/type arguments ("which declarations use parameterized type T<U>?");
  annotations as entities ("which methods carry annotation @A?");
  try/catch/exception flow ("which exceptions can escape method M?");
   lambdas beyond the enclosure flag ("which lambdas capture variable V?");
   use-def links ("where is local V assigned and read?"); comments/Javadoc
   ("which public methods lack a doc comment?"). Type declarations now carry
   typed analysis snapshots for version gates, records, sealed permits,
   generics, lambda capture/escape, method references, switch arms, and
   patterns. Snapshot features are declarative only; limitation codes remain
    explicit and no snapshot establishes execution. Concurrency/async marker
    abstention remains resolver-level because it is a window-level safety rule,
   not a declaration feature. Deferred promotion questions are tracked in
    `docs/IAP-java-query-items-1-3-plan-v1.0.md` and must be added here before
   a deferred capability becomes a public model record or predicate.
