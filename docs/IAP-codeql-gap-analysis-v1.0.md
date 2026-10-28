# Design: CodeQL Java capability gap analysis (and IAP adoption order)

Read-only survey of the local CodeQL repository (`~/workspace/repos/opensource/codeql/codeql`,
`java/` tree, `.qll`/`.ql` plus `ext/*.model.yml`) against IAP's Java source-query layer.
Two questions: what does CodeQL have that IAP lacks, and is IAP behind on proxies (the
thread that prompted this review). Answer to the second: no — verified absence on both
sides. What follows is the ranked adoption list, what IAP should pointedly not copy, and
the placement of each item relative to the window-scoped model and the planned
repo-scoped model store.

Status: **Survey record + adoption proposal.** Framework-fact claims below are file- and
line-cited so they can be re-verified against the pinned CodeQL revision. Adoption items
are ordered proposals, not authorizations.

## Context

IAP's Java analysis is a Tree-sitter, window-scoped, fail-closed query layer: per-window
model records, limitation codes, parity harnesses, no whole-program facts. CodeQL is the
reference semantic analyzer: whole-program database, CHA-style dispatch, dataflow/taint
framework, Models-as-Data external summaries. The survey covered proxy/Spring/Hibernate
modeling, dispatch machinery, reflection handling, lambda desugaring, generated code, and
taint configuration — full detail per area is in the conversation record
(`docs/session-20261028-java-q4-design-threads.md` follow-ups and the 2026-10-28 review
thread); this document keeps the durable conclusions.

No `docs/dev/architecture.md` exists in the project; architecture changes are recorded
against `docs/IAP-architecture-v1.0.md` (see Documentation Plan).

## References

- Local CodeQL tree: `~/workspace/repos/opensource/codeql/codeql/java/` — `ql/lib/semmle/code/java/dispatch/` (`VirtualDispatch.qll`, `DispatchFlow.qll`), `ql/lib/semmle/code/java/Reflection.qll`, `ql/lib/semmle/code/java/dataflow/` (`DataFlow.qll`, `FlowSteps.qll`, `ExternalFlow.qll`), `ql/lib/ext/*.model.yml` (Models-as-Data), `ql/lib/semmle/code/java/GeneratedFiles.qll`, `ql/lib/semmle/code/java/frameworks/` (Spring, Hibernate — noted for absence).
- `docs/IAP-java-proxy-canonicalization-design-v1.0.md` — The proxy response; CodeQL offers no prior art (verified absence), so that design is greenfield.
- `docs/IAP-knowledge-bank-component-spec-v1.0.md` — Generic conversion layer; MaD-style externalization (item 4 below) is its future catalog mechanism.
- `docs/IAP-java-query-vocabulary-v1.0.md` — Closed semantics that adoption items must extend, not bypass.

## Goals / Non-Goals

**Goals:**

- Record exactly what CodeQL knows about proxies/Spring/Hibernate (almost nothing — kill the assumption).
- Rank what IAP should adopt, ordered by window-compatibility (no whole-program dependency first).
- Name what IAP must not copy (silent narrowing, taint cuts without reasons).
- Place each adoption item against the window-scoped model vs the planned repo-scoped model store.

**Non-Goals:**

- No CodeQL dependency, query port, or extractor reuse (survey only; IAP stays Tree-sitter-native).
- No whole-program database design here (that is its own future epic; items below say which parts need it).
- No proxy modeling duplication (covered by the canonicalization design, not here).

## Decisions

### D1: Proxy/Spring/Hibernate — verified absence, IAP leads

**Decision:** Record as fact: CodeQL has no JDK-proxy call edge (one auto-generated taint
step on `InvocationHandler.invoke` plus explicit neutral/no-flow entries for
`newProxyInstance`), zero CGLIB/ByteBuddy references, a 24-line Hibernate file of type
wrappers feeding SQL-injection sinks only, and `@Aspect` methods present solely as
dead-code liveness. IAP's canonicalization plan is therefore novel work, and IAP's
limitation-code vocabulary already exceeds CodeQL's honesty machinery (which narrows
silently or cuts taint without reasons).

**Alternative considered:** Treating CodeQL as the proxy authority and porting its approach.
Rejected — there is nothing to port; porting the neutral-model entries would mean copying
IAP's abstention but without the reason codes.

---

### D2: Adopt literal-based reflection inference (window-compatible)

