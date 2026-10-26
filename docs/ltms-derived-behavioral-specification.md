# LTMS Derived Behavioral Specification

**Status:** proposed, reconstructed contract; not an upstream normative specification.  
**Target revision:** `738161f2bdc2a938d97c40f974f51766f45d4c0c`.  
**Purpose:** specify observable contracts and unresolved semantics independently of the current implementation, so a future implementation or integration can be evaluated without silently elevating quirks into requirements.

## 1. Conformance labels and requirement categories

Every requirement has one or more labels:

- **[Theory]** grounded in established logical/TMS concepts.
- **[Intent]** required by repository docs/plan.
- **[Observed]** confirmed in source and supported by inspected tests where stated.
- **[Proposed]** clarification recommended for deterministic safe behavior.
- **[Unresolved]** requires owner/design decision; consumers must not assume an answer.

The tests cited below were inspected and subsequently executed with `uv` after the initial environment limitation was identified. At revision `738161f2bdc2a938d97c40f974f51766f45d4c0c`, `uv run --extra dev --extra sat pytest -ra -o addopts=''` passed **169 tests in 1.17s** under CPython 3.13.15. Tests establish the asserted cases only; they are not a proof of general correctness.

## 2. Core concepts and supported logical language

### 2.1 Terms

**[Observed]** A term is an atom (`str`, `int`, finite `float`), a `Var`, or a recursively nested tuple. A proposition is a term treated as a fact; compounds are tuples. Variables with equal names compare equal. A binding environment is a partial map `Var → Term`.

**[Intent]** Unification returns either a binding environment or a unique failure sentinel. The empty environment is a valid success. Unification rejects a binding where the variable occurs in the resolved term. Variables are not standardized apart; call sites must supply appropriately distinct variable names/environments.

**[Proposed]** Public APIs accepting a raw binding map should either validate it is acyclic or document that cyclic user-created maps are invalid; `substitute` may otherwise not terminate.

### 2.2 Propositional formulas

**[Observed]** The normalizer accepts node leaves and connective tuples:

| Connective | Contract |
|---|---|
| `not(f)` | Exactly one operand; flips each literal sign. |
| `and(f...)` | All operands; empty conjunction is true / zero clauses. |
| `or(f...)` | All operands; empty disjunction is false / one empty clause. |
| `implies(a,b)` | Exactly two operands; equivalent to `¬a ∨ b`. |
| `iff(a,b)` | Exactly two operands; equivalent to both implications. |
| `taxonomy(f...)` | Nonempty; exactly one operand true (at least one plus pairwise not-both). |

The formula language is propositional once LTRE translates simple propositions into nodes. It is not first-order logic over variables in clauses; variables participate in rule matching before propositional assertion.

## 3. Rules, data presence, and belief

**[Observed]** A fact/datum may exist with node label UNKNOWN. `INTERN` triggers on datum creation/presence. `TRUE` and `FALSE` triggers depend on the LTMS label. `run_rules` drains a LIFO queue of Python callbacks and bindings.

**[Intent]** TRE matching is incremental in both arrival orders: a new fact is matched against registered rules, and a new rule is tested against existing facts. Duplicate fact insertion is suppressed structurally.

**[Proposed]** Rule APIs must state whether a trigger is a one-shot match activation or an event on every transition into a label. If callbacks are one-shot, state it in API docs. If transition-based, maintain/re-arm activation registrations. In either case, callback effects are not implicitly logical support and must be separately modeled if they need retraction.

**[Unresolved]** Current source clears parked activations at first matching transition and does not re-arm them; whether this matches intent is undecided.

## 4. Belief-state semantics

### 4.1 JTMS

**[Theory/Intent]** JTMS labels are `IN` and `OUT`; OUT means currently unsupported/not derivable, not false. A justification is a definite Horn implication from antecedent nodes to a consequence. A node records one current support and can have multiple candidate justifications. Retraction requires a two-phase operation: withdraw nodes whose current support depends on the retracted node, then search for an alternative supported justification.

### 4.2 LTMS

**[Theory/Intent]** Each proposition node has one of `UNKNOWN`, `TRUE`, `FALSE`; each clause is a disjunction of signed literals. A literal `(n, TRUE)` is satisfied when node `n` is TRUE; `(n, FALSE)` is satisfied when `n` is FALSE.

For clause `C`, define:

`pvs(C) = |{ l ∈ C : l is UNKNOWN or satisfied }|`  
`sats(C) = |{ l ∈ C : l is satisfied }|`

