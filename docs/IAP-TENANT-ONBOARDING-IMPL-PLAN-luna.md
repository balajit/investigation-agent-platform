# Implementation Plan: IAP Tenant Configuration Onboarding — Luna Revision

Companion to [IAP-TENANT-ONBOARDING-DESIGN-Luna.md](./IAP-TENANT-ONBOARDING-DESIGN-Luna.md). This plan incorporates the focused architecture review of capability completeness, workflow dispatch, deletion-store coverage, and approval-token correctness. It is a sequence and acceptance contract, not implementation authorization. Do not commit, migrate a live database, purge data, deploy, or execute external onboarding without the required explicit approval.

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

## Phase 0 — Scope, capability catalog, and contract baseline

**Priority:** P1 foundation
**Goal:** Freeze the product boundary and make supported behavior traceable before persistence or side effects are introduced.

| Area | Plan |
|---|---|
| Dependencies | Security/product/platform owners confirm IAP-only scope, supported tenant profiles, initial actor roles, data owners, environments, and required source classes. |
| Work | Create the catalog schema and initial entries for tenant reservation, intake revision, approval, repository/profile, logs, traces, metrics, datasources, config servers, read-only knowledge, activation, retry, cancel, suspend/resume, cleanup-plan, cleanup-approval, and status. Map each to owner, scope, capability, required/optional policy, transition, probe/freshness, recovery, and cleanup action. Define tenant/request/profile/job state ownership and role × operation matrix. Label any non-executable API example explicitly. |
| Deliverables | Capability catalog; lifecycle/transition table; owner/source-of-truth matrix; explicit initial support table (code-only, telemetry-enabled, datasource-enabled); environment eligibility assumptions. |
| Tests | Each intake capability has a catalog entry; unsupported capability fails before side effects; role matrix covers ordinary tenant, onboarding operator, approver, cleanup operator, support/break-glass, and bootstrap principal; invalid state transitions fail. |
| Exit criteria | Product/security sign off on module scope and role boundaries. Every supported capability maps to an owner, API/CLI operation, state, readiness contract, and deletion action. Unknown capability and unreserved tenant are rejected. |
| Rollback | Documentation and validation-only additions can be disabled; no data or external system side effects exist in this phase. |

## Phase 1 — Shared control store, immutable revisions, and authorization

**Priority:** P1
**Goal:** Establish shared durable authority and request identity.

| Area | Plan |
|---|---|
| Dependencies | Phase 0 approved; current Alembic head and RLS/app-role coverage inspected. |
| Work | Add tenant registry/reservation, onboarding request revision, approval linkage, and control-store repositories. Add RLS and app-role grants. Persist exact policy/catalog/schema versions, actor, content hash, and expected revision. Keep operator YAML as offline import/export only. Wire capability checks to every route and repeat authorization before privileged workflow activities. |
| Deliverables | Additive migration(s); tenant registry and request service; typed intake/response schemas; auth policy and bootstrap reservation flow; immutable revision API; audit event contract. |
| Tests | Multi-replica consistency; unique reservations under concurrency; slug aliases/tombstones; stale revision conflict; ordinary/privileged role matrix; tenant A/B IDOR and RLS adversarial tests; no path, credential value, arbitrary URL, or unsupported field reaches persistence. |
| Exit criteria | API and worker resolve the same immutable revision after restart; duplicate reservation has one winner; capability checks bind principal/tenant/app/environment/operation; all new tenant tables have tested RLS and app-role grants. |
| Rollback | Feature-flag routes off; retain registry/request records; existing profiles are unchanged. No silent file-based fallback for workers. |

## Phase 2 — Outbox dispatch, durable workflow, and step ledger

**Priority:** P1 critical reliability
**Goal:** Guarantee durable workflow intent and idempotent recovery.

| Area | Plan |
|---|---|
| Dependencies | Phase 1 control store and job contract; Temporal workflow ID/start semantics verified against current client SDK and existing outbox implementation. |
| Work | Add dispatch row/outbox write in the same transaction as request revision, idempotency reservation, job row, audit linkage, and `QUEUED` state. Build leased dispatcher and reconciler. Use deterministic workflow ID per `(tenant_id, request_id, revision)` and store accepted Temporal run ID. Add durable step ledger keyed by request revision, step name/version, and idempotency key. Direct API-to-Temporal starts are disallowed as authoritative dispatch. |
| Deliverables | Outbox/dispatch migration and repository; dispatcher worker; deterministic workflow definitions; dispatch reconciler; step-ledger activity contracts; operational runbook for dispatch repair. |
| Tests | Inject crashes before transaction commit, after commit/before dispatch, after Temporal start/before outbox acknowledgement, expired lease, duplicate event, `AlreadyStarted`, Temporal outage, and worker restart. Prove no workflow starts for rolled-back state; every committed dispatch is started or explicitly escalated; one logical revision maps to one workflow identity; no duplicate provider side effects. |
| Exit criteria | All crash-boundary tests pass; queued request/job without a workflow is detected and repaired; duplicate dispatch is idempotent; dispatch lag, expired leases, retries, and terminal failures are observable and alertable. |
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
**Goal:** Ship the onboarding workflow and profile integration using durable revisions and recovery.

