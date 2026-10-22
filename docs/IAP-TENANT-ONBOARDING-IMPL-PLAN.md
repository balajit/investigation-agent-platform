# Implementation Plan: Tenant Onboarding (Revision 2)

Companion to [IAP-TENANT-ONBOARDING-DESIGN.md](./IAP-TENANT-ONBOARDING-DESIGN.md) (decisions D1–D9). This plan sequences the remediation in review-roadmap order, gates each phase on measured evidence, and defines rollback per phase. No phase weakens an existing check, and no destructive flow ships before its backup, approval-binding, and restore proofs pass.

The feedback follow-up requirements and active phase gates are in [the canonical Luna implementation plan](./IAP-TENANT-ONBOARDING-IMPL-PLAN-luna.md) and the [security/concurrency](./IAP-TENANT-ONBOARDING-SECURITY-CONCURRENCY.md), [workflow correctness](./IAP-TENANT-ONBOARDING-WORKFLOW-CORRECTNESS.md), and [deployment/deletion assurance](./IAP-TENANT-ONBOARDING-DEPLOYMENT-DELETION-ASSURANCE.md) specifications. Phase 0R in that plan blocks executable intake or dispatch.

## Operating rules

All phases obey these rules without exception. Intake validation, eligibility evaluation, and approval binding complete before any clone, credential resolution, or external write. Privileged routes re-check capability and approval binding rather than trusting earlier checks. Telemetry uses additive fail-open hooks with closed label vocabularies, so a metrics failure can never fail provisioning. Served-cached conclusions and existing profile revisions are untouched; new optional JSON keys degrade to labeled fallbacks. No commit without operator approval per standing constraint.

## Historical Phase 0 — Scope and capability foundation

The capability foundation was implemented in commit `cb858f7`. Current as-built status and all active gates are maintained in [the canonical Luna implementation plan](./IAP-TENANT-ONBOARDING-IMPL-PLAN-luna.md); do not use the remaining original phase text below as the active execution sequence.

Goal: land the non-behavioral skeleton the whole remediation stands on. Declare the narrowed scope and trust boundary in the architecture docs, add the tenant registry model with canonical ids and tombstones, add the capability vocabulary with the platform-operator role and bootstrap invitation, and replace placeholder router and CLI examples with explicitly labeled non-executable stubs plus failing conformance test scaffolds so nothing aspirational can be mistaken for working.

| Item | Detail |
|---|---|
| Design decisions | D1 (identity, capabilities, bootstrap), scope statement |
| Files created | `application/onboarding/authorization.py`, `tests/unit/test_tenant_authorization.py` (matrix scaffold) |
| Files touched | `docs/IAP-architecture-v1.0.md` (scope section), tenants router stubs labeled non-executable |
| Tests | Slug normalization table; capability matrix (ordinary user denied onboard, approve-own denied, cross-tenant denied, operator allowed); bootstrap reserves but never self-claims |
| Metrics | None yet; conformance scaffolds fail loudly until implemented |
| Exit | Scope statement reviewed; authorization matrix green; every stub labeled and covered by a failing-then-passing test |
| Rollback | Pure additions; disable by not wiring routes |

## Phase 0R — Remediate implemented security and concurrency contracts (active details in Luna v2)

Goal: close review gaps in the committed capability helper and control-store schema before enabling intake or dispatch. Preserve the capability vocabulary and migration 023 data while adding scoped grants, reviewed database-role boundaries, tenant-safe references, lifecycle fencing, and execution generations.

| Item | Detail |
|---|---|
| Design decisions | Scoped authorization/revocation; registry bootstrap/admin path; forced RLS and tenant-composite relationships; cleanup epoch/fence; dispatch lease fencing; business execution generations. See the security and workflow supplements. |
| Files created/changed | Scoped grant types/resolver and tests; additive migrations after revision 023 and ORM changes; RLS role contract and database integration tests. |
| Tests | Cross-tenant/app/environment/operation scope and revocation; real application/worker/admin roles with FORCE RLS; cross-tenant FK rejection; bootstrap privilege audit; stale epoch/lease/generation race tests; fresh-install and upgrade compatibility. |
| Exit | Security/platform owners approve role/bootstrap boundaries; actual-role isolation and concurrency gates pass; routes/workers remain disabled until exit. |
| Rollback | Disable routes/workers; preserve all durable request/dispatch/audit records and fencing state. No destructive rollback or removal of committed intent. |