Then **violated** iff `pvs=0`, **unit-open** iff `pvs=1`, and **satisfied** iff `sats>0`. If unit-open, the remaining non-false literal must be UNKNOWN and is forced to its sign. The selected support for that node is the clause that forced it. Contradictions are dispatched after propagation settles.

**[Intent]** BCP is sound but incomplete: an entailed literal or an unsatisfiable theory may remain UNKNOWN without a detected conflict. UNKNOWN is lack of current derivation, not false.

**[Proposed]** All public mutations should preserve clause counter equivalence to recomputation and ensure support points to an enabled assumption or a currently forcing clause. Direct user mutation of internal node/clause fields is outside the contract.

## 5. Assumption lifecycle

An assumption is a node permitted to receive an explicit label with support sentinel `ENABLED_ASSUMPTION`.

| Transition | Required precondition | Required result |
|---|---|---|
| Enable unknown node | Node is marked assumption; requested label is TRUE or FALSE. | Set requested label and assumption support; propagate BCP and dispatch contradictions. |
| Enable same known value | Node is eligible assumption. | Mark assumption support as authoritative for the same label. |
| Enable opposite known value | Node already has opposite known label. | Reject with explicit conflict error; do not corrupt labels/counters. |
| Retract enabled assumption | Node's selected support is assumption sentinel. | Phase 1 withdraw it and all support-dependent labels; phase 2 rederive from alternative clauses; settle and check contradictions. |
| Temporary assumption context exit | Context installed assumption(s). | Retract only assumptions created/owned by this context, including exceptional exit. |

**[Observed]** `LTMS.with_assumptions` makes a best-effort exception-safe cleanup, while LTRE uses `Datum.assumption` as its informant owner. **[Proposed]** Ownership is exclusive unless an explicit multi-owner reference-counted contract is introduced. Same-owner repeated entry must not accidentally let an inner scope retract an outer scope's assumption. Compound and simple assumptions must follow one ownership rule.

## 6. Clause/formula operations

**[Observed]** Clause addition simplifies duplicate literals, drops tautologies, registers the clause, updates counters and propagates unless marked internal. `add_formula` converts the complete input formula to CNF before installing the resulting clauses. Negated propositions reuse the same node with the opposite sign.

**[Proposed]** Formula validation should complete before mutation. If any operation after clause installation fails, the public API must either roll back all mutations or explicitly document non-atomic partial mutation. This proposed transaction contract is not established by current source.

## 7. Contradiction semantics

**[Intent/Observed]** When a clause becomes violated, it is recorded. At top-level settlement, handlers are invoked newest-first. A handler returns truthy only if it resolved the supplied current contradictions. On a truthy result, propagation resumes; if no handler resolves the conflict, `LTMSContradiction` is raised.

**[Proposed]** A handler that temporarily changes assumptions must also remove or invalidate pending rule activations whose triggering support was withdrawn, unless those activations are explicitly durable events. Handler stack entries must always be removed on return or exception.

## 8. Support and explanation semantics

**[Observed]** A node has one selected current support: assumption sentinel, a forcing clause, or no support/unknown. Explanation traverses the selected clause's other nodes before the conclusion and includes informant metadata.

**[Proposed]** Interpret explanations as “why this current label has its recorded support,” not “all reasons this claim is true.” Explanations are not probability, causal proof, or audit-grade evidence certificates. If downstream use requires alternate derivations, cycle proofs, or durable provenance, model those explicitly.

## 9. Closed-world and indirect proof semantics

### 9.1 Closed-world assumption

**[Observed]** `close_predicate` assumes FALSE for currently interned UNKNOWN datums in the named bucket; it does nothing for an uninterned predicate and does not automatically cover future instances. The context manager withdraws the forms it recorded.

**[Proposed]** Closure should own only assumptions newly created by that closure scope; pre-existing assumptions should remain intact. Current source records forms when `assume` returns without checking whether the node remained UNKNOWN after that call. In normal state, an already-owned active assumption is known and skipped; a collision requires an owner marker to persist while the LTMS node becomes UNKNOWN. This is an unresolved/conditional hazard pending a reachable reproducer. A complete absence conclusion requires a closed, scoped, successful data collection boundary, which LTMS itself does not define.

### 9.2 Indirect proof

**[Observed]** `try_indirect_proof(f)` assumes the opposite label, propagates and runs rules; a contradiction whose support depends on that temporary assumption causes retraction plus nogood installation. The function returns whether `f` is then TRUE. If the opposite assumption survives, it is retracted and proof returns false.

**[Proposed]** Refutation must be isolated: queued callbacks produced only by the temporary assumption cannot run after it has been retracted, unless their execution is deliberately retained as a durable event and its semantics are specified. Current implementation fails this requirement (FIND-LTMS-001).

