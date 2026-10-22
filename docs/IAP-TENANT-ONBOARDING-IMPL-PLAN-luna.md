# Implementation Plan: IAP Tenant Configuration Onboarding — Luna Revision

> **Active sequence (consolidated):** This file is the active phase sequence. It records Phases 0 and 1A as committed as-built status, gates the required Phase 0R remediation before intake or dispatch, and sequences Phases 1B–6. The former `IAP-TENANT-ONBOARDING-IMPL-PLAN-luna-v2.md` has been folded into this file and removed. Stop at each phase boundary for owner review; do not start the next phase until that review is accepted. Current session work is planned and reviewed in the session sequence below; implementation status is recorded only after each phase's checks complete.

Companion to [IAP-TENANT-ONBOARDING-DESIGN-Luna.md](./IAP-TENANT-ONBOARDING-DESIGN-Luna.md). This plan incorporates the focused architecture review of capability completeness, workflow dispatch, deletion-store coverage, and approval-token correctness. It is a sequence and acceptance contract, not implementation authorization. Do not commit, migrate a live database, purge data, deploy, or execute external onboarding without the required explicit approval.

The follow-up review is specified in [the security/concurrency supplement](./IAP-TENANT-ONBOARDING-SECURITY-CONCURRENCY.md), [the workflow correctness specification](./IAP-TENANT-ONBOARDING-WORKFLOW-CORRECTNESS.md), and [the deployment/deletion assurance specification](./IAP-TENANT-ONBOARDING-DEPLOYMENT-DELETION-ASSURANCE.md). These supersede less precise approval, RLS, retry, inventory, and residual language below. Phase 0R is required before enabling intake or dispatch because already-committed Phase 0/1 foundations do not yet satisfy those contracts.

## Operating Rules

- Implement the narrowed IAP configuration-onboarding scope only. Identity, billing, subscriptions, DNS, certificates, networks, and infrastructure remain external authorities.
- Every tenant-bearing row and operation uses canonical tenant identity, explicit capability authorization, RLS/repository scoping, and an audit decision.
- Intake, policy, capability catalog, source commits, and adapter versions are immutable per accepted request revision.
- The normative workflow-start mechanism is a same-transaction PostgreSQL outbox. Do not implement direct API-to-Temporal start as the normal path.
- Outbox delivery is at least once. Temporal start uses a deterministic workflow ID; every external side effect uses step idempotency or discover-before-create and has reconciliation behavior.
- No tenant becomes `ACTIVE` unless all catalog-required readiness checks pass for the same request, policy, and catalog versions.
- No cleanup is approved or reported complete unless all registered required stores have complete inventory and terminal per-entry outcomes, subject to documented retention dispositions.
- No destructive phase ships before approval binding, legal-hold, backup, restore, shared-reference, and residual-reporting acceptance criteria pass.
- Keep metrics fail-open, but keep authorization, state, outbox, audit, store enumeration, and approval persistence fail-closed.
- Record unresolved recommendations in the project ledger. Update architecture/runbook documentation before closing a recommendation as implemented.
- Do not enable intake or dispatch until Phase 0R repairs pass. Do not enable cleanup until fencing, inventory assurance, atomic approval, and restore gates pass.
- Before any registry repository implementation, resolve and review the separately authenticated registry-admin access model required by FORCE RLS. No `BYPASSRLS`, magic tenant GUC value, or unreviewed permissive policy is allowed.
- Establish a baseline once per session. Record existing failures and do not repeat the same known-failing full suite after every edit; use focused tests during a phase and rerun broader checks at phase close.

## Current Session Work Breakdown — review after every phase

This work is a staged implementation, not permission to bypass the Phase 0R gate or claim production readiness. A completed phase pauses for owner review; the next phase does not begin until accepted.

### Session Phase A — Baseline and planning record

- [x] Record existing test baseline and failure signature in this ledger.
- [x] Verify recommended sequence against current code and preserve pre-existing untracked review documents.
- [x] At close, report the failure baseline so it is not repeatedly rerun during every subsequent change.

**Status:** Complete, baseline corrected 2026-10-22. The 5 `test_mcp_fanout.py` failures recorded at session baseline were fixed by commit `fe611a9` (spec-bound deduplicator mock); the fanout suite is 6/6 green and restored to normal triage. Session baseline otherwise stands: 104 passed, 86 skipped. No onboarding code changed during baseline/planning.

### Session Phase B — Phase 0R authorization/RLS and dispatch foundation — IN PROGRESS