## Phase 1 — Control store, intake revisions, and executable intake (P1 core)

Goal: make intake durable, versioned, and safe. After Phase 0R, use the registry/control-store tables with verified RLS and role boundaries, immutable intake revisions with optimistic concurrency, typed secret references with path and value rejection, structured repo identifiers with destination allowlists and commit pinning, and the tenants intake/status surface. Files become offline capture and export only.

| Item | Detail |
|---|---|
| Design decisions | D2 (intake revisions, secret refs), D5 (outbound bounds), Data Storage (registry, requests) |
| Files created | Alembic revision for `tenant_registry` and `onboarding_requests`; `application/onboarding/intake.py`; tenants intake and status routes; `tenant-id.env.example.yaml` without paths or secret shapes |
| Tests | Secret values, filesystem paths, cross-tenant references, and write-capable refs rejected before side effects; stale-revision writes conflict; replay reproduces the same plan from pinned inputs; SSRF suite (DNS rebinding, redirect-to-private, encodings, oversize, branch movement, metacharacters) denied before contact |
| Metrics | Intake validation rejections by reason; SSRF denials by class |
| Exit | Multi-replica consistency on one approved revision; concurrent updates version-checked; host loss loses no request state; profile records immutable source revisions |
| Rollback | Feature-flagged routes; old file-based capture continues to work offline |

## Phase 2 — Adapters with real probes plus readiness and eligibility gates

Goal: earn the right to activate. Implement the four-step adapter pipeline with typed outcomes and probe provenance for telemetry, datasources, config servers, and knowledge corpora; wire the placement and eligibility policy with pre-side-effect rejection; enforce mandatory readiness gates with `auto_promote` unable to bypass them. Unknown kinds and omitted mandatory adapters block readiness.

| Item | Detail |
|---|---|
| Design decisions | D3 (adapters, read-only corpora), D4 composition, D9 eligibility |
| Files created | `application/onboarding/adapters.py` plus per-class adapters; eligibility policy module |
| Tests | Per-adapter healthy, wrong-tenant, unauthorized, timeout, stale-credential, partial-response, and outage cases; no `VERIFIED` without a successful scoped probe; unsupported combinations rejected before side effects with evidence recorded |
| Metrics | Probe outcomes by adapter and class; eligibility rejections by policy reason |
| Exit | Every mandatory adapter green on fixtures; readiness decision durable, version-checked, and auditable; optional integrations skippable only per explicit policy |
| Rollback | Adapters individually flaggable; readiness falls back to blocking with review notes |

## Phase 3 — Durable orchestration, recovery, and attach (P1 core)

Goal: make provisioning resumable and safe. Ship the deterministic workflows with stable ids per request revision, the step ledger with idempotency keys and external resource references, the transactional outbox dispatcher with reconciler, compensation as separate idempotent actions, and the revision-pinned attach merge with conflict semantics.

| Item | Detail |
|---|---|
| Design decisions | D7 (orchestration, ledger, outbox, reconciler), D5 attach semantics |
| Files created | Workflow definitions, `onboarding_steps` table migration, outbox dispatcher and reconciler, attach merge with tombstone support |
| Tests | Follow the workflow correctness specification: crash injection before/after each side effect and dispatch boundary; replay duplicates nothing; uncertain outcomes reconcile by provider key; lease fencing; `AlreadyStarted`/ambiguous start; terminal retry generation; cancellation at each side-effect boundary; stuck jobs alert with explicit resume or terminate; concurrent attaches never lose updates |
| Metrics | Step durations, retry rate, stuck workflows, compensation and reconciliation backlog, orphan resources found |
| Exit | End-to-end apply and attach on fixtures with failure injection green; duplicate submissions replay or conflict correctly; no in-place mutation of the active request snapshot |
| Rollback | New workflow versions deploy alongside old; in-flight requests drain on their pinned definition |