| Area | Plan |
|---|---|
| Dependencies | Phases 1–3; profile generation/seeding contracts reviewed for idempotency and source pinning. |
| Work | Implement submit/apply/attach using pinned request, policy, catalog, profile base revision, repository commit, and adapter versions. Record external references and sanitized step outcomes. Implement retry, cancel, suspend, and resume only as guarded transitions. Reconcile uncertain provider results by stable key before retry. Compensation is a separate idempotent action. |
| Deliverables | Onboarding workflows, service composition, REST routes, thin CLI, status model, per-step recovery and operator runbook. |
| Tests | Concurrent identical request replay; same key/different payload conflict; concurrent attach no lost update; profile generation or seed crash at every side-effect boundary; provider success followed by local failure; timeout after provider success; cancel during each stage; policy/intake mutation invalidates affected approval and requires new revision. |
| Exit criteria | End-to-end synthetic tenant reaches readiness and activation; retry does not duplicate resources; no active profile implies tenant active without explicit activation; all failed/partial paths have clear status and operator action. |
| Rollback | Stop new requests and drain/cancel according to workflow policy. Keep pinned definitions and request records until in-flight work is reconciled. |

## Phase 5 — Store registry, deletion manifest, approval, and restore proof

**Priority:** P1 destructive-flow gate
**Goal:** Prove every store is inventoried and every deletion action is scoped, authorized, recoverable, and auditable.

| Area | Plan |
|---|---|
| Dependencies | Store owners provide inventory, retention/hold rules, scope identifiers, and adapter feasibility; Phase 1 identity/capabilities; Phase 2 outbox/worker; approved backup/restore environment. |
| Work | Define a versioned deployment store registry with required/enabled status and enumerate/plan/apply/verify adapter contract. Implement typed per-store/resource plans and completeness markers. Implement canonical approval payload containing manifest hash, scope, registry/policy versions, requester, approver, expiry, and nonce; use identical canonicalization in mint/validate; atomically consume once. Generate and verify backup before purge. Persist append-only outcomes and residuals. Ensure shared-reference resources detach/reassign rather than delete while referenced. |
| Deliverables | Store registry migration and adapters; manifest schema/version; approval persistence and consumer; deletion workflow; retention/backup policy; store inventory catalog; cleanup and restore runbooks. |
| Tests | Registry-vs-manifest coverage equality; unregistered required store, partial enumeration, stale inventory, timeout, legal hold, backup failure, plan mutation, policy change, wrong tenant, wrong authenticated approver, self-approval, requester/approver separation, raw-token secrecy, token expiry/replay, concurrent approval, sibling app, shared resource, object version, cache key, search index, graph projection, schedule/workflow, local artifact, audit retention, and residual paths. At most one purge workflow starts. |
| Exit criteria | Every required store returns complete inventory at preview and execution; plan hash covers all entries and policy versions; every entry reaches explicit terminal action; no residual is reported complete; restore drill passes; legal holds and retained backups behave according to policy. |
| Rollback | Disable new cleanup requests and stop workers before destructive stages. Preserve manifests, approvals, tombstones, outcomes, and backup references for forward recovery. Never “rollback” by deleting the audit/manifest. |

## Phase 6 — Contract conformance, scale, and production operations

**Priority:** P1 production gate
**Goal:** Validate the module under supported workloads and provide operations evidence.

| Area | Plan |
|---|---|
| Dependencies | Phases 0–5; product workload assumptions; SRE/security/data-owner review. |
| Work | Run CLI/REST conformance under JWT authentication; establish per-principal/tenant/global quotas, fair scheduling, bounded request sizes, provider concurrency, retry/backoff, circuit breakers, dashboards, alerts, SLOs, RTO/RPO, retention, support access policy, and deployment/rollback procedure. Test app-role RLS for all new tables and tenant context across events, jobs, schedules, caches, search, artifacts, and provider adapters. |
| Deliverables | Threat-model test results, load-test report, dashboards/alerts, runbooks, approved SLI/SLO and recovery objectives, architecture documentation, deployment/rollback playbook. |
| Tests | Agreed peak/burst load, noisy-neighbor isolation, backlog recovery, dependency outage, regional/database outage, outbox catch-up, stuck workflow repair, cleanup partial failure, restore/re-delete behavior, and end-to-end tenant A/B isolation. |
| Exit criteria | Security and data owners approve isolation and deletion evidence; SRE accepts SLO/RTO/RPO and runbooks; load tests meet agreed targets; no Critical/High unresolved finding remains for the enabled capability set. |
| Rollback | Disable feature flags per capability while preserving recoverable workflows and audit. Never remove schema needed by in-flight requests; use forward-compatible migrations and drain workflows before destructive rollback. |

## Dependency Order

`Phase 0 → Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5 → Phase 6` is the safe default. Phase 3 adapter work may be prototyped against typed contracts in parallel with Phase 2, but cannot dispatch production jobs or establish readiness before the workflow ledger and authorization gates exist. Phase 5 planning/schema design can proceed with store owners during earlier phases, but no purge is enabled before Phase 4 and restore evidence. No phase may skip the prior acceptance gate for convenience.

## Traceability

| Review item | Design decision | Plan phase | Required proof |
|---|---|---|---|
| Capability completeness (F-20) | D2 catalog | 0, 3, 6 | Catalog coverage, role/state matrix, profile-tier tests |
| Durable dispatch (F-21) | D6 normative outbox | 2, 4 | Crash-window, duplicate-start, reconciliation tests |
| Complete store coverage (F-22) | D7 store registry/manifest | 5 | Registry-to-manifest equality and per-entry terminal outcomes |
| Approval token binding (F-23) | D8 canonical payload + random one-time token | 5 | Canonical payload golden; authenticated approver binding; token secrecy; tamper/expiry/replay/concurrency tests |
| Tenant authorization and bootstrap | D1 | 0, 1, 6 | Role matrix, self-claim and IDOR tests |
| Readiness and activation | D4/D5 | 3, 4 | Real scoped probes and no activation before all required gates |

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