- [x] Record approved grant authority, database roles, opt-in PostgreSQL test environment, and worker polling-policy decisions.
- [x] Inspect existing database session, background-job, migration, and operator-bootstrap patterns.
- [x] Additive schema and ORM: `onboarding_grants`, unique `(tenant_id, id)` for background jobs, composite request/dispatch job FKs, dispatch fencing/generation, append-only dispatch/grant audit; leave migration 023 unchanged and seed no grant rows.
- [x] Implement database-backed scoped grant resolution from verified JWT `sub`; tenant claim is identity context only; fail closed on invalid scope, time window, revocation, or policy-version mismatch.
- [x] Provision `iap_migrator`, `iap_app`, `iap_worker`, and `iap_registry_admin` outside migrations with no runtime DDL or `BYPASSRLS`; migration 024 revokes revision 023 ACLs and applies the explicit role matrix.
- [x] Add human-only registry-admin policy and dedicated iap_worker policies on `onboarding_dispatches` only; worker UPDATE is column-limited; worker request reads require transaction-local tenant context from the claimed dispatch.
- [x] Implement atomic `SKIP LOCKED` claims, monotonic fencing, lease renewal/release, expired-lease reconciliation, audit identity/token, stale-owner rejection, and shared reconciler claim path.
- [x] Add opt-in actual-role tests gated by `IAP_RUN_ONBOARDING_DB_TESTS=1`, `IAP_TEST_PG_URL`, TCP skip, scratch-role setup/teardown, cross-tenant visibility, concurrency, privilege denial, stale-token, app isolation, and migrator DDL checks; test run remains skipped when PostgreSQL is unreachable.
- [x] Document role provisioning, dual-person first-platform-grant workflow, and DB-trigger `updated_at` maintenance for raw worker updates.
- [x] Add the dual-person grant mint/revoke CLI with exact-payload digest checks and an append-only audit row; external identity verification remains operator responsibility.
- [x] Ensure dispatcher claim/renew/release/stale-fence audits contain transaction-local worker process ID and database `session_user`; both are attribution only, never authorization.
- [x] Run focused/phase-close checks; Ruff, mypy, offline Alembic SQL, and diff checks pass. Pause for owner review before intake schemas.

**Phase B owner decisions, 2026-10-22:** The entitlement, role, and test-environment choices are approved. The worker policy decision further approves a single dedicated role-targeted polling/claim policy on `onboarding_dispatches`, limited column UPDATE grants, `SKIP LOCKED` atomic claims, tenant-context request reads, worker process and database identity/token auditing, and shared dispatcher/reconciler operations. The worker `WITH CHECK` permits null lease owner on release/requeue or requires the owner to equal transaction-local `iap.worker_id`. Registry admin remains human/operator-only. No tenant-directory table or registry-admin polling path is permitted. Rationale and exact constraints are recorded in `docs/IAP-TENANT-ONBOARDING-DB-ROLES.md` and the ledger.

**Binding Phase 0R implementation sequence — implemented; disposable-database verification passed:**

1. Additive migration/ORM: `onboarding_grants`; `UNIQUE (tenant_id, id)` on `background_jobs`; tenant-composite dispatch job FK; dispatch lease fencing token and execution generation; explicit role grants/policies including the dispatch-only worker policy; no edits to revision 023 and no grant seed data.
2. Authorization: resolve database-backed grants against verified JWT `sub`, capability, canonical scope, validity, revocation, and policy version; represent platform scope only with the explicit `__platform__` literal; fail closed.
3. Worker operations: atomic fleet-wide dispatch claim with column-limited update privileges, append/record audit identity and fencing token, then set tenant-local context from the claimed dispatch before any request/intake read; implement lease renewal/release and stale-token rejection. Reconciliation shares these operations.
4. Bootstrap/roles/docs: provide reviewed external role-provisioning script (migrator/app/worker/registry-admin, no runtime DDL or BYPASSRLS), dual-person offline first-grant procedure and audit runbook. No migrations seed platform grants.
5. Actual-role integration gate: opt-in `IAP_RUN_ONBOARDING_DB_TESTS=1`, `IAP_TEST_PG_URL` default `postgresql://iap:iap@localhost:5432/iap_test`, TCP reachability skip, scratch-role lifecycle and teardown; assert cross-tenant worker claims for tenants A/B, concurrent `SKIP LOCKED` single winner, column/table privilege denial, stale lease token rejection, monotonic token, app tenant isolation, and migrator-only DDL. Verified 2026-10-22 against a fresh disposable PostgreSQL 16 container: stamped 023 and applied migration 024 in isolation over faithful 023-shaped stubs; migration upgrade succeeded, and the opt-in suite returned 5 passed/0 skipped. Actual-migration role spot-checks for coverage RLS, dispatch fencing, support-registry grants, and no-grant denial all passed. The repository suite still uses mirrors for these new tables; permanent actual-migration coverage is tracked in the recommendations ledger.

