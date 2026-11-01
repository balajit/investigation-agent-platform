# IAP S1-S7 Remediation Findings

## Scope and method

Findings below were revalidated against the current source at HEAD `8d2c13fd004e746b765c681b4481b58f90247b9d` on branch `iap-full-1025`. Each finding distinguishes static evidence from runtime verification. WP-01 was subsequently authorized and remediated; all other work packages remain investigation-only.

## Findings

### F-01 — Production conclusion UoW is stale/in-memory

- **Work package:** WP-01
- **Classification:** REMEDIATED IN WP-01; runtime acceptance remains partially unverified
- **Severity:** P1 release blocker
- **Original evidence:** `src/investigation_agent_platform/api/dependencies.py:1919-1946` constructed `InMemoryConclusionUnitOfWork` with repository references. `src/investigation_agent_platform/bootstrap/__init__.py:180-187,237-256` replaced `ctx.finding_repo` with SQLAlchemy without installing `PostgresConclusionUnitOfWork`.
- **Implementation:** `src/investigation_agent_platform/bootstrap/__init__.py` now assigns `PostgresConclusionUnitOfWork(session_factory, hypothesis_repo, finding_repo)` after production repositories are created.
- **Tests:** `tests/unit/test_gateway_composition.py` asserts the production context receives the transactional UoW and the same production repositories. Focused WP-01 suite: `32 passed`.
- **Runtime:** Fresh-context database readback and tenant-isolation execution remain not verified.
- **Residual risk:** Live production composition and database acceptance still require the WP-01 acceptance gate.

### F-02 — S4 qualification failure can fall through to evidence quantity

- **Work package:** WP-02
- **Classification:** REMEDIATED IN WP-02; live replay acceptance remains unverified
- **Severity:** P1
- **Original evidence:** `application/worker/activities/reasoning.py:460-516` converted undecidable/exceptional S4 readiness to `{}`. `application/worker/workflows/forcing.py:1558-1684` treated absent readiness as permission for count-based forcing.
- **Implementation:** `_s4_readiness()` now returns explicit `READY`, `NOT_READY`, or `QUALIFICATION_FAILED` outcomes. The reason result preserves `s4_outcome`; forcing suppresses the floor only for explicit `READY`, and never uses row count as qualification. Missing outcomes fail closed.
- **Tests:** `tests/unit/test_s4_qualification.py`, `tests/unit/test_synthesis_forcing.py`, `tests/unit/test_tranche2_cascade_guards.py`, `tests/unit/test_f8_forcing_rules.py`, and `tests/unit/test_cascade_seam.py` pass: `117 passed` and `104 passed` in the focused groups.
- **Runtime:** Temporal replay and live workflow verification remain not verified.
- **Residual risk:** The current loop's typed outcome propagation is unit-tested but not proven through a live worker/replay environment.

### F-03 — Terminal handoff invariants are incomplete

- **Work package:** WP-03
- **Classification:** IMPLEMENTED; live workflow acceptance remains unverified
- **Severity:** P1
- **Original evidence:** `domain/finding/models.py:353-375` permitted state-invalid `S7Handoff` objects. `classify_terminal_handoff` could emit `BUDGET_DEFERRED` without a concrete fetch or positive deterministic price, and unknown inputs fell through to `FRONTIER_EXHAUSTED` without exhaustion proof.
- **Implementation:** `S7Handoff` now validates state-specific requirements. `BUDGET_DEFERRED` requires a concrete fetch, positive price, resume pointer, and steps. `FRONTIER_EXHAUSTED` requires an elimination record. Deferred states require recorded limitations. Unknown/malformed classifier inputs and unproven frontier outcomes use `REASONING_DEFERRED`. No migration or new persisted state was introduced.
- **Tests:** S7 handoff, R9 capability, conclusion rewrite, typed failure, stall, budget, and reasoning-degrade suites pass: `156 passed, 1 skipped`.
- **Runtime:** Live Temporal workflow execution and database persistence acceptance remain not verified.
- **Residual risk:** Existing persisted malformed handoff rows are rejected by the repository rehydration boundary and require remediation outside this WP-03 code change.