**Decision:** Highest-value portable item. `Reflection.qll` resolves `Class.forName("a.B")`
with a string literal to the class and `getMethod("name")` + arity to the method, feeding
liveness. IAP currently abstains on the markers without harvesting the adjacent literals.
Adopt the literals-only rule as named model facts (class/factory target candidates),
keeping the abstention for everything non-literal. No whole-program data required.

**Alternative considered:** Full reflective-call dispatch edges. Rejected — needs the
program database; literals-only captures the majority of real-world value at window scope.

---

### D3: Adopt callback-through-parameter resolution (window-compatible)

**Decision:** `WrappedInvocation.qll` resolves callbacks passed as functional-interface
arguments, including a heuristic `Argument[pos] → Argument[pos].Parameter[-1]` form. IAP
records `method_argument` events and alias-escape barriers but never resolves the target —
even the syntactic case (`runner(() -> x)`) with the lambda body in-window. Adopt the
heuristic form first (in-window lambda bodies only), whole-program form later with the
model store.

**Alternative considered:** Full interprocedural callback graph now. Rejected — premature
without the store; the heuristic form is self-contained and testable per window.

---

### D4: Generalize Models-as-Data catalogs

**Decision:** CodeQL's `ext/*.model.yml` (sources/sinks/summaries/neutrals) is the mechanism
for framework knowledge without code changes. IAP just invented the narrow form
(`framework_boundaries_v1.json`, schema-versioned). Generalize the pattern: versioned
catalogs for dataflow summaries (`Argument[i] → ReturnValue`), neutral/no-flow entries,
and source/sink rows, starting with the frameworks already in the boundary catalog.
The knowledge-bank spec's future catalog mechanism should be this same shape.

**Alternative considered:** Hardcoding each new framework fact in code. Rejected — it
repeats the pre-catalog pattern the boundary work just retired, and blocks non-code
contributors from extending framework knowledge.

---

### D5: Adopt generated-code classification

**Decision:** `GeneratedFiles.qll` labels `@Generated`/ANTLR/marker-comment files so queries
can exclude them. IAP's boundary catalog recognizes the same markers but only to abstain.
Add a `generated_source` model flag (queryable, exclusion-capable) instead of an
abstention. Window-local, no dependencies. Directly supports the proxy thread: generated
frames (`$Proxy`, CGLIB hashes, `GeneratedMethodAccessor`) are a subclass of this problem.

**Alternative considered:** Keep abstaining on generated markers. Rejected — abstention
throws away queryable signal (generated code is still readable source with exact shapes).

---

### D6: Adopt the taint-configuration vocabulary (engine later)

**Decision:** CodeQL's per-query `ConfigSig` (`isSource`/`isSink`/`isBarrier`/
`isAdditionalFlowStep`), global sanitizers, and taint-inheriting containers are the right
*shape* for IAP's future flow engine. Adopt the vocabulary now as record kinds (source /
sink / sanitizer / barrier records); build the engine when whole-program flow exists. IAP's
`BarrierFact`s become inputs to this config rather than a parallel concept.

**Alternative considered:** Building a flow engine first. Rejected — vocabulary-first lets
queries, fixtures, and the closed spec converge before the expensive part.

---

### D7: Defer generics-unification pruning until post-whole-program types

**Decision:** `Unification::failsUnification` prunes dispatch on generics mismatch — the
payoff of IAP's new syntax-level generics model. Explicitly deferred: pruning needs
whole-program subtype facts to be sound. Record the dependency so the generics track
doesn't drift toward unsound local pruning.

**Alternative considered:** Local generics-based pruning now. Rejected — window-local
pruning of overload targets risks the exact false-negative class the parity harnesses
exist to prevent.

---

### D8: Do not copy silent narrowing or reason-free cuts

**Decision:** Non-adoption as a decision. CodeQL's low-confidence gates narrow dispatch
without telling the querier, and `neutralModel` cuts taint without a reason artifact.
IAP's limitation codes, PARTIAL-with-reason results, and parity harnesses are load-bearing
differentiators (the `dfc-generated` taint step on `InvocationHandler.invoke` is exactly
the unreviewed noise an IAP harness would catch). Any adopted mechanism keeps IAP's
reason-attached semantics; where CodeQL is silent, IAP stays explicit even at the cost
of more verbose results.

## Data Storage