Phase 0R implementation and actual-role PostgreSQL proof are accepted by owner 2026-10-22 (D1; carry-forwards: live-API enablement gate, platform-owners sign-off under AG-01b/D-01, permanent migration-backed coverage and dual-person DB enforcement per ledger). The disposable PostgreSQL 16 verification applied migration 024 successfully and the opt-in role suite returned 5 passed/0 skipped; the additional role checks for the 024 coverage/support tables passed. This does not enable intake or routes: owner review and the separate unresolved live-API prerequisite remain tracked independently.

**Current Phase B implementation status:** Additive migration 024, ORM grant/fencing/audit models, scoped-grant repository/resolver, verified-principal dependency, claim/requeue/renew/release/reconciliation operations, external role-provisioning SQL, dual-person grant mint/revoke CLI and audit table, and actual-role PostgreSQL test scaffolds are in place. The worker UPDATE check requires `lease_owner IS NULL OR lease_owner = current_setting('iap.worker_id', true)`, and claim/reconcile transactions set that context before changing dispatch rows. Actual-role tests cover audit context/attribution and visibility, exact-scope grant matching and denial cases, and the accepted procedural dual-person limitation. Disposable-database verification additionally applied 024 and checked the new coverage/support table permissions and RLS. The suite still mirrors those newer tables; durable migration-backed tests remain a follow-up. No onboarding routes exist yet; any future route must invoke `authorize_scoped`. Phase E remains interfaces-only until Phase 2 defines ACCEPTED/TERMINAL transitions. The JCS digest carve-out and exact-application/environment v0.1 scope are documented in `docs/IAP-TENANT-ONBOARDING-DB-ROLES.md`.

**Phase B phase-close verification:** Full suite `.venv/bin/python -m pytest -q` returned `109 passed, 88 skipped` before the repository's live-API probe exited with `API not reachable at http://127.0.0.1:8000: Connection refused`. The six dispatcher crash scaffolds remain strict xfails. The actual-role PostgreSQL gate was subsequently closed on 2026-10-22: migration 024 applied in isolation after stamping 023, the opt-in role suite returned 5 passed/0 skipped, and all eight 024-table role spot-checks passed. The unrelated live-API prerequisite remains outstanding; this phase report does not claim it green.

### Session Phase C — Typed intake schema slice (Phase 1B first dependency)

- [ ] Define strict, versioned intake models and typed secret references; forbid extra fields, secret values, filesystem paths, arbitrary URLs, and write-capable references.
- [ ] Add adversarial schema tests for POSIX/Windows paths, credential-like values, URL forms, unknown keys, and reference access modes.
- [ ] Do not implement persistence or routes in this slice; document any schema decisions needed by subsequent routes.
- [ ] Pause for owner review.

### Session Phase D — Remaining v0.1 route/example scaffolding

- [x] Add `config/tenant-id.env.example.yaml` containing safe synthetic values only and no secret-value/path-shaped fields.
- [x] Add tenant onboarding router skeleton with explicit HTTP 501 responses for not-yet-implemented create/read/submit operations; no accepted/durable-success response and no side effects.
- [x] Add route tests proving authentication, tenant mismatch, 501 behavior, typed schema linkage, and example validation without enabling onboarding.
- [x] Pause for owner review — accepted 2026-10-22 (D1).

**Status:** Session Phase D accepted by owner 2026-10-22 (D1); routes remain inert and do not access the application context, persistence, or workflow dispatch.

### Session Phase E — Dispatcher/reconciler skeleton and failure scaffolds

- [x] Keep dispatcher/reconciler interfaces-only until Phase 2 authorizes `ACCEPTED`/`TERMINAL` transitions and defines transactional transition rules. No state-changing dispatcher skeleton before then.
- [x] Add typed workflow start/lookup ports carrying pinned request revision, execution generation, opaque workflow identity, and versioned input reference; no claims, acknowledgements, retries, or lifecycle transitions are implemented in these interfaces.
- [ ] Implement authorized lifecycle transitions together with Phase 2 durable dispatch/reconciler work and required crash/failure proofs.
- [x] Add contract tests proving the interface captures immutable identity and receipts do not carry lifecycle state.
- [ ] Run focused crash/failure tests and phase-close checks; pause for owner review.