## 10. Dependency-directed search

**[Observed]** Search takes a sequence of choice sets, tries candidates depth-first, skips known FALSE candidates, descends immediately for a known TRUE candidate, and learns a nogood from assumptions implicated in a contradiction. The caller-provided `extract` function observes each solution.

**[Unresolved]** The API does not state whether a choice set implies exactly-one or at-most-one, and the search code does not itself install such constraints. Proposed requirement: callers must supply constraints for exclusivity/cardinality and must avoid mutating external state from `extract` in a way that contaminates sibling branches.

## 11. Optional completeness and watched-literal semantics

### 11.1 CLTMS

**[Intent/Observed]** `complete()` computes a subsumption-minimal prime-implicate saturation of clauses present at call time, adds missing clauses and runs BCP. The intended guarantee is complete unit propagation/refutation for that fixed propositional clause set, subject to correct consensus/subsumption implementation. Worst-case growth is exponential. It does not establish first-order rule completeness.

**[Proposed]** API docs must specify that future clause changes require recompletion; until then, do not label a mutated theory complete. Independent SAT-oracle tests are recommended before relying on the guarantee.

### 11.2 WatchedLTMS

**[Observed]** WatchedLTMS is an alternative BCP implementation over clauses with assumptions/retraction and explanations. It is not the backend used by LTRE/CWA/DDS. Existing tests compare it to counter LTMS on generated clause additions/forward assumptions and check soundness against MiniSat.

**[Proposed]** Conformance requires parity for labels, contradictions, supports, assumption/retraction sequences and handlers, plus independent SAT soundness. Current checked-in randomized tests do not cover all these sequences.

## 12. Error, exception, and mutation semantics

**[Observed]** Parsing malformed expressions and invalid connective arity raises `ValueError`; a failed DSL `expect` raises `AssertionError`; unresolved LTMS contradictions raise `LTMSContradiction`; invalid conflicting assumptions raise `ValueError`; file errors propagate from `Path.read_text`.

**[Unresolved]** Queue behavior when a callback raises, atomicity of `load_kb`, and atomicity of multi-clause formula installation are not fully specified.

**[Proposed]** Parsing and validation errors should identify line/column or directive; unknown/trailing directive arguments should be rejected. `load_kb` should choose one explicit contract: transactional load into an existing engine, or clearly documented incremental execution with partial state on failure. Never silently turn malformed directive text into a proposition assertion.

## 13. Complexity and completeness limitations

- Unification and substitution scale with term size and binding dereferences; no pathological-input bounds are specified.
- Rule matching is bucketed by leftmost symbol but may still scan every fact in a bucket; callback recursion can generate unbounded work.
- BCP is incremental over clause counters but incomplete for general CNF.
- CNF disjunction cross-products may grow exponentially.
- Prime implicate generation can be exponential in clause count/variables.
- Watched literals reduce clause rescans in suitable workloads, but no measured package benchmark was reviewed.
- CWA only closes known finite instances; it is not domain closure over arbitrary tuples.
- No temporal, probabilistic, causal, distributed, persistence, security/tenant, or resource-budget semantics are included.

## 14. Public API contracts and requirement traceability