No new persistence in this design. D4 catalogs are versioned JSON resources alongside
`framework_boundaries_v1.json` (same loader discipline: schema-version gate, fail-closed
validation, wheel-inclusion test). D6 record kinds extend existing model records when
their engine phase lands.

## Data Structures

No new runtime structures. The durable artifacts are the D4 catalog schemas (to be
specified when the first summary catalog ships) and the D6 vocabulary additions to
`IAP-java-query-vocabulary-v1.0.md` (source/sink/sanitizer as named, deferred-until-engine
record kinds — vocabulary-only, no producers yet, mirroring the withdrawn-states precedent).

## Interfaces

No new user-facing or code interfaces. The adoption surface is: new query predicates
(D2 literal targets, D3 callback targets, D5 `is_generated` predicates) following the
existing `query.py` conventions and contract-test gates (every public predicate documented
or the suite fails), plus catalog file formats (D4).

## Implementation Detail

Ordered, each independently shippable against the window model:

1. D5 generated-source flag (marker-comment + `@Generated` + ANTLR-subtype detection as
   record flags; exclusion predicates; fixtures).
2. D2 literal reflection harvesting (`Class.forName("a.B")` → class fact;
   `getMethod("name")` + arity → method fact; literals-only rule with tests proving
   non-literals still abstain).
3. D4 first summary catalog (dataflow summaries for the boundary-catalog frameworks;
   loader + version gate + wheel test, mirroring Item 8).
4. D3 in-window callback resolution (heuristic form; nested-lambda fixtures).
5. D6 vocabulary kinds (spec-only; no producers until the flow engine phase).
6. Whole-program database epic (unlocks D7 and the full forms of D2/D3) — separate design,
   referenced here, not contained here.

## Migrations

D5 → D2 → D4 → D3 → D6(vocab) → whole-program epic. No step changes existing resolver
contracts; each adds records/predicates under the contract-test gate. Rollback per step is
deletion of the additive records.

## Testing Philosophy

### Literal reflection harvesting

Literals resolve; every non-literal (concatenation, parameter, field) abstains with the
existing limitation code. Fixtures pair each literal shape with its non-literal twin.

### Callback resolution

In-window lambda bodies resolve; out-of-window or non-lambda arguments abstain. Heuristic
form never crosses method boundaries; tests assert the boundary.

### Catalog discipline

Every catalog ships schema-versioned with a loader validation test, a wheel-inclusion
test (the Item 8 precedent), and a marker-exactness test against the previously hardcoded
set when migrating an existing inline table.

### No-silent-narrowing

Any adopted pruning (D7 future) ships with the kept-candidate list visible in test
outputs: tests assert not only the selected target but the explicitly excluded set with
reasons, mirroring the candidate-set semantics already in the vocabulary.

## Documentation Plan

### `docs/IAP-codeql-gap-analysis-v1.0.md` (this file)

**Audience:** Architects and the chief.

Durable record of what was surveyed (with file paths for re-verification), what IAP adopts
in what order, and what it deliberately does not copy.

### `docs/IAP-architecture-v1.0.md` changes

**Audience:** Architects and future maintainers.

When D5/D2/D4 land: note the generated-source flag, literal-reflection facts, and the
catalog family in the Java analysis section. No structural diagram change (all items are
window-local additions).

### `docs/IAP-java-query-vocabulary-v1.0.md` changes

**Audience:** Query authors.

D5 predicates and D2 literal-target facts documented on landing (contract test enforces);
D6 kinds added as named deferred records without producers, following the withdrawn-states
precedent — documented as not-yet-produced, never silently producible.

## Risks / Trade-offs

### Survey staleness

**Risk:** The local CodeQL checkout advances; file paths and predicates cited here drift.

**Mitigation:** Citations include the mechanism, not just the path; re-survey is scoped to
the cited files. Pin the surveyed revision in the index (consolidation doc).

### Literal harvesting overreach

**Risk:** `Class.forName` with a constant expression (not a literal) tempts constant
folding, sliding from literals-only into unsound inference.

**Mitigation:** The rule is syntactic literal or abstain — no folding, no propagation;
the non-literal twin fixtures enforce it permanently.

### Catalog sprawl

**Risk:** Each new catalog (summaries, sources, sinks) becomes its own loader, version
scheme, and test pattern.

**Mitigation:** One loader discipline (copied from the boundary catalog: schema-version
gate, fail-closed validation, wheel-inclusion test); new catalogs reuse it verbatim.
