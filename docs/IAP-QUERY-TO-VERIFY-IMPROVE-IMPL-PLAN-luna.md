# Implementation Plan: Query-to-Verify Improved Proof Core

Historical companion-plan copy, reconciled for the Luna design package. The detailed normative sequence is [IAP-proof-kernel-plan-v1.0-luna.md](./IAP-proof-kernel-plan-v1.0-luna.md); the controlling architecture update is [IAP-QUERY-TO-VERIFY-IMPROVED-DESIGN-luna.md](./IAP-QUERY-TO-VERIFY-IMPROVED-DESIGN-luna.md). This file preserves the earlier work breakdown as history; conflicting proposals below are superseded by the Luna design and plan. It is not implementation authorization.

## Operating rules

All phases obey these rules without exception. Flag-off paths reproduce current behavior bit-for-bit, proven by flag-off-identical tests per phase. Telemetry uses additive fail-open hooks with closed label vocabularies, so a metrics failure can never fail verification. Served-cached conclusions follow primaries and grade the cached answer, so schema additions never change cached verdict semantics. The existing workflow keeps retries, signals, budgets, checkpoints, and terminal transitions; no second runtime is introduced at any phase. Analyzer and solver outputs never satisfy a gate alone at any phase. No commit without operator approval per standing constraint.

## Phase 0 — Synthetic proof-closure harness gate (hard precondition)

Goal: prove the failure taxonomy before baking it into product behavior. Builds the test-only harness in D1 with synthetic fixtures for every representative shape (L1 location, L2 caller enumeration, L3 history, L4 corrected path, L5 guarded behavior, L8 nonexistent target) crossed with banked-but-unverified, bound-but-wrong-type, wrong-revision, evaporated-ID, missing-causal-step, and genuinely-absent variants, plus a sampled set of real banked-but-unverified runs classified the same way.

| Item | Detail |
|---|---|
| Design decisions | D1, seven-bucket vocabulary from D3 |
| Files created | `application/investigation/proof_closure.py`, `tests/unit/test_proof_closure_harness.py`, `tests/fixtures/closure/*.json` |
| Files touched | None in product paths |
| Tests | Single-bucket classification per fixture; unclassifiable fixture fails the gate as a taxonomy defect; L8 fixtures must abstain; sampled real runs classify identically |
| Metrics | Bucket distribution over fixtures and sampled runs; unclassifiable count (must reach zero) |
| Exit | Every fixture lands in exactly one bucket, abstention fixtures abstain, sampled real runs agree; operator records go |
| Rollback | Delete harness directory; zero product impact by construction |
| If gate fails | Fix the taxonomy (split, merge, or redefine buckets), not the verifier; re-run until green |

## Phase 1 — Binding diagnostics (historical proposal; superseded)

This earlier proposal grouped retrieval-ledger work, citation repair, and authoritative typed-edge behavior in one phase. The Luna plan supersedes that sequencing: diagnostics first, retrieval separately with no repair, kernel and composition gates before Stage D edges, and repair only as a later independently gated experiment. The details below are historical work-package notes, not authority to implement them in this order.

| Item | Detail |
|---|---|
| Design decisions | D2, D3, D4, D5, D12 hygiene |
| Files created | `application/investigation/retrieval_ledger.py`, `application/investigation/grounding.py`, `domain/relations/edges.py` |
| Files touched | `reasoning.py` (prompt version bump, per-claim query, validator), `activities.py` (ledger persistence into `assessments_json`, `_resolve` claim-local first), `verification.py` (`evaluate_hypothesis`, `check_claim_kind`, `call_site_proof` tier reorder, `causal_step_grounding`, `next_check_for`), `domain/finding/models.py` (evaporated and demotion diagnostics), `context_compiler.py` (per-claim rows first) |
| Tests | Guardrail pins (fabrication rejected, bounded repair kept, over-reach refused, evaporated demoted); tier ordering (static, dynamic, speculative-hop likely-max, speculative-endpoint fail-closed, construction-as-invocation fail-closed, enumeration completeness, depth-cap truncation with hint); flag-off-identical suite |
| Metrics | `evaporated_total` and `repaired_total` by bucket and kind; L2/L3/L5 blocker names; false-accept count as a separate series (must not increase) |
| Exit | L2/L3/L5 blockers name the bucket; L2#1-shape caps at likely while L2#2-shape abstains by predicate; battery `PARTIAL` still fails exit; full suite green, `ruff` clean, no new `mypy` errors on touched files |
| Rollback | Flags off reproduce prior behavior; ledger and edge fields are optional JSON keys ignored by old readers |

## Phase 2 — Obligation unification and shadow routing (historical proposal; superseded)

This earlier proposal includes the D7 model-graded entailment extension. The Luna design supersedes it for proof semantics: heuristic/model-graded output cannot discharge source-code obligations or contribute to a publishable certificate. Any future prose assistance is advisory only. The deterministic planner remains shadow-only until its own gate.

| Item | Detail |
|---|---|
| Design decisions | D6, D7, D8, D9 |
| Files created | `application/investigation/obligations.py`, `application/investigation/action_policy.py`, `tests/unit/test_obligations.py`, `tests/unit/test_action_policy.py` |
| Files touched | `activities.py` (typed verifier output, policy input plumbing, diagnostics), `workflows.py` (shadow policy computation beside forcing rules, divergence logging), `agent_metrics.py` (bound-vs-present, evaporated, repaired, forced-rate, readiness-vs-verified series), battery grader (obligation-level assertions) |
| Tests | Expansion table rows per supported kind and category; unknown pairs unsupported; roll-up (all-satisfied rolls up subject to ceilings, any-conflicted rolls up contradicted, else inconclusive with missing obligation); policy determinism (same obligations, history, capabilities, budget give the same action); no-required-open-on-verified; inconclusive ships a tool-hinted missing obligation; flag-off-identical suite |
| Metrics | `obligation_total` by kind, status, and missing code; `policy_divergence_total` by chooser; target-action utility, no-progress routing, capability misses, budget outcomes, parity vs current policy; L8 abstention preserved |
| Exit | Shadow shows strictly better next actions with zero gate-behavior change; strangler flag owns only mapped cases (definition range, call site, runtime leg, scoped absence); operator records go per mapped case |
| Rollback | Shadow flag off; policy module unused; forcing rules untouched as the authority until each mapping graduates |

