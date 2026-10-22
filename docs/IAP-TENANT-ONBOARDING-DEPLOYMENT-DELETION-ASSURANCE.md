# Tenant Onboarding Deployment and Deletion Assurance Specification

**Status:** Design contract and production qualification gate. This specification refines D7–D10 in [the Luna onboarding design](./IAP-TENANT-ONBOARDING-DESIGN-Luna.md). Metrics are proposed initial qualification thresholds; service owners must approve them or publish justified replacements before production enablement.

## 1. Deployment inventory is the source of truth

A store registry is evidence of what has been registered, not proof that registration is complete. Completeness is established by reconciling it to an authoritative deployment inventory generated from version-controlled deployment configuration and runtime component discovery. The inventory records component identity/version, data classes, tenant-scoping mechanism, owner, environment, retention authority, deletion/expiry mechanism, enumeration capability, and the store adapter responsible.

The deployment inventory is versioned and signed/attested by the deployment pipeline. The cleanup store registry is a derived, reviewed projection of that inventory, not a hand-maintained independent list. Registry updates require owner review, schema/adapter coverage, and an auditable change record. Deployment admission fails closed if a data-bearing component is absent from the registry projection, lacks an owner, or declares a data class with no lifecycle treatment.

### 1.1 Authoritative inventory classes

For every enabled deployment, inventory coverage must explicitly consider:

| Class | Minimum coverage evidence |
|---|---|
| Relational stores | Tables, partitions, indexes/materialized copies, tenant/app/environment keys, FK relationships, RLS policy, backup/replica retention. |
| Object/artifact stores | Buckets, prefixes, versions, multipart/incomplete uploads, derived artifacts, replication and immutable-retention rules. |
| Search/vector/graph projections | Index/collection/dataset, alias, generation, graph projection, derived chunks/embeddings and shared-reference semantics. |
| Messaging/event stores | Topics/queues, partition/key strategy, retention horizon, consumer offsets, DLQ and replay behavior. Messages may expire by policy rather than exact per-message deletion. |
| Workflow/job stores | Temporal executions/history/schedules/signals, job read models, dispatch/outbox and step ledger. Workflow history may be retained under a separately approved policy. |
| Cache and ephemeral stores | Key namespace, TTL, invalidation/purge API, replicas, snapshots and maximum stale-access window. |
| Credentials and access | Secret references/grants, provider tokens, role bindings, credentials rotation/revocation evidence; secret values are never copied into manifests. |
| Backups and replicas | Backup sets, snapshots, replicas, retention lock, key destruction policy, restore test and expiry; distinguish logical access revocation from physical expiry. |
| Audit and compliance records | Required retained fields, access restrictions, lawful retention basis and expiry date; do not promise deletion where policy mandates retention. |
| Operator exports/configuration | Deployment-approved export destinations, scope ownership, credential-free content, version and retention; local user files are not implicitly enumerable by the shared service. |

External customer-owned systems are represented only to record IAP access revocation or an authorized customer-owned action. IAP does not claim physical deletion from those systems absent a separate product/data-ownership contract.

### 1.2 Drift detection

At deployment and continuously at a bounded interval, compare declared deployment components with registry entries and compare registered adapters with deployed versions. A missing, disabled-required, unexpected, version-drifted, or unowned component blocks new cleanup approval. During an active cleanup, inventory drift invalidates approval and requires a new manifest; it cannot silently inherit the prior plan.

An adapter may declare `EXACT_ENUMERATION`, `SCOPED_REVOCATION`, `POLICY_EXPIRY`, or `UNSUPPORTED`. `UNSUPPORTED` cannot satisfy required coverage. Non-exact classes must state a bounded expiry/revocation guarantee and evidence. Unknown enumeration semantics block approval.

## 2. Inventory and manifest lifecycle

1. Acquire the tenant/app/environment cleanup fence and lifecycle epoch before final inventory; drain or reconcile in-flight work as specified in the [security/concurrency supplement](./IAP-TENANT-ONBOARDING-SECURITY-CONCURRENCY.md).
2. Freeze the deployment inventory and store-registry versions for this cleanup attempt. Record the runtime deployment attestation and inventory timestamp/window.
3. Query every registry-required adapter in that deployment. Each response declares scope, adapter/version, enumeration mode, completeness, start/end times, cursor/final-page marker, counts by resource class, and bounded diagnostic code.
4. Reject duplicate resource identities, ambiguous ownership, mismatched tenant/app/environment, unknown resource class, incomplete pagination, stale snapshots, and entries outside the frozen scope.
5. Build a complete immutable manifest. Empty enumeration is valid only with explicit complete enumeration evidence for every applicable class; absent adapter response is never interpreted as an empty store.
6. Apply policy for holds, backups, shared references, customer-owned resources, retention, access revocation, and physical expiry. Then compute the canonical hash and bind approval to it.
7. At execution, verify the exact frozen versions/hash and repeat required drift, hold, and fence checks before each destructive stage. Newly discovered resources invalidate the plan and require a new manifest/approval or a versioned appended reconciliation plan with renewed approval.