**Status:** Interface-only Phase E slice accepted by owner 2026-10-22 (D1); implementation remains blocked on Phase 2 transition/outbox contracts.

### Session test baseline — 2026-10-22 (corrected)

- Baseline command: `uv run --frozen pytest -q`.
- Original result: 5 failed, 104 passed, 86 skipped. All five failures were in `tests/evaluation/test_mcp_fanout.py`.
- Correction: commit `fe611a9` replaced the hand-rolled mock with `AsyncMock(spec=EvidenceDeduplicator)`; the fanout suite is 6/6 green and restored to normal triage. Future failures in that file are regressions, not baseline.
- For reference, the original failure signature was: `_gateway.<locals>.<lambda>() takes 2 positional arguments but 3 were given` when `deduplicate` receives the investigation ID as its third positional argument.
- Retest policy: do not run the full suite after each onboarding change. Use affected focused tests; run broader checks at each owner-review boundary. `pytest` was not on PATH; `uv run --frozen` provides the repo's locked environment.

## Phase 0 — Capability-policy foundation — COMPLETE, REVIEW REQUIRED

Committed as `cb858f7` (`onboarding: D1 capability authorization + matrix tests`). Added the capability vocabulary and pure authorization helper plus matrix tests and architecture scope notes.

- **Known impact:** The helper does not resolve scoped tenant/application/environment/operation grants or current revocation/policy versions. It must be revised in Phase 0R before any intake or privileged route is enabled.
- **Evidence reported at commit (unverified at consolidation):** 10 focused tests passed; full unit suite at that time passed with Ruff and strict mypy clean on touched files.

## Phase 1A — v0.1 control-store schema — COMPLETE, REVIEW REQUIRED

Committed as `797f6a2` (`onboarding: add v0.1 control-store schema`). Added revision 023 with `tenant_registry`, `onboarding_requests`, and `onboarding_dispatches`; ORM metadata, RLS/grants, immutability triggers, chain tests, offline SQL checks, and strict dispatcher failure scaffolds. Migration 023 grants CRUD to `CURRENT_USER`; deployment role equivalence with the runtime role remains unverified. No live database was migrated.

- **Known impacts:** Job references are not fully tenant-composite; no reviewed registry bootstrap/admin path; no lifecycle cleanup fence/epoch; no dispatch lease fencing token; no execution-generation model. These are Phase 0R work, not grounds to claim the schema is production-ready.
- **Evidence reported at commit (unverified at consolidation):** unit suite passed with intentional xfails; Ruff clean; strict mypy clean on the ORM file; offline SQL generated; Alembic head `023_onboarding_control_store`.

## Phase 0R — Implemented-foundation security and concurrency remediation

**Priority:** P0 blocker before intake/dispatch enablement
**Goal:** Bring committed capability authorization and migration 023's data relationships to the scoped security, RLS, and concurrency contracts in the review supplements. This phase repairs already-implemented work before intake or dispatch behavior is enabled.

| Area | Plan |
|---|---|
| Dependencies | Review the current authorization helper, revision 023 ORM/DDL, RLS session/context flow, background-job model, migration ownership/grants, and actual DB-role deployment contract. Security/platform owners approve the registry bootstrap/admin path. |
| Work | Replace unscoped capability-set decisions and caller-supplied operator booleans with verified-principal scoped grants for tenant/app/environment/operation, validity, revocation, and policy version. Recheck at route and privileged worker boundaries. Define separate registry bootstrap/admin access without `BYPASSRLS` or magic tenant values. Add tenant-composite FK/uniqueness for dispatch-to-background-job ownership. Add cleanup fence/lifecycle epoch and dispatcher lease fencing fields/operations. Add execution-generation persistence before same-revision business recovery can be offered. Use additive migrations after 023; keep revisions and durable dispatch intent intact. |
| Deliverables | Scoped grant domain/resolver and audit record; role/policy matrix; reviewed runtime, worker, migration, and registry-admin privilege contract; additive migration/repository changes for tenant-safe job link, lifecycle fence/epoch, dispatch lease token, and execution generation; documented enablement flags and disabled default. |
| Tests | Tenant A grant cannot access B, sibling app, environment, or operation; revoked/stale/expired grants deny; worker-side recheck denies after revocation. Connect to PostgreSQL as actual application/worker/admin roles with `FORCE RLS`; prove policy and composite FK isolation. Race tests prove one fence owner, stale epochs/lease owners cannot write, and old execution generation cannot supersede current state. Fresh-install and upgrade compatibility both pass. |
| Exit criteria | Security/platform owners approve role and bootstrap boundaries; database-role, cross-scope, FK, stale-worker, and fencing tests pass; ORM/migration schemas agree; no intake/dispatch route or worker is enabled before this exit. |
| Rollback | Disable routes/workers; retain registry, request, dispatch, job, audit, and fence/generation records. Any schema rollback is forward-compatible and non-destructive; never delete committed intent or lower a fencing epoch. |

