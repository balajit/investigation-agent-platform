# IAP S1-S7 Phase 0 Report

## Phase outcome

**WP-01 IMPLEMENTATION COMPLETE — VERIFICATION READY / STOPPED**

Phase 0 source revalidation is complete. WP-01 was explicitly authorized and its production composition correction was implemented and focused-tested. The evidence does not establish full operational acceptance because live fresh-context persistence and tenant-isolation execution remain outstanding. WP-02 through WP-09 were not started.

## Repository and execution baseline

- Repository: `investigation-agent-platform` workspace
- Branch: `iap-full-1025`
- HEAD: `8d2c13fd004e746b765c681b4481b58f90247b9d`
- HEAD: `T-E r2 grader + comparability harness + fixture keys + baseline regen, docs §110`
- HEAD timestamp: `2026-11-01T01:16:11-04:00`
- Python: `3.13.15`
- OpenCode: `1.19.3`
- Working tree: dirty before this task; existing changes were preserved
- OpenCode project configuration: default `professor`, `subagent_depth: 1`
- Discovery: seven expected project agents were confirmed by `opencode agent list --pure`; ambient agents were excluded from project conclusions

## Instructions and design documents read

`AGENTS.md`, `professor/AGENTS.md`, `professor/luna_iap_rev.md`, `professor/luna_prompt.md`, `professor/phase2_prompt.md`, `docs/design/DES-S6-binding-v1.0.md`, and `docs/design/DES-distill-brief-v1.0.md` were read before source revalidation and artifact creation.

## WP-01 through WP-09 disposition

| WP | Result | Evidence summary |
| --- | --- | --- |
| WP-01 | IMPLEMENTED; LIVE ACCEPTANCE PARTIALLY UNVERIFIED | Production bootstrap now assigns `PostgresConclusionUnitOfWork` with the production session factory, hypothesis repository, and finding repository. |
| WP-02 | IMPLEMENTED; LIVE REPLAY PARTIALLY UNVERIFIED | S4 now emits typed outcomes and forcing requires explicit `READY`; exceptions and missing outcomes fail closed. |
| WP-03 | IMPLEMENTED; LIVE ACCEPTANCE PARTIALLY UNVERIFIED | State-aware handoff validation and classifier fail-closed handling are implemented; live workflow/database acceptance remains outstanding. |
| WP-04 | IMPLEMENTED; LIVE ACCEPTANCE UNVERIFIED | Typed exact-fetch continuation, tenant-scoped CAS/leases, API dispatch, dedicated Temporal workflows, lifecycle creation/reconciliation, and continuation-only conclusion history replacement are implemented. Focused tests pass; live database/Temporal acceptance remains outstanding. |
| WP-05 | IMPLEMENTED; LIVE ACCEPTANCE UNVERIFIED | Range remains production-called; cbv4 adds Delta diff witnesses, final-gate Contrast binding, and data-gated Mechanism-shift validation. Focused tests pass; live provider/telemetry acceptance remains outstanding. |
| WP-06 | IMPLEMENTED SLICE; LIVE ACCEPTANCE UNVERIFIED | `lv14` adds deterministic frontier selection for locatable smallest-fetch work orders while preserving fixed-order fallback and older pins. Full forcing-to-frontier migration and live acceptance remain outstanding. |
| WP-07 | PARTIALLY IMPLEMENTED | Context selection, tiering, excerpts, provenance, and budgeting exist; accepted rerank/prune/brief/recall/faithfulness/feedback requirements are not established. |
| WP-08 | VERIFIED DEFECT | Production coordinator builder injects `_DefaultPromptSafetyPolicy`, whose validator always returns `True`. |
| WP-09 | UNVERIFIED | Unit tests, bounded DB evidence, and partial replay harness exist; no actual end-to-end S1-S7 acceptance battery was established. |

Detailed evidence is in `IAP-S1-S7-remediation-findings.md`, `IAP-S6-binding-contract-matrix.md`, and `IAP-distillation-contract-matrix.md`.

## Tests and verification performed

```text
pytest -q tests/unit/test_h14_conclusion_unit.py tests/unit/test_h14_finding_persist_order.py tests/unit/test_s4_qualification.py tests/unit/test_s7_priced_handoff.py tests/unit/test_buffered_persistence.py tests/unit/test_f8_forcing_rules.py tests/unit/test_synthesis_forcing.py
257 passed in 1.59s

pytest -q tests/unit/test_s6_binding.py tests/unit/test_j5_newer_constructs.py tests/unit/test_frontier.py tests/unit/test_j10_frontier.py
76 passed in 1.50s
```

These tests are focused unit/domain/composition evidence. No live database, Temporal worker, API, model provider, or full workflow test was executed for this Phase 0 report.

WP-01 focused verification additionally ran:

```text
pytest -q tests/unit/test_gateway_composition.py tests/unit/test_h14_conclusion_unit.py tests/unit/test_h14_finding_persist_order.py
32 passed in 2.18s
```