### F-04 — Exact S7 continuation is not production-consumed

- **Work package:** WP-04
- **Classification:** IMPLEMENTED; live acceptance unverified
- **Severity:** P1
- **Evidence:** WP-04 now adds typed continuation state and deterministic fetch derivation in `domain/continuation/models.py` and `application/investigation/continuation.py`; tenant-scoped continuation, conclusion-history, and reasoning-lease persistence plus migration `041_s7_continuations`; API claim-before-dispatch at `api/v1/routers/continuations.py`; dedicated Temporal workflows and activities; normal execution-path reuse; lifecycle continuation creation; and continuation-only archive-then-replace conclusion persistence.
- **Runtime:** Focused WP-04 and adjacent unit evidence passes (`96 passed` before final contract fixes; rerun required after final fixes). Full unit execution exceeded the five-minute verification window. Live database, Temporal worker/replay, and provider acceptance remain unverified.
- **Risk:** A displayed continuation pointer may not be executable or safely repeatable.
- **Required evidence before closure:** approved design and implementation evidence for exact fetch dispatch, tenant binding, authorization, idempotency, pointer consumption, retries, and partial failure.
- **Authorized implementation scope:** implement `DES-s7-continuation-v1.4` only: typed continuation and fetch grammar, continuation/history persistence, tenant-scoped CAS and lease, per-attempt continuation and reasoning workflows, normal-path execution, API dispatch, lifecycle creation/reconciliation, archive-then-replace, and focused tests. No WP-05 work.
- **Implementation:** The continuation activity reloads durable action state, validates version and canonical payload identity, propagates principal identity, reuses the normal executor, records bounded status outcomes, and coordinates the reasoning lease. The reasoning workflow reconstructs persisted context, reopens the same investigation, runs existing reason/verify/conclude activities, and uses the continuation repository's archive-then-replace path. Pending rows are superseded on new continuation creation and creation failure is audited without failing the already-persisted conclusion.
- **Residual risk:** Live database/Temporal acceptance cases A-K, full unit completion, and production provider behavior remain unverified. No migration was executed.

### F-05 — S6 four-dimensional binding is incomplete

- **Work package:** WP-05
- **Classification:** PARTIALLY IMPLEMENTED
- **Severity:** P2
- **Evidence:** Range is implemented in `application/investigation/verification.py:3399-3421,3878-3915,5201-5334` and called by `application/worker/activities/verification.py:682-727`. Contrast qualification exists in `domain/evidence/qualification.py:348-428`, but complete S6 conclusion binding is not established. No distinct Delta binding for hydrated before/after target-path change was found. No Mechanism-shift validator tying architecture-derived parents, incident boundaries, and conditional metric shift to the production path was found. The accepted definitions are in `docs/design/DES-S6-binding-v1.0.md:7-24`.
- **Tests:** Focused S6/Java/frontier tests passed, but do not cover the complete four-dimensional contract.
- **Runtime:** Full production semantics not verified.
- **Risk:** History or contrast evidence may be mistaken for complete S6 proof.
- **Required evidence before closure:** contract matrix, production caller trace, typed failure/refusal behavior, and adversarial tests for all four dimensions.

### F-06 — Collection frontier is not production-wired

- **Work package:** WP-06
- **Classification:** IMPLEMENTED BUT NOT PRODUCTION-WIRED
- **Severity:** P2
- **Evidence:** `domain/collection/frontier.py:1-10` explicitly documents no production caller. Frontier candidate ordering/exit logic is present at `:101-312`; work-order translation is present in `domain/collection/work_orders.py:153-215`. Production instead follows `verify_root_cause_activity -> _carry_gap_orders -> action_history -> _t_smallest_fetch -> execute_action_activity` across `application/worker/activities/verification.py:1159-1203`, `application/worker/workflows/forcing.py:2384-2449,3046-3099`, and execution activity wiring.
- **Tests:** Frontier unit tests passed; no production scheduler integration was established.
- **Runtime:** Not verified.
- **Risk:** Two different scheduling models exist conceptually, with no established authoritative frontier/fixpoint scheduler.
- **Required evidence before closure:** design reconciliation, one authoritative scheduler, replay-safe integration, budget/novelty/failure distinctions, restart behavior.