**Implementation-status note:** Phase 0 authorization and revision 023 are committed, but do not yet meet this phase's scoped-grant, registry-admin, tenant-job-FK, fence/epoch, lease-fencing, or execution-generation requirements. Phase 0R is a required repair, not optional future hardening.

## Phase 1B — Typed intake and immutable request revisions

**Priority:** P1; depends on Phases 0R and 1A.
**Goal:** After Phase 0R, use the repaired control-store authority to durably persist safe, immutable tenant configuration intake as DRAFT revisions. Acceptance-to-`QUEUED` (acceptance transaction with outbox + job) is Phase 2; this phase creates no accepted requests, jobs, or dispatch intent.

| Area | Plan |
|---|---|
| Dependencies | Phase 0R/D/E owner reviews accepted (D1); migration 023 and additive remediation migrations applied in an approved test environment; actual-role RLS and registry bootstrap/admin tests pass; provisional v0.1 provider set approved sample-only (D2: `PROVISIONAL_V01_PROVIDER_KEYS` = `sample-git-host`, `sample-database-provider`, transcribed from the shipped example YAML). AG-01b agent identity and the live-API enablement gate are explicitly not 1B entry gates. |
| Work | Reuse the registry/request/dispatch schema as repaired by Phase 0R. Implement typed intake and strict `SecretReference`; reject credential values, filesystem paths, arbitrary URLs, unsupported fields, and write-capable secret references before persistence or network effects. Validate structured provider/repository identifiers against the provisional v0.1 provider set, rejecting unknown provider keys fail-closed. Implement immutable DRAFT revisions, expected-version CAS, canonical request hash, idempotency reservation, and audit linkage. No state transitions beyond DRAFT, no job rows, no dispatch writes, and no 2xx-accepted responses in this phase; the acceptance transaction (request + idempotency + job + outbox + `QUEUED`) belongs to Phase 2. Keep operator YAML as offline capture/export only. Add separate AG-02b coverage linkage fields (request/revision/generation/report ID/content hash) outside the intake payload so request `content_hash` covers intake only. Add additive migration 025 filing audit and tenant-scoped worker insert path; file reports idempotently by tenant/request/revision/generation after recomputing canonical input and content hashes, with FILED attribution audit in the same transaction. |
| Deliverables | Typed intake/domain and response schemas; DRAFT-only tenant reservation/request services (no transitions, no jobs, no dispatch); immutable revision API; destination validation against the provisional provider set (fail-closed); audit event contract; `tenant-id.env.example.yaml` template without secret/path shapes; report linkage model and `SqlAlchemyOnboardingCoverageRepository`; migration 025 filing audit/function and worker filing RLS policy. |
| Tests | Valid/invalid schema cases; secret/path/URL adversarial inputs; redirects/DNS/IP destination tests; stale revision and concurrent update conflicts; same-key/same-body replay and same-key/different-body conflict; coverage report same-generation idempotency, hash tamper and float rejection, generation bump; actual-role tenant isolation, cross-tenant write denial, filing attribution and idempotent refile; scoped role matrix and tenant/app/environment RLS; DRAFT persistence commits revision + idempotency + hash + audit or none; unknown provider-key rejection; provisional-set membership pin; negative tests prove no job rows, no dispatch writes, no state beyond DRAFT, and no 2xx-accepted responses; no path, credential value, arbitrary URL, unsupported field, or write-capable secret ref reaches persistence or side effects. |
| Exit criteria | Invalid requests cause no side effects; DRAFT revisions durable and replay-safe after restart; authorization and database integration gates remain green. Acceptance-to-`QUEUED` is Phase 2. Pause for phase review. |
| Rollback | Disable intake; retain DRAFT revisions and idempotency records. No dispatch intent exists in this phase. |