Python bytecode compilation of the touched source and test files also passed. The focused test run emitted two existing runtime warnings.

WP-02 focused verification additionally ran:

```text
pytest -q tests/unit/test_s4_qualification.py tests/unit/test_synthesis_forcing.py tests/unit/test_tranche2_cascade_guards.py
117 passed in 0.50s

pytest -q tests/unit/test_f8_forcing_rules.py tests/unit/test_cascade_seam.py
104 passed in 0.49s
```

Python bytecode compilation of the touched WP-02 source and test files passed. No live Temporal replay or workflow execution was performed.

WP-03 focused verification additionally ran:

```text
pytest -q tests/unit/test_s7_priced_handoff.py tests/unit/test_r9_capability_handoff.py tests/unit/test_conclusion_rewrite.py tests/unit/test_t5_priced_bound.py tests/unit/test_t4_stall_exit.py tests/unit/test_h7_typed_failures.py tests/unit/test_h7_reasoning_degrade.py
156 passed, 1 skipped in 0.72s
```

The skipped test requires a Temporal test server and owner-approved binary download. Python bytecode compilation of the touched WP-03 source and test files passed.

## Findings disproved or no longer applicable

No major prior WP-01 through WP-09 concern was disproved by current source. Existing functionality is stronger than the original concern in several areas: PostgreSQL UoW code exists, S4 domain predicates are substantially fail-closed, range binding is implemented and called, frontier domain logic is present, and policy rejection works when a rejecting policy is injected. Those facts do not close the production wiring or contract gaps described above.

## Security investigation

Tracked `.env.local` remains present. A safe key-name scan found credential-shaped assignment names in `.env.local` and `.env.example`; values were not read, printed, or copied. Credential rotation and approved repository-history remediation were not performed and require explicit owner direction.

## Severity and release impact

P1 release/acceptance blockers are WP-01, WP-02, WP-03, WP-04, WP-08, and WP-09. WP-05, WP-06, and WP-07 are P2 contract/integration gaps. The combined evidence supports `NOT READY` for release evaluation and does not authorize implementation.

## Proposed implementation scope, dependencies, and test plan

The next work should be separately authorized and executed sequentially, not as a batch:

1. WP-01: prove fresh-context readback, atomic rollback, tenant isolation, and commit-failure handling.
2. WP-02: execute Temporal replay/live workflow verification for typed qualification outcomes and fail-closed forcing.
3. WP-03: execute live workflow/database acceptance for state-specific handoff invariants and assess malformed persisted-row remediation.
4. WP-04: produce the required continuation design before any implementation; bind exact fetch, tenant, authorization, idempotency, pointer consumption, and retry semantics.
5. WP-05: complete the S6 contract matrix and obtain owner decisions for any deferred Delta/Mechanism-shift semantics.
6. WP-06: reconcile frontier and gap-fill into one authoritative scheduling model through a design-only step before integration.
7. WP-07: define measurable distillation evidence for recall, faithfulness, citations, dissent, and feedback without inventing thresholds.
8. WP-08: inject a real production safety policy and prove rejection occurs before provider invocation on every construction path.
9. WP-09: execute scenarios A-I using actual application composition and mark unavailable runtime scenarios `NOT VERIFIED`.

Dependencies include owner decisions on contract semantics, database/Temporal/provider availability, safe test fixtures, and explicit authorization for each implementation or design phase. No migration, public contract change, secret rotation, or external-system action is included in this report's authorization.

## Risks and unresolved questions

- Production wiring defects are source-confirmed but not live-reproduced.
- WP-01 and WP-02 focused tests pass, but their live acceptance gates remain outstanding.
- WP-03 focused tests pass, but live workflow/database acceptance and malformed-row remediation remain outstanding.
- The exact accepted semantics for S6 Delta and Mechanism-shift need confirmation if the design is intentionally incomplete.
- Frontier integration must avoid creating a second scheduler.
- Distillation thresholds must come from the accepted design or owner decisions.
- Full workflow replay and provider-backed acceptance remain unavailable in the current evidence.
- The tracked `.env.local` requires a separate security decision.

## Source changes

Created during Phase 0:

- `docs/audits/IAP-S1-S7-remediation-baseline.md`
- `docs/audits/IAP-S1-S7-remediation-findings.md`
- `docs/audits/IAP-S6-binding-contract-matrix.md`
- `docs/audits/IAP-distillation-contract-matrix.md`
- `docs/audits/IAP-S1-S7-phase0-report.md`

Created during authorized WP-01 implementation:

- `src/investigation_agent_platform/bootstrap/__init__.py`
- `tests/unit/test_gateway_composition.py`

No migrations, public contracts, or configuration were changed. The WP-01 production change is limited to composition wiring; the lifecycle and transactional UoW implementation were not altered.

## Mandatory stop

WP-01 implementation is complete for this authorization boundary. I will not begin WP-02 or any later work package until the user provides explicit authorization. WP-01 live acceptance remains a documented follow-up, not a basis for proceeding automatically.