| Requirement ID | Requirement | Category | Implementation / inspected test evidence | Status |
|---|---|---|---|---|
| R-LTMS-001 | `unify` distinguishes `{}` success from `FAIL`. | Intent, observed | `unify.py::unify`; `test_unify.py`. | Confirmed in source/tests; full suite passed 169 tests. |
| R-LTMS-002 | Occurs-check rejects cyclic term binding generated by unification. | Theory, intent, observed | `unify.py::_occurs`; `test_unify.py`, property test. | Confirmed for tested inputs. |
| R-LTMS-003 | TRE fact/rule insertion order does not lose matches. | Intent, observed | `tre/engine.py`; `test_tre.py::test_order_independence_rule_after_facts`. | Confirmed for tested case. |
| R-LTMS-004 | BCP maintains clause counters and does not claim completeness. | Intent, observed | `core.py`; `test_core_properties.py`, `test_core_bcp.py`. | Tests inspected; full suite passed 169 tests. |
| R-LTMS-005 | Retraction is two-phase and preserves independent alternative support. | Theory, intent, observed | `core.py::_propagate_unknownness`; `test_core_assumptions.py`. | Representative tests inspected; full suite passed 169 tests. |
| R-LTMS-006 | Belief labels propagate to rule triggers; datum existence remains distinct. | Intent, observed | `ltre.py`, `core.py::set_truth`; `test_ltre.py`. | Initial firing tested; repeat lifecycle unresolved. |
| R-LTMS-007 | Contradiction handlers compose newest-first and clean up on exit. | Intent, observed | `core.py::with_contradiction_handler`; limited tests. | Partial. |
| R-LTMS-008 | Explanation reflects selected support and current label only. | Proposed clarification | `explain.py`; `test_explain.py`. | Semantics should be documented. |
| R-LTMS-009 | Compound assumptions obey same ownership conflict behavior as simple assumptions. | Proposed | `ltre.py::assume`; missing test. | Not satisfied. |
| R-LTMS-010 | Indirect proof discards activations made stale by retraction. | Proposed | `indirect.py`, `ltre.py::run_rules`; no test. | Not satisfied. |
| R-LTMS-011 | CWA scope should not retract a pre-existing assumption it did not create. | Proposed | `cwa.py`, `ltre.py::assume/retract`; no collision test. | Conditional hazard; requires reproducing stale datum ownership with UNKNOWN node. |
| R-LTMS-012 | Malformed DSL directives are rejected and load mutation semantics are explicit. | Proposed | `dsl.py::load_kb`; `test_dsl.py` lacks edge cases. | Not satisfied/underspecified. |
| R-LTMS-013 | Completion guarantee is scoped to a fixed clause set and recomputation is explicit. | Intent, proposed clarification | `cltms.py::complete`; `test_cltms.py`. | Current behavior fixed-set only. |
| R-LTMS-014 | Watched engine parity includes retraction/handler sequences. | Proposed | `watched.py`; `test_watched.py`. | Full suite passed 169 tests; randomized retraction/handler coverage remains incomplete. |

## 15. Conformance examples

### 15.1 Empty success vs failure

Given identical ground terms `p(a)` and `p(a)`, `unify` returns `{}`. Given `p(a)` and `p(b)`, it returns `FAIL`. A consumer MUST compare to `FAIL`, never test truthiness.

### 15.2 Unit propagation and incompleteness

Let `C1 = (¬p ∨ q)` and `C2 = p`. The expected BCP state is `p=TRUE, q=TRUE`. For `C3=(x∨¬y), C4=(x∨y)`, expected base BCP state is `x=UNKNOWN, y=UNKNOWN` despite entailment of `x`; a complete solver may derive more, but this LTMS layer must not be described as complete.

### 15.3 Retraction with alternate support

Let `a→c`, `b→c`, with assumptions `a=TRUE` and `b=TRUE`. Retracting `a` leaves `c=TRUE` supported by `b`; retracting `b` then leaves `c=UNKNOWN` unless another independent clause supports it.

### 15.4 Presence vs belief

Intern datum `p(a)` without assertion. An INTERN trigger may fire; a TRUE trigger must remain parked/not fire while the node is UNKNOWN. Later asserting `p(a)` may activate the TRUE trigger. Repeated transition behavior is unresolved and must not be inferred from this one-transition example.

### 15.5 DSL failure contract

For input with a valid assertion followed by an invalid directive, the implementation currently preserves the first assertion. A future conforming contract must explicitly choose transactional rollback or documented incremental semantics; tests must assert the selected behavior. A `query` directive followed by a tab and expression, and `complete extra`, should be rejected rather than silently reinterpreted/ignored under the proposed strict parser contract. The 169-test suite passed, but does not currently assert these edge cases.

## 16. Explicit non-goals

This specification does not define a probabilistic model, causal inference semantics, temporal logic, a complete first-order reasoner, an ATMS, a CDCL SAT solver, durable storage, distributed execution, tenant isolation, a hostile-input sandbox, a stable cross-version wire format, or IAP adoption.

## 17. Open questions

1. Is trigger activation one-shot or transition-based? How should callbacks be handled when their premise is retracted before execution?
2. Is compound assumption owner conflict a bug, and is informant identity exclusive or shareable?
3. Should `closed_world` track only newly created assumptions rather than every fact it touched?
4. Should indirect proof restore the pending queue exactly or discard activations from the failed branch?
5. What are `dd_search` choice-set cardinality requirements, and is `extract` required to be pure?
6. Should `.kb` load be atomic or intentionally incremental?
7. What happens on callback exception: retain later queue items, roll back effects, or abort the engine state?
8. What revalidation/recompletion is required after clause changes in CLTMS mode?
9. Is one selected support adequate for intended explanation use, or must alternative supports be enumerable?
10. Which invariant tests should be expanded to randomized retraction sequences and SAT differential checks?
