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
  `ARITY_MISMATCH` (reason records expected vs definition arity). Callers
  filter; the predicate does not decide.
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

## Buckets (A7 closure)

- **In-model (queryable):** compilation unit package/imports; classes (4
  declaration kinds, nesting); methods/constructors (name, arity, param
  types, return, overload key, varargs/override/default flags); call sites
  (target, arity, receiver, lambda, method-ref flag, per-arg sketches);
  decoded literals; fields/locals (name, type, enclosing scope, initializer
  kind); all flags and per-arg shape/operator/type above.
- **Annotated (carried, not queryable):** source `text` snippets on args and
  the `key`/overload identity (display/diagnostic use, never proof).
- **Deferred (named, with promoting question):** cross-window call graph
  ("all callers of X in this repo"); constructor-chain/init-order relations
  ("what initializes field F before use"); framework boundary facts as
  queryable entities ("all Spring-mapped endpoints"); generics/type
  arguments; annotations as entities; try/catch/exception flow; lambdas
  beyond the enclosure flag; use-def links ("where is local v assigned");
  comments/Javadoc.