## Phase 2 — Outbox dispatch, durable workflow, and step ledger

**Priority:** P1 critical reliability; depends on Phases 0R and 1B.
**Goal:** Guarantee durable workflow intent and idempotent recovery.

| Area | Plan |
|---|---|
| Dependencies | Phase 0R/1B control store and job contract (1B delivers durable DRAFT persistence; acceptance starts here); Temporal workflow ID/start semantics verified against current client SDK and existing outbox implementation. |
| Work | Own the acceptance transaction: add dispatch row/outbox write in the same transaction as request revision, idempotency reservation, job row, audit linkage, and `QUEUED` state (acceptance-to-`QUEUED`; 1B persists DRAFT only). Build atomic lease claim/renew/release with fencing token and reconciler. Use deterministic non-sensitive workflow IDs with explicit execution generation; separate redelivery from business retry. Store Temporal Run ID as correlation only. Define retry budgets, state CAS transitions, cancellation signaling/compensation, and projection repair. Direct API-to-Temporal starts are disallowed as authoritative dispatch. |
| Deliverables | Outbox/dispatch migration and repository; dispatcher worker; deterministic workflow definitions; dispatch reconciler; step-ledger activity contracts; operational runbook for dispatch repair. |
| Tests | Follow the workflow correctness specification: inject crashes before transaction commit, after commit/before dispatch, after Temporal start/before outbox acknowledgement, expired/lost lease, duplicate event, `AlreadyStarted`, ambiguous start timeout, Temporal outage, worker restart, terminal-generation retry, projection drift, and cancellation at every side-effect boundary. Prove no workflow starts for rolled-back state; every committed dispatch starts or explicitly escalates; stale workers cannot write; no duplicate provider side effects. |
| Exit criteria | All crash-boundary and retry-generation tests pass; queued request/job without a workflow is detected and repaired; duplicate dispatch is idempotent; dispatch lag, expired leases, retries, terminal failures, and cancellation are observable and alertable. |
| Rollback | Stop accepting new onboarding requests; continue dispatch/reconciliation for already committed outbox rows. Do not delete pending events or abandon accepted workflows. |

## Phase 3 — Adapter registry, actual probes, and readiness

**Priority:** P1
**Goal:** Enable only explicitly supported configuration capabilities and activation gates.

| Area | Plan |
|---|---|
| Dependencies | Phase 0 capability catalog and Phase 1 typed intake/secrets; Phase 2 workflow/ledger. |
| Work | Implement request-scoped adapters for the first supported source classes. Each adapter returns typed stage outcomes, probe provenance, freshness, scope, and redacted diagnostic code. Use scoped secret-store resolution and destination policy. Persist results in the step ledger and catalog-linked readiness report. Define whether each probe is mandatory, optional, or not applicable per supported service tier. |
| Deliverables | Adapter registry, adapters, policy evaluator, readiness report model, source/secret ownership references, operator diagnostic guidance. |
| Tests | Healthy, wrong-tenant, unauthorized, timeout, provider outage, stale credential, partial result, unsupported kind, and empty-but-valid response per adapter. No `VERIFIED` without a successful scoped probe. Probe expiry or policy/catalog change invalidates readiness. |
| Exit criteria | Every mandatory adapter has full contract coverage; each initial profile tier activates only with its mandatory gates; `auto_promote` cannot bypass a required gate; optional adapters skip only by explicit policy. |
| Rollback | Disable individual adapters; affected capability becomes blocked/unsupported and cannot activate. Existing active tenants are not silently downgraded. |

## Phase 4 — Apply, attach, cancellation, and lifecycle operations

**Priority:** P1
**Goal:** Ship the onboarding workflow and profile integration using durable revisions and the specified transition, retry, cancellation, and reconciliation contracts.