## 3. Manifest canonicalization and integrity

### 3.1 Versioned canonical format

Every manifest declares `canonicalization_version`, `manifest_schema_version`, and hash algorithm (`SHA-256`). The canonical JSON representation follows RFC 8785 (JCS) over the validated JSON-compatible manifest core, with domain-separated hashing: `SHA-256("IAP-TENANT-ONBOARDING-MANIFEST\0" || version || 0x00 || canonical_bytes)`.

Before canonicalization:

- reject duplicate JSON property names, invalid Unicode, non-finite numbers, unsupported types, unknown fields, and out-of-range values;
- represent large integers and decimal quantities as schema-defined strings rather than relying on cross-runtime floating-point behavior;
- normalize all timestamps to UTC RFC 3339 with fixed precision and `Z`; reject timezone-naive timestamps;
- normalize identifiers only where the field schema explicitly defines normalization; never normalize opaque provider/resource IDs;
- serialize optional values using one defined representation (either always present with `null` or omitted per schema; each version fixes the choice);
- sort unordered collections by stable schema-defined keys before serialization. Preserve order only where order is semantically meaningful and declared so by the schema;
- reject duplicate logical keys such as `(store_id, resource_type, resource_ref)` after the specified identifier normalization.

JCS sorts object keys and preserves array order; therefore collection ordering is an explicit application-level step, not delegated to JCS. The same versioned library/function and golden vectors are used at plan creation, persistence verification, approval validation, and execution-time verification. Hashes are integrity evidence only, never authorization.

### 3.2 Hashed fields

The manifest hash covers: tenant/app/environment scope; cleanup lifecycle epoch; manifest ID and plan version; canonical resource entries and their ownership/action; full enumeration coverage evidence; deployment inventory and store-registry versions; adapter and policy versions; retention/hold/backup disposition; requester and required approval policy; creation/expiry timestamps; and canonicalization/schema versions. It excludes only the hash field itself and explicitly designated mutable execution outcomes. Outcomes are append-only and reference the immutable manifest hash plus entry key.

Approval payload has its own version and binds manifest hash, exact scope, requester, authenticated approver, capability/policy version, store-registry and deployment-inventory versions, expiry, nonce, and approval ID. Token/payload hashes use the same canonicalization rules. Any change in a hashed field invalidates the approval.

### 3.3 Golden tests

Golden vectors cover reordered JSON object keys, reordered unordered entries, preserved ordered entries, optional/null handling, UTC timestamp equivalents, Unicode, numeric boundaries, duplicate resource keys, schema-version changes, and mutations to every security-relevant field. Known vectors must match across all supported runtimes/implementations. Hash compatibility is preserved by keeping old canonicalization versions available for verification of existing manifests.

## 4. Per-resource outcome and terminal semantics

Each immutable manifest entry receives append-only attempts with actor/adapter version, action, bounded result code, started/completed timestamps, idempotency key, and redacted completion evidence. The manifest itself is never rewritten to record execution progress.

| Overall terminal/status | Meaning | Required evidence |
|---|---|---|
| `COMPLETE` | All applicable entries have the requested action verified; no residual resource remains in the tenant access boundary. | Per-entry verified completion; final re-enumeration or equivalent provider proof; fence still valid at finalization. |
| `COMPLETE_WITH_RETENTION` | Remaining resources are explicitly required/allowed by retention policy and inaccessible to the tenant through IAP. Physical expiry is scheduled and tracked. | Retention authority/reason, data classes, expiry date, access-revocation proof, owner, recheck schedule, and final report that clearly says data is retained. |
| `COMPLETE_WITH_APPROVED_RESIDUALS` | Specific entries could not be physically removed but a named authorized exception permits the residual. | Entry-specific residual code, reason, owner, risk/access boundary, expiry/review date, required approver(s) distinct from requester, and customer-visible disclosure where required. Never describe this as physical erasure. |
| `INCOMPLETE` | Enumeration, action, verification, or reconciliation remains outstanding or has an unknown result. | Outstanding entry state and retry/next action; cleanup fence remains or is explicitly transferred to recovery ownership. |
| `BLOCKED` | Authorization failure, legal hold, inventory drift, policy denial, backup failure, or unsupported required store prevents safe work. | Stable blocker code, scope, responsible owner, and remediation condition. No success claim. |
| `CANCELED` | Cleanup was canceled before irreversible actions or after a recorded partial execution. | Per-entry completed/retained/residual outcomes and explicit access state; cancellation is not equivalent to restoration. |

