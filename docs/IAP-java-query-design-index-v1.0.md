# Index: Java query + knowledge design family (consolidated)

Single entry point for the Java QL-style query layer, knowledge-bank, proxy, CodeQL, and
remediation designs on `iap-java-query`. Purpose: consolidate what exists, mark status
honestly, name what is missing, and order the family so a reader (or reviewer) knows
which document governs what. Repo-wide consolidation is explicitly out of scope — this
index covers only the design family produced by the 2026-10-28 sessions.

Convention: statuses are `PROPOSED` (awaiting owner mark), `ACCEPTED` (marked, governing),
`IN PROGRESS` (work underway, claims gated), `IMPLEMENTED` (landed with green gates).
No `IMPLEMENTED` claim stands while its gates are red (remediation-plan rule).

## Reading order

| # | Document | Status | Governs |
| --- | --- | --- | --- |
| 1 | `docs/session-20261028-java-q4-design-threads.md` | Record (this session's first half) | Q4 closeout, type-resolver agreement, remaining-work plans, model-store/webhook thread, knowledge-bank thread |
| 2 | `docs/IAP-java-query-vocabulary-v1.0.md` | `ACCEPTED` (Q0–Q4 layer) | Closed predicate semantics, outcome table, buckets (incl. `OUT_OF_MODEL`), deferred-promotion rule; enforced by contract tests |
| 3 | `docs/IAP-java-query-items-1-3-plan-v1.0.md` | `IMPLEMENTED` (work done, gates re-verified in remediation) | Item 1 dataflow migration, Item 2 vocab semantics, Item 3 A7 boundary |
| 4 | `docs/IAP-java-query-items-6-7-plan-v1.0.md` | `IMPLEMENTED` (ditto) | Item 6 use-def/constant propagation, Item 7 guard/barrier predicates |
| 5 | `docs/IAP-java-query-remediation-plan-v1.0.md` | `IN PROGRESS` | Recovery from the `4ae33d5` review: A1 fail-open, contract decisions, A2–A7 fixes, dead-state decision, harness/contract/lint gates |
| 6 | `docs/IAP-java-generics-design-v1.0.md` | `IN PROGRESS` (2 tests red at last check) | Syntax-level generics model (engineer track; separate from remediation) |
| 7 | `docs/IAP-java-semantic-attribution-design-v1.0.md` | `DESIGN` (no deployment/dependency auth) | JDT-sidecar semantic attribution architecture; Tree-sitter stays the syntax authority |
| 8 | `docs/IAP-knowledge-bank-component-spec-v1.0.md` | `PROPOSED` | Generic raw→knowledge conversion component (adapters + core + serve); awaits owner answers on scope/converter/organizing-axis |
| 9 | `docs/IAP-java-proxy-canonicalization-design-v1.0.md` | `PROPOSED` | Proxy-frame canonicalization (JDK + Spring CGLIB, then Hibernate); canonical+raw, matching-keys-never-proof |
| 10 | `docs/IAP-codeql-gap-analysis-v1.0.md` | Survey record + `PROPOSED` adoption order | What CodeQL has/doesn't (proxies: verified absence), ranked IAP adoptions D2–D7, explicit non-adoptions |

## Missing pieces (named, not hidden)

- **Items 4 and 5 are phantoms.** Referenced by numbering, defined nowhere — not in either
  items plan, not in the ledger, not in code. Either define them or remove the references;
  numbering must not point at empty space (remediation plan Phase 6 requires this).
- **Items 8, 9, 10 shipped without plan docs**, breaking the 1-3/6-7 pattern. Retrospective
  one-pagers or an explicit waiver; the boundary catalog (Item 8) at least has its loader,
  version gate, and wheel test as implementation evidence.
- **The A6 pruning mandate vs legacy parity is unresolved.** The remediation plan requires
  not descending into nested methods; the frozen legacy oracle does descend, and parity is
  the higher gate. Current code follows legacy (with span-dedupe against double-recording).
  This needs an explicit decision, not silent drift — recorded here until the plan is amended.
- **Occurrence-unique method identity is an open follow-up.** Same-signature nested methods
  (e.g. anonymous-class `run()` inside `run()`) share one identity string. Harmless today
  (dedupe keeps records single; joins stay consistent), but any cross-method accounting
  built on identity inherits the collision. Deliberately not done in the dedupe fix (blast
  radius: GuardBranch identity contract, joins, tests).

## Open owner questions (consolidated from the family)

1. Q4 tail scope: dataflow only vs all five resolvers; push timing; drift-pin venue.
2. Model store: retention policy, branch scope, schema scope, placement.
3. Knowledge bank: per-investigation vs cross-investigation scope, "accessed" definition,
   deterministic vs LLM vs hybrid converter, organizing axis + first reader, contradiction
   policy, sharing rule, phasing vs invalidation.
4. Proxy design: family scope (Hibernate order confirmed; ByteBuddy/Mockito later?),
   intake vs Java-only placement, scaffolding drop-vs-flag default, priority vs red-tree work.
5. CodeQL adoptions: D2–D7 order accepted? Whole-program epic authorized as a design track?
6. Semantic attribution: JDT-sidecar direction accepted? (No implementation auth implied.)
7. Ledger/numbering hygiene: items 4–5 defined or removed; `4ae33d5` stays a checkpoint
   (not "Complete") until remediation gates pass.

## Gate state referenced by this index (2026-10-28, verified this session)

- Unit suite: 4148 passed, 2 known pre-existing pins (`test_range_read_is_labeled_range`,
  `test_forced_range_read_reconciles_unresolved_edge_with_occ_retry`).
- Focused Java track (dataflow, query, contract, substrate, items 8/9, fixture battery,
  flow, generics, field facts, methods, method returns, locals): 161 passed.
- Explicit parity harness (dataflow legacy-vs-model + type legacy-vs-model + full fixture
  battery): 20 passed.
- `ruff check` on changed Java/Python/test files: clean. Repo-wide `ruff check`/`format`
  gates are red at HEAD from pre-existing `tests/fixtures` intentional-bad-code files and
  pre-existing style drift — independent of this track, reported for the record.
- `git diff --check`: clean. Wheel contains `framework_boundaries_v1.json` (build-verified).
- Model-build timing baseline (representative, no warmup): ~0.2 KiB → ~2.5 ms;
  ~1.0 KiB → ~2.6 ms; ~17.6 KiB synthetic (100 classes) → ~39 ms (≈2.2 ms/KiB, ~linear).