| Area | Plan |
|---|---|
| Dependencies | Phases 0R–3; profile generation/seeding contracts reviewed for idempotency and source pinning. |
| Work | Implement submit/apply/attach using pinned request, policy, catalog, profile base revision, repository commit, and adapter versions. Record external references and sanitized step outcomes. Implement retry, cancel, suspend, and resume only as guarded transitions. Reconcile uncertain provider results by stable key before retry. Compensation is a separate idempotent action. |
| Deliverables | Onboarding workflows, service composition, REST routes, thin CLI, status model, per-step recovery and operator runbook. |
| Tests | Concurrent identical request replay; same key/different payload conflict; concurrent attach no lost update; profile generation or seed crash at every side-effect boundary; provider success followed by local failure; timeout after provider success; cancel during each stage; policy/intake mutation invalidates affected approval and requires new revision; stale workflow cannot activate after suspension, retirement, cleanup fence, or epoch change. |
| Exit criteria | End-to-end synthetic tenant reaches readiness and activation; retry does not duplicate resources; no active profile implies tenant active without explicit activation; all failed/partial paths have clear status and operator action. |
| Rollback | Stop new requests and drain/cancel according to workflow policy. Keep pinned definitions and request records until in-flight work is reconciled. |

## Phase 5 — Store registry, deletion manifest, approval, and restore proof

**Priority:** P1 destructive-flow gate
**Goal:** Prove the attested deployment inventory is covered and every deletion action is scoped, authorized, recoverable, and auditable.

| Area | Plan |
|---|---|
| Dependencies | Store owners provide inventory, retention/hold rules, scope identifiers, and adapter feasibility; Phase 1 identity/capabilities; Phase 2 outbox/worker; approved backup/restore environment. |
| Work | Derive the versioned store registry from attested deployment inventory and continuously detect drift. Implement typed per-store/resource plans, enumeration modes, completeness markers, lifecycle fence/epoch, and final re-enumeration. Canonicalize manifests using the versioned RFC 8785 contract and golden vectors. Persist token digest, canonical payload digest, authenticated requester/approver, scope, expiry, nonce, policy versions, and consume linkage. Conditionally consume once in the same transaction as purge job, audit, and outbox intent. Define distinct complete, retained, approved-residual, incomplete, blocked, and canceled outcomes. Generate and verify backup before purge. Persist append-only per-resource outcomes; shared-reference resources detach/reassign rather than delete while referenced. |
| Deliverables | Deployment inventory attestation and drift reconciler; store registry migration/adapters; manifest schema/canonicalizer and golden vectors; approval persistence/consumer; deletion workflow; retention/backup policy; cleanup and restore runbooks. |
| Tests | Reconcile deployment inventory with registry; omitted/new component and drift block approval; partial enumeration, stale inventory, timeout, legal hold, backup failure, fence races, plan/policy/registry mutation, wrong tenant/app, wrong authenticated approver, self-approval, raw-token secrecy, expiry/replay, concurrent database approval, shared resources, object versions, caches, indexes, graph projections, schedules/workflows, audit retention, and each residual/retention path. At most one purge workflow/job/outbox intent commits. |
| Exit criteria | Every required store/class has owner and complete evidence or a documented supported revocation/expiry guarantee; canonical hash vectors pass; every entry reaches an explicit honest terminal outcome; no residual is described as erased; restore drill passes; legal holds and retained backups behave per policy; production targets in the assurance spec are approved and met. |
| Rollback | Disable new cleanup requests and stop workers before destructive stages. Preserve manifests, approvals, tombstones, outcomes, and backup references for forward recovery. Never “rollback” by deleting the audit/manifest. |

## Phase 6 — Contract conformance, scale, and production operations

**Priority:** P1 production gate
**Goal:** Validate the module under supported workloads and provide operations evidence.

| Area | Plan |
|---|---|
| Dependencies | Phases 0, 0R, 1A, 1B, and 2–5; product workload assumptions; SRE/security/data-owner review and approved target values. |
| Work | Run CLI/REST conformance under JWT authentication; establish per-principal/tenant/global quotas, fair scheduling, bounded request sizes, provider concurrency, retry/backoff, circuit breakers, dashboards, alerts, SLOs, RTO/RPO, retention, support access policy, and deployment/rollback procedure. Test app-role RLS for all new tables and tenant context across events, jobs, schedules, caches, search, artifacts, and provider adapters. |
| Deliverables | Threat-model test results, load-test report, dashboards/alerts, runbooks, approved SLI/SLO and recovery objectives, architecture documentation, deployment/rollback playbook. |
| Tests | Agreed peak/burst load, noisy-neighbor isolation, backlog recovery, dependency outage, regional/database outage, outbox catch-up, stuck workflow repair, cleanup partial failure, restore/re-delete behavior, and end-to-end tenant A/B isolation. |
| Exit criteria | Security and data owners approve isolation and deletion evidence; SRE accepts SLO/RTO/RPO and runbooks; load tests meet agreed targets; no Critical/High unresolved finding remains for the enabled capability set. |
| Rollback | Disable feature flags per capability while preserving recoverable workflows and audit. Never remove schema needed by in-flight requests; use forward-compatible migrations and drain workflows before destructive rollback. |

