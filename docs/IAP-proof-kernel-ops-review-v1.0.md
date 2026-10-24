# IAP Proof-Kernel Migration, Operations, Review, and Readiness v1.0

## 6. Workflow, planner, witnesses (Parts 6–7)

- Obligation → action policy (pure, in `application/investigation/`): inputs claims + history + capabilities + budgets; outputs one valid action + rationale + expected discriminator + bounded cost. LLM may rank already-admissible actions only; deterministic policy validates before execution. Track workflow / evidence / proof progress separately; successful fetch with duplicates leaves obligation `OPEN`; exhaustion ends in typed abstention with missing codes.
- Shadow-only: no side effects, no execution, no authoritative-state mutation; compare choice, predicted discriminator, evidence novelty, proof progress, cost, terminal outcome; Temporal replay-safe.
- Witness pilots (Stage E, deferred): generic path-witness record (nodes/edges, roles, order, completeness, truncation, source revision, analysis config, limitations); solver manifest (encoded predicate, assumptions, omissions, status, model mapping, independent check). `UNKNOWN`/timeout/unavailable advisory-only. Adoption requires native baseline + predeclared benefit/soundness/latency/maintenance bars (accuracy/coverage/latency/cost + encoder fidelity for solver families, trust-boundary binding + generic consumption + Test N for analyzer).

## 7. Cross-cutting concerns

- Determinism: declared inputs versioned; operational metadata excluded or declared; nondeterminism detected (K16).
- Security: tenant isolation, evidence/analyzer output as untrusted input, parser/certificate/process size + resource bounds, audit integrity, secret/PII redaction in logs.
- Failure semantics: typed failures end to end (`Provider → Ledger → Validator → Evaluator → Checker → Projection → Gate`); `FAILED`/`INCOMPLETE`/`COMPLETE_EMPTY` never conflated; provider recovery reopens evaluation.
- Schema evolution: explicit versions, tolerant readers, fail-closed legacy, migration fixtures.
- Observability/cost: obligations + certificates per investigation; premises per derivation (avg/tail); dependents invalidated per change; reuse rate; full-recompute %; ledger/certificate growth; snapshot-validation latency; proof-eval time vs retrieval/workflow. Content-addressed reuse first; no proof cache/graph DB until measurements demand it.
- Compatibility: per-step flag-off contract; schema migration reversible where feasible; missing legacy metadata never implicitly trusted.
- Failure to prove: insufficient evidence, missing coverage, unsupported capability, timeout, invalid certificate, stale context, exhausted budget → typed abstention with missing codes, never `VERIFIED`/`CONTRADICTED`.

Do-not-do (abridged, normative): no second runtime/graph store; no witness-gated `VERIFIED`; no lexical/LLM proof of code relations; no solver beyond admitted value/branch predicates; no verdict caching by claim text or cross-revision bank reuse; no workflow/confidence-as-proof; no obligation-table persistence before shadow value; no high-cardinality labels; no side-effecting shadow; no durable `UNSUPPORTED`; no freshness-as-applicability; no unvalidated legacy admission; no count-gated monotonicity; no cross-build composition; no derived-copy corroboration; no mixed-snapshot publication; no authoritative `LIKELY` cap in edge state; no depth-cap-as-verdict; no single-hash identities; no `*_validated` authority; no planner-fed derivation; no persisted deficiencies; no witness-shaped native model; no premature caches; no producer-label trust; no self-declared soundness; no reproducible-without-complete manifests; no `CONFLICTED` catch-all; no check-then-unguarded-write; no bypass writes; no missing-artifact acceptance; no undeclared-dep invalidation.

## 8. Migration, rollout, observability, rollback

- Order: baseline + diagnostics → ledger (split flags) → kernel + checker → composition/publication → edges (own flag) + shadow planner → per-family migration → witness pilots on signal.
- Flags: `IAP_VERIFIER_V2` (removed from code; the live mechanism is the `metadata["verifier_version"]` pin, current `v2`), `IAP_PROOF_KERNEL`, `IAP_TYPED_EDGES`, `IAP_SHADOW_PLANNER`, `IAP_WITNESS_ANALYZER`, `IAP_WITNESS_SOLVER`; defaults off except existing behavior; independent flags + isolated experiments per step. Only `IAP_TYPED_EDGES` exists in code today; `IAP_SHADOW_PLANNER` and the rest remain proposals, not code.
- Dashboards: proof metrics (§7) + existing loop telemetry + OTel closed vocab; reason-code series for false accepts / provable / abstentions separately.
- Rollback: flag-off restores prior authoritative behavior without bypassing the gate; per-family rollback; no data corruption or ambiguous workflow state.
- Runbooks: publication-race recovery, stale-context deny/reevaluate, provider-recovery reevaluation, invalidation-storm triage, witness-pilot kill-switch.
- Readiness per stage: correctness + compatibility + operational + rollback criteria; no advance on open mandatory gate.

