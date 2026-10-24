# IAP Proof-Kernel Implementation Plan v1.0

> Staged, gated, parallel-safe. No authoritative Stage B–D behavior before executable contracts + adversarial gates pass. Stage E deferred.

## Phase → stage mapping

| Plan phase | Focus | Gate before advance |
|---|---|---|
| Phase 0 | Source + baseline understanding | Requirements, paths, gates mapped |
| Phase 1 | Diagnostics + retrieval ledger (no repair; no edge semantics) | Diagnostics do not perturb legacy; retrieval typed |
| Phase 2 | Frozen contracts + minimal kernel | Certificate, identity, context, rules, checker adversarially tested |
| Phase 3 | Composition, invalidation, reconciliation, publication | Scope-safe composition, closure, atomic publication pass |
| Phase 4 | Typed edges after Stage C; shadow planner + incremental migration | Shadow observable, compatibility proven, family-gated |
| Phase 5 | Optional witness pilots | Demonstrated incremental value + operational acceptance |

Stages: A = Phase 0–1 (GO). B = kernel (GATED). C = composition/publication (GATED). D = edges + routing (CONDITIONAL, per-family). E = witnesses (DEFERRED).

## Work breakdown (stable IDs; each WP has entry/exit criteria in execution)

- WP-A1 Baseline capture: pin revision/config/battery; per-claim traces (retrieved/bound IDs, validity, kind result, gate inputs/decision). → G1.
- WP-A2 Diagnostics: `evaporated_ids`, demotions, `MissingObligation`, reason codes; schema-versioned `assessments_json` with tolerant readers. → G2.
- WP-A3 Retrieval ledger: per-claim `search_query` + `RetrievalAttempt`; `search_failed` vs `scoped_empty` distinct; bounded re-retrieval; novelty + proof-lift measured. → G3.
- WP-B1 Canonical identity + fixtures (proposition/context/derivation/decision; canon/v1 vectors; canonicalization open items in design §1.2 decided with golden vectors first). Entry to B.
- WP-B2 Manifests + immutable evaluation (`Evaluate(C,E,R,M)`; retention or `PREMISE_UNAVAILABLE`).
- WP-B3 Typed rules + semantic validators (DEFINITION first, then CALL_EDGE; digests; unsupported abstention).
- WP-B4 Certificate serializer + independent checker (accept/reject/replay from schema + registry alone). → G4.
- WP-C1 Invalidation graph + composition (conjunction/alternatives; `Deps(R_i)` closure; cycle reject).
- WP-C2 Atomic publication + races (linearization point; CAS on context; stale reject; migration/role review + controlled interleaving test vs the actual DB/RLS path). → G5. Requires hypothesis CAS (see gap).
- WP-D1 Typed edges (orthogonal call-edge record + profile dispatch table) behind own flag after G5; resolver soundness boundaries + tests.
- WP-D2 Shadow planner (`application/investigation/` pure policy; no side effects; Temporal replay-safe). → G6.
- WP-D3 Family migration (definition → call-site → runtime → absence/history; parity + rollback per family). → G7.
- WP-E1/E2 Analyzer/solver pilots (generic contracts only; shadow; adoption bars). → G8.
- WP-F1 Security/compat/release audit (all stages).

Parallel matrix: A1 ∥ A2/A3-spec ∥ B1-spec ∥ E-research. B1 ∥ B2/B3-design ∥ fixture authoring. B4 needs frozen cert/rule contracts. C1 needs rule deps; C2 needs context + checker. D1/D2 separable; D3 needs G5+G6. E needs stable generic contracts + native baseline. F1 continuous.

Critical path: A1 → A2 → A3 → B-contracts frozen → B1–B4 → G4 → C1–C2 → G5 → D2-shadow → D3-families → G7. D1 behind its flag joins after G5.

## Gates G1–G8 (go/no-go; evidence, not code-complete)

- G1 Baseline: frozen inputs/traces/classifications; failures attributable; else continue diagnosis, no behavior change.
- G2 Diagnostics: reason-code + legacy-fixture comparison pass; no new false accepts.
- G3 Retrieval: query/attempt ledger + novelty + scoped-empty tests show benefit or defensible diagnostic gain; else keep telemetry, no repair-by-assumption.
- G4 Kernel: certificate goldens + semantic-premise + checker-rejection suite all pass. No authoritative rollout on fail.
- G5 Composition: invalidation, conflict, alternative-route, race, mutation tests pass; no stale/inconsistent publication; unrelated routes preserved. Stages D–E stay non-authoritative on fail.
- G6 Shadow: deterministic replay + divergence/budget/recovery tests; measurable action-quality or diagnostic benefit; zero side effects.
- G7 Migration: per-family parity + rollback + retry/pause/resume/budget tests; next family only after current passes.
- G8 Witnesses: contract tests + incremental-value + cost analysis pass approved thresholds; else reject/defer.

Stop conditions (any gate): `VERIFIED` without valid current derivation; silent drop raising strength; failed route killing valid route or ignored counterexample; commit after relevant context change; nondeterministic derivations on same inputs; workflow/score/analyzer/solver bypass.

## Feature flags + compatibility

- Future behavior flags (none exist yet): add `IAP_PROOF_KERNEL` (off; checker + certificates), `IAP_TYPED_EDGES` (off, authoritative use only after G5), `IAP_SHADOW_PLANNER` (shadow-only, never executes), `IAP_WITNESS_ANALYZER` / `IAP_WITNESS_SOLVER` (shadow-only). Engine-version changes route on the recorded per-investigation version, never on environment switches. Retrieval ledger and edges ride separate flags so any changed result attributes to exactly one cause. Proposed flags are not current configuration keys or implementation authorization.
- Flag-off equivalence defaults to identical outputs + persisted rows + routing + externally visible behavior for Phases 1–2; any deviation explicit per step.
- Schema: version `assessments_json`; tolerant readers; no fabricated defaults; legacy rows classified discovery-only / revalidatable / historical-context.

## Definition of done (per WP)

Scope + files; preconditions (passed gates, frozen contracts); implementation + schema/version impact; positive/negative/adversarial/replay fixtures; metrics + reason codes; compatibility + rollback; exit evidence (fixtures, sign-off, measurements); decision (GO / CONDITIONAL / HOLD / NO-GO). Gate owner approves evidence, not code presence.