### F-07 — Accepted distillation contract is only partially implemented

- **Work package:** WP-07
- **Classification:** PARTIALLY IMPLEMENTED
- **Severity:** P2
- **Evidence:** `application/investigation/context_compiler.py:379-433,812-876,1075-1122` implements deterministic selection, tiering, grouping, excerpts, provenance, and token budgeting. The accepted design at `docs/design/DES-distill-brief-v1.0.md:7-21` also requires cross-encoder reranking, sentence-level pruning, extractive briefs with citations/dissent, a raw-token reduction target, recall-at-brief, faithfulness validation, and used/unused/incorrect-claim feedback. Those capabilities were not found in the inspected production path.
- **Callers:** `application/investigation/reasoning.py:1258-1271`, `application/worker/activities/reasoning.py:521-730`, and `application/worker/workflows/run_investigation.py:812-835` establish production source wiring for the existing compiler.
- **Runtime:** Not verified.
- **Risk:** Token budgeting may be mistaken for evidence-faithful distillation quality.
- **Required evidence before closure:** contract matrix, measurable recall/faithfulness tests, dissent/citation preservation, and feedback-path evidence.

### F-08 — Production prompt-safety policy always allows

- **Work package:** WP-08
- **Classification:** VERIFIED DEFECT
- **Severity:** P1 security blocker
- **Evidence:** `_DefaultPromptSafetyPolicy.validate_prompt_safety` at `bootstrap/__init__.py:57-61` returns `True` unconditionally. `build_reasoning_coordinator` at `:64-77` injects it. The coordinator checks the policy before the gateway at `application/investigation/reasoning.py:1415-1473`; injected rejection can block the gateway, but normal production composition has no rejecting policy. Activity fallback construction is at `application/worker/activities/reasoning.py:567-602`.
- **Tests:** Tests use injected doubles and do not demonstrate production composition plus no-provider-call rejection.
- **Runtime:** Not verified.
- **Risk:** Unsafe prompts or untrusted retrieved instructions can reach the provider through the default production path.
- **Required evidence before closure:** explicit production policy injection, missing-policy fail-closed behavior, exception semantics, alternate-constructor coverage, and zero-provider-call rejection test.

### F-09 — Integrated S1-S7 acceptance is not evidenced

- **Work package:** WP-09
- **Classification:** UNVERIFIED
- **Severity:** P1 acceptance blocker
- **Evidence:** The required scenarios and evidence standard are defined in `professor/luna_iap_rev.md:930-972`. Unit tests cover isolated S1/S2/S4/S6/S7 behavior. `tests/integration/test_s2_flow_battery_db.py:1-34` explicitly excludes model calls and Temporal. `tests/replay/iap_replay_harness.py:1-11` explicitly labels full `RunInvestigationWorkflow` replay open. No scenario matrix proving actual composition, real persistence, Temporal, fresh-context readback, tenant isolation, prompt safety, and replay was found.
- **Runtime:** No tests executed for WP-09.
- **Risk:** The system cannot be classified accepted or operationally ready from the current evidence.
- **Required evidence before closure:** scenarios A-I with traceable results and explicit `NOT VERIFIED` for unavailable environments.

## Security observation

Tracked `.env.local` remains present. A key-name-only scan identified credential-shaped assignments; no values were read or printed. Rotation and approved history remediation are owner decisions and were not performed.

## Overall disposition

The current branch is `NOT READY` for implementation completion or release evaluation. Phase 0 has identified an ordered remediation scope, but no remediation has been authorized or applied.