## 9. Risks, decisions, ADRs

Open decisions (owner + deadline required): hypothesis-CAS design; per-kind snapshot-reuse policy; `*_validated` derivation registry shape; durable obligation table (only on measured need); analyzer/solver adoption scorecards; production quota/SLO values. Risks: canonicalization drift across services; rule-implementation drift without digest bump; invalidation over/under-breadth; storage growth of manifests/certificates; migration-write races. Each risk carries owner, trigger, mitigation, and gate.

Ownership (decided 2026-10-23): single-member team — all eleven decisions owned by the sole engineer, with the assistant acting as chief architect (drafts, analysis, and recommendations only; approvals stay with the owner). ADR-01–05 decided 2026-10-23 (`docs/architecture/ADR-KERNEL-001.md` through `ADR-KERNEL-005.md`); remaining deadlines TBD — no Stage B build work starts until fixtures + gate entry criteria are met.

ADRs required before Stage B: ADR-01 closed registry + digest binding; ADR-02 four-ID scheme + canon/v1; ADR-03 atomic publication mechanism on the actual store; ADR-04 generic witness contracts; ADR-05 shadow-planner boundary. Material changes to v6 decisions need new ADRs, never silent edits.

## 10. Adversarial self-review (20 scenarios; fault → detection → expected outcome → test → gate)

1. Valid hash + false semantic type → semantic validator + K1 → reject → K1 → G4.
2. Circular derivation → lineage cycle check → reject → K2 → G4.
3. Empty search as absence → coverage premise → deny, `OPEN` → K3 → G4.
4. One execution for universal → quantifier check → reject → K4 → G4.
5. One route fails, one survives → alternative evaluation → supported via survivor → K5 → G5.
6. Root change, transitive dependent stale → `Deps` closure → reevaluate all → K6 → G5.
7. Distinct claims collide → identity fixtures → migration, no transfer → K7 → G4.
8. Evidence changes at publication → context revalidation + CAS → deny/reevaluate → K8 → G5.
9. Rule impl changes, identity unchanged → digest/release control → detect → K9 → G4.
10. Missing premise artifact → availability check → `PREMISE_UNAVAILABLE` → K10 → G4.
11. Cross-scope support/refute → scope compatibility → no conflict → K11 → G5.
12. Policy changes, proof same → Layer split → proof kept, decision reevaluated → K12 → G5.
13. Fast path/confidence bypass → obligation gate → deny → K13/M → G4.
14. Duplicate evidence as corroboration → lineage dedup → no inflation → K14 → G5.
15. Stale certificate/context → context check → reject → K15 → G5.
16. Nondeterministic evaluator → reproducibility test → fail → K16 → G5.
17. Truncated analyzer result as negative → completeness-indexed admissibility → discovery-only → G/T → G8.
18. Provider recovers, `UNSUPPORTED` persists → context-scoped status → reevaluate → Q → G6.
19. Undeclared dep over/under-invalidates → `Deps(R_i)` + mutation → correct closure → mutation suite → G5.
20. Legacy path publishes without gate → transition-API audit → rejected + inventoried → bypass tests → G5.

Uncovered-by-list risks still open: resolver soundness for dynamic dispatch/generated code; multi-tenant policy interaction with publication freshness; certificate-storage retention vs audit completeness. Each is a tracked risk, not a silent assumption.

## 11. Readiness verdict: NOT READY for implementation (conditional)

- Ready: Stage A (baseline + diagnostics + ledger design); problem understanding and gate structure; current-behavior mapping with verified symbols.
- Blocked: frozen kernel spec + fixtures (G1/G2 in sixth-review terms: versioned kernel contract + canonicalization registry); hypothesis CAS + atomic-commit design against the actual store; closed registry with digests + semantic validators; K1–K16 + A–T + property/mutation/concurrency suites (fixtures before code); legacy-writer inventory routed through the transition API.
- Missing evidence: measured native baseline attributing banked-but-unverified to binding vs retrieval vs policy vs variance; resolver soundness boundaries per language; witness benefit scorecards.
- Decisions needing approval: ADR-01–05 owners and deadlines; per-kind snapshot-reuse policy; witness adoption thresholds; production SLO/quota ownership.
- Recommendation: authorize Stage A only; prepare frozen kernel spec + fixtures in parallel; withhold authoritative Stage B–D changes until G4/G5 pass; keep Stage E deferred. Do not schedule build tasks from this package until the plan-doc entry criteria for Phase 2 are satisfied.

Traceability: every requirement above maps to a design section, work package, test, and gate; no requirement was dropped for convenience. Gaps are reported honestly above.