## Phase 4 — Exact-plan cleanup with backup proof (P1 core)

Goal: make deletion safe before it exists. Follow the deployment/deletion assurance specification: derive the store registry from attested deployment inventory, detect drift, fence writes before final enumeration, canonicalize manifests with versioned RFC 8785 rules, atomically consume approval with purge job/outbox creation, and report distinct complete/retained/residual/incomplete/blocked/canceled outcomes. No purge ships until inventory, approval, backup, and restore gates pass.

| Item | Detail |
|---|---|
| Design decisions | D6 (manifest, atomic approval, tombstone, per-system purge), retention policy |
| Files created | `deletion_manifests` table migration; manifest planner and approval consumer; per-store purge workers; backup verification job |
| Tests | Concurrent confirmation yields at most one job; any plan change invalidates approval; app-scoped cleanup cannot touch siblings; legal hold blocks; shared-reference resources survive until unreferenced; restore drill passes; deletion idempotent and replay-safe |
| Metrics | Manifests by state; residuals by store; restore drill results |
| Exit | End-to-end deletion and restore rehearsal green on fixtures; audit distinguishes complete from residual and pending; backup retention and key separation verified |
| Rollback | Purge workers halt on flag; manifests remain as auditable intent records |

## Phase 5 — Hardening, abuse controls, and operability (P1 remainder)

Goal: close the adversarial and operational gaps. Fix pre-auth rate limiting to verified-principal or global buckets, add layered post-auth caps with stable retry guidance, complete the RLS catalog and app-role audit with cross-tenant negative probes, propagate signed tenant context end to end, and ship dashboards, alerts, runbooks, and backup and restore verification with approved service objectives.

| Item | Detail |
|---|---|
| Design decisions | D8 (layered limits, conformance suites), D9 (isolation audit, retention, telemetry) |
| Tests | Spoofed headers cannot spend another tenant's quota; per-principal, per-tenant, and global caps bound accepted work; tenant A cannot reach tenant B through REST, jobs, schedules, cache, search, artifacts, admin flows, or operator scopes |
| Metrics | Throttle decisions by layer; cross-tenant probe results; queue age and stuck-work alerts |
| Exit | Threat-model suite green; dashboards detect stuck work and backlog; restore meets approved objectives; SLI, SLO, and ownership documented |
| Rollback | Limiter and audit changes are additive checks; disable per check on false positives with logged reason |

## Phase 6 — Deferred items with explicit entry conditions (P2/P3)

Full optimistic-concurrency conformance suite beyond the Phase 0R gate, tenant and global quota fairness with load validation, compliance evidence mapping with regulated-tenant approval paths, and self-service, migration, and dedicated-tier futures. Each item starts only when its entry condition in the design's Risks and Documentation Plan is met; none may weaken a Phase 0R–5 gate or be built silently without its named owner.

## Traceability

Phase 0 covers the scope statement and D1 vocabulary. Phase 0R repairs the implemented authorization/schema foundations against the three review supplements. Phase 1 covers D2, D5, and the registry/request intake surface. Phase 2 covers D3, D4 composition, and D9 eligibility. Phase 3 covers workflow correctness and attach. Phase 4 covers deployment inventory, D6/D8 approval and deletion assurance. Phase 5 covers remaining abuse controls and operability. Phase 6 covers deferred items. P0 security/concurrency gates precede intake and dispatch; production acceptance gates in the assurance specification precede rollout.

## Do-not-do list (binding on all phases)

No customer identity, billing, subscription, DNS, certificate, infrastructure, or network provisioning inside IAP at any phase. No filesystem paths or secret values in API intake at any phase. No credential forwarding across hosts at any phase. No build-script execution during generation at any phase. No analyzer witness or solver verdict satisfying publication at any phase (unchanged from the proof-core program). No write paths into knowledge systems at any phase. No text-keyed verdict or cross-revision inheritance of approvals, manifests, or probe results at any phase. No threshold or semantics changes to verification or abstention states to improve answer rates at any phase.