## Dependency Order

`Phase 0 (complete) → Phase 1A (complete) → Phase 0R → Phase 1B → Phase 2 → Phase 3 → Phase 4 → Phase 5 → Phase 6` is the safe default. Review the completed Phase 0/1A status and Phase 0R evidence before starting Phase 1B. Review each subsequent phase before beginning the next. Phase 3 adapter work may be prototyped against typed contracts in parallel with Phase 2, but cannot dispatch production jobs or establish readiness before the workflow ledger and authorization gates exist. Phase 5 planning/schema design can proceed with store owners during earlier phases, but no purge is enabled before Phase 0R, Phase 4, authoritative inventory, atomic approval, and restore evidence. Phase 6 qualifies the measurable production targets; targets must be approved and met before rollout. No phase may skip the prior acceptance gate for convenience.

## Traceability

| Review item | Design decision | Plan phase | Required proof |
|---|---|---|---|
| Capability completeness (F-20) | D2 catalog | 0, 1B, 3, 6 | Catalog coverage, role/state matrix, profile-tier tests |
| Durable dispatch (F-21) | D6 normative outbox | 0R, 1B, 2, 4 | Crash-window, duplicate-start, reconciliation tests |
| Complete store coverage (F-22) | D7 store registry/manifest | 5 | Registry-to-manifest equality and per-entry terminal outcomes |
| Approval token binding (F-23) | D8 canonical payload + random one-time token | 0R, 5 | Canonical payload golden; authenticated approver binding; token secrecy; tamper/expiry/replay/concurrency tests |
| Tenant authorization and bootstrap | D1 | 0, 0R, 1B, 6 | Role matrix, self-claim and IDOR tests |
| Readiness and activation | D4/D5 | 1B, 3, 4 | Real scoped probes and no activation before all required gates |
| Review F-01/F-05/F-09/F-10 | Security/concurrency supplement | 0R, 1B, 6 | Scoped grant/revocation tests; actual-role FORCE RLS; tenant-composite FK and registry-admin proof |
| Review F-03/F-08/F-13 | Security supplement + workflow correctness specification | 0R, 2, 4 | Fencing, CAS, retry-generation, cancellation, lease-loss and reconciliation tests |
| Review F-02/F-06/F-07/F-14/F-15 | Deployment/deletion assurance specification | 5, 6 | Inventory drift, canonical hash vectors, database approval race, terminal residual and measurable target evidence |
| Scoped grants, role/RLS, tenant-safe FKs, cleanup fence, lease fence, execution generation | Security/concurrency supplement | 0R, 1B, 2, 4, 5 | Actual-role PostgreSQL tests, revoke/race tests, stale-token rejection, no unaccounted side effects |
| Outbox recovery, retry semantics, state CAS, cancellation, reconciliation | Workflow correctness specification | 2, 4, 6 | Crash-window, duplicate-start, generation, cancellation, reconciliation and projection-repair tests |
| Deployment inventory, JCS manifest, residual semantics, measurable SLOs | Deployment/deletion assurance specification | 5, 6 | Drift reconciliation, canonical golden vectors, database approval race, restore and target-qualified load evidence |

## Deferred Items

Self-service onboarding, tenant migration, dedicated/hybrid infrastructure, customer identity or billing orchestration, and unsupported provider adapters are deferred. Each requires a named owner, capability-catalog update, threat model, data ownership contract, operational runbook, and review of impact on activation/deletion before it enters scope.

## Do-Not-Do List

- Do not start Temporal directly from the onboarding API outside the transactional outbox dispatcher.
- Do not assume exactly-once message or provider delivery; use idempotency, discovery, and reconciliation.
- Do not approve a manifest with incomplete inventory or an unregistered required store.
- Do not hash a different approval payload during token creation and validation; do not rely on a digest as authorization.
- Do not report `DEPROVISIONED` while required store outcomes are missing or residuals are unresolved.
- Do not delete customer-owned external data, shared resources with live references, or audit records without an explicitly approved retention policy.
- Do not let tenant-claim equality replace capability authorization or separation-of-duties checks.
- Do not add customer identity, subscriptions, billing, DNS, certificates, infrastructure, or network provisioning to IAP under this plan.