An entry may terminate as `VERIFIED_ACTION`, `RETAINED_BY_POLICY`, or `APPROVED_RESIDUAL`, each with evidence. `UNKNOWN`, `RETRYABLE`, `IN_PROGRESS`, or missing entries are nonterminal. A successful API response must use the exact overall status and never collapse retention/residual/incomplete into a generic success Boolean.

## 5. Production acceptance criteria

Targets below are initial qualification proposals, not claims about current performance. Product, Security, Data Owners, and SRE approve or replace each target with a documented rationale before production enablement. Measure per tenant and globally; never expose raw resource IDs or sensitive payloads in metrics.

| Measure | Proposed initial target | Measurement/alert |
|---|---|---|
| Accepted-request durability | 100% of committed acceptance transactions have request, idempotency, job, audit linkage, and dispatch intent; zero accepted-but-unrecoverable records in failure injection. | Transaction invariant tests; periodic consistency scan; any mismatch is a critical alert. |
| Dispatch latency | p95 commit-to-Temporal-start ≤ 30 seconds and p99 ≤ 2 minutes under qualified load. | Histogram by bounded environment/tier; alert on sustained p95/p99 breach. |
| Dispatch recovery | Every expired lease or due unaccepted dispatch is detected within 2 minutes; no item remains silently stuck. | Oldest due age, expired-lease count; page when age > 5 minutes or count persists. |
| Outbox availability/recovery | 100% of committed test intents resolve to started or explicitly terminal/escalated after injected DB/Temporal outages; catch-up completes within 15 minutes after dependency recovery at qualified load. | Chaos/failure replay plus backlog drain test. |
| Workflow retries | Retry budget and max elapsed age are finite and versioned; zero duplicate provider resource creation across retry/failure suite. | Step idempotency audits and provider-fixture inventory comparison. |
| Fairness | One tenant cannot consume >20% of worker concurrency for over 5 minutes unless its configured quota permits it; p99 queue age per supported tier ≤ 5 minutes at agreed peak load. | Per-tenant queue age/concurrency; synthetic noisy-neighbor load. |
| Tenant isolation | Zero cross-tenant read/write/reference successes across role-based REST, repository, worker, cache, search, event, and operator negative probes. | CI integration gate on deployed-equivalent roles; production canary probes without sensitive payloads. |
| Approval atomicity | Exactly one purge job/outbox intent commits under at least 20 simultaneous attempts for one approval; zero success on replay/expiry/tamper/wrong approver. | Database concurrency test and replay/tamper suite. |
| Inventory coverage | Every deployed data-bearing component maps to an owned registry entry and lifecycle class; 100% required adapters respond completely or approval is blocked. | Deployment drift reconciliation; any uncovered component blocks release/cleanup. |
| Cleanup correctness | Zero `COMPLETE` reports with missing entries, unverified actions, active tenant access, or unresolved outcomes. | Final re-enumeration, access probes, manifest/outcome consistency checks. |
| Cleanup duration | p95 ≤ 24 hours for the published supported-scope envelope; larger scopes require declared bounds before acceptance. | Per-scope duration distribution and backlog aging; tune only from owner-approved capacity evidence. |
| Recovery objectives | Control-store RPO ≤ 5 minutes and RTO ≤ 60 minutes; outbox recovery and manifest/approval restoration rehearsed quarterly. | Backup restore drill and reconciliation replay report. |
| Observability | 100% of dispatch, transition, approval, fence, and outcome events carry correlation IDs and bounded reason codes; zero secrets/raw tokens/provider payloads in sampled logs. | Schema validation, secret-pattern test, and sampled log audit. |

Thresholds are gates: violating a target blocks the affected capability or cleanup scope until a corrective decision and requalification are recorded. No target is waived by turning a failed test into a skip or by reporting only aggregate averages.

## 6. Sign-off and deployment gates

Before enabling onboarding intake, dispatch, or cleanup, the release record includes:

- deployed inventory attestation reconciled to store registry and adapters;
- role/grant/RLS matrix and integration results for runtime, worker, migration, registry-admin, and operator principals;
- authorization revocation and cross-tenant/app/environment negative test results;
- migration upgrade and fresh-install compatibility evidence, without running destructive rollback against durable intent;
- workflow crash-window, retry-generation, cancellation, and reconciliation results;
- canonicalization golden vectors and database concurrent-approval proof;
- backup restore evidence and cleanup residual/retention report examples;
- load, fairness, dispatch-latency, recovery, and SLO results against approved targets;
- named Product, Security, Data Owner, and SRE approvers with operational ownership and rollback/drain procedures.

## References

- [RFC 8785: JSON Canonicalization Scheme](https://www.rfc-editor.org/rfc/rfc8785)
- [Security and concurrency supplement](./IAP-TENANT-ONBOARDING-SECURITY-CONCURRENCY.md)
- [Workflow correctness specification](./IAP-TENANT-ONBOARDING-WORKFLOW-CORRECTNESS.md)