## Phase 3 — Optional pilots (historical proposal; superseded)

The old sequence placed optional pilots immediately after shadow routing. The Luna design defers all witness work until Stages B and C pass and Stage D measurements establish a native limitation (G8).

| Item | Detail |
|---|---|
| Design decisions | D10, D11 |
| Files created | `application/analysis/path_witness.py`, `application/verification/solver_pilot.py`, `tests/unit/test_path_witness.py`, `tests/unit/test_solver_pilot.py` |
| Files touched | Value and branch validation call sites (post-gate diagnostic threading only), `loop_telemetry` (solver and witness series), battery fixtures (pilot goldens) |
| Tests | Packet validation (revision mismatch, bound overflow, broken ordering, empty path rejected); encoder goldens (guarded-return satisfiable-negation and unsatisfiable-negation, off-by-one counterexample with named-symbol projection, inadmissible classes rejected, timeout yields unknown, completion-default hygiene, zero solver calls on L8 shapes) |
| Metrics | Witness usable-path, mismatch, truncation, size, runtime, and divergence rates; solver admitted rate and satisfiable, unsatisfiable, unknown, and inadmissible split with p50 and p95 latency; counterexample fetch-hit rate; native-verdict flip count (must stay zero) |
| Exit | Analyzer: complete same-revision witnesses on positive fixtures, abstention on negative and incomplete cases, no unresolved false acceptance, acceptable performance for target repository sizes. Solver: admitted cases terminate within budget at 80 percent or better, every counterexample model maps to a bankable fetch, zero native flips, encoder goldens frozen |
| Rollback | Pilots are diagnostics behind default-off flags; declining a pilot removes its package pin candidacy and keeps the vocabulary without the dependency |
| If pilots fail | Decline the dependency and retain the native checker; record the measurement so the question stays answered |

## Phase 4 — Persist and prune (historical proposal; superseded)

The old sequence is retained for provenance only. The Luna design requires the kernel, composition and atomic publication milestones before authoritative edge behavior; it also requires a separate evidence-based decision before durable obligation persistence. Optional witness outputs remain advisory and cannot become tiebreak inputs to proof status.

| Item | Detail |
|---|---|
| Design decisions | D5 profiles, D6 persistence, D8 convergence, D12 cache rules |
| Files created | Alembic revision for `proof_obligations` (recompute-on-read, no historical backfill) |
| Files touched | `CheckpointRepository` and `InvestigationState` schemas as needed, profile configuration for onboarded tenants, forcing-helper call sites removed one by one |
| Tests | Invalidation on claim, revision, verifier-version, and evidence-availability change; old rows without new keys degrade to labeled fallbacks; per-helper parity suites before each removal; full battery with false-accept as a separate series |
| Metrics | Same series as Phases 1–3 plus per-helper removal parity; honest-abstention cases preserved three of three |
| Exit | Full suite green; battery pass, partial, and fail with false-accept tracked separately; operator confirms each removal |
| Rollback | Per-helper revert; obligation rows recompute from claims and verifier outputs, so deletion of the table degrades to derived mode |

## Traceability (historical proposal; superseded)

Phase 0 covers D1 and the D3 bucket vocabulary. Phase 1 covers D2, D3, D4, D5, and D12. Phase 2 covers D6, D7, D8, and D9. Phase 3 covers D10 and D11. Phase 4 consolidates D5, D6, D8, and D12. No phase implements a decision outside its row without an explicit operator-approved amendment to this plan.

## Do-not-do list (binding on all phases)

No second durable runtime or checkpoint store at any phase. No central graph database or analyzer platform as the relation store at any phase. No analyzer witness or solver verdict satisfying publication alone at any phase. No lexical or model-judged proof of source-code relationships at any phase. No solver encoding of call graphs, history, absence, dispatch, or whole-program behavior at any phase. No verdict caching by claim text or cross-revision inheritance of witness or solver attributes at any phase. No collapse of workflow status and verification status into router labels at any phase. No new persisted tables, analyzer caches, or solver verdict stores before shadow evidence justifies them at the phase gates above. No threshold or semantics changes to verification or abstention states to improve answer rate at any phase.

## Supersession and controlling sequence

This copied plan predates the v6 proof-kernel gate. Its Phase 1 combines typed-edge behavior with citation work, its Phase 2 includes model-graded entailment in roll-up, and its Phase 3 advances optional pilots before composition/publication is complete. These are preserved above as historical proposals, but must not be used as an implementation sequence. The authoritative Luna sequence and gate contract is `IAP-proof-kernel-plan-v1.0-luna.md`: Stage A baseline/diagnostics/retrieval; Stage B frozen minimal kernel and independent checker; Stage C composition, invalidation, reconciliation and atomic publication; Stage D typed edges and planner shadow followed by family migration; Stage E optional witnesses only on measured native-core limitation and G8. The Luna design at `IAP-QUERY-TO-VERIFY-IMPROVED-DESIGN-luna.md` rejects model-graded proof contribution, persistent edge-level `LIKELY` authority, and any Stage D behavior before G5. No task in the historical tables authorizes code changes.
