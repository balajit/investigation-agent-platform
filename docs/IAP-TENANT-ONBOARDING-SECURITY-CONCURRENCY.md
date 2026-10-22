# Tenant Onboarding Security and Concurrency Supplement

**Status:** Design contract; not an assertion that the listed controls are implemented. This supplement refines D1, D7, D8, and D10 in [the Luna onboarding design](./IAP-TENANT-ONBOARDING-DESIGN-Luna.md). The current implementation status and required repair phase are tracked in [the Luna implementation plan](./IAP-TENANT-ONBOARDING-IMPL-PLAN-luna.md).

## 1. Security invariants

1. Authorization is a decision over a verified principal, requested capability, canonical tenant, application, environment, operation, and current policy/grant version. A capability name or tenant claim alone never grants access.
2. Authorization is repeated at API entry and immediately before each privileged workflow side effect. A worker uses trusted persisted scope and a fresh policy decision; it does not trust a caller-supplied operator flag or authorization result copied into an intake payload.
3. Missing, expired, revoked, stale, or unavailable authorization data denies the operation. A decision records principal, exact scope, capability, policy version, decision time, and stable reason code in the audit trail.
4. PostgreSQL RLS is defense in depth. Repositories also bind tenant and, where present, application/environment scope. Cross-tenant references are rejected by database constraints, not merely by service convention.
5. Cleanup fences mutations before final inventory and keeps the fence through every destructive action and final reconciliation.
6. An approval can be consumed at most once. Consumption, purge-job creation, audit linkage, and purge outbox intent commit in one database transaction or none commit.

## 2. Scoped authorization contract

### 2.1 Inputs and grants

The authenticated principal is derived from a verified identity token or trusted identity service. The requested capability is selected by the server-side operation, never accepted as an authorization claim from the request body. The resource scope is resolved from the canonical tenant registry and persisted request/manifest, not from a display slug.

A grant is a versioned, server-managed record with at least:

| Field | Contract |
|---|---|
| `principal_id` | Stable verified subject identifier. |
| `capability` | Closed enum for the operation being granted. |
| `tenant_id` | Exact canonical tenant or an explicitly defined platform scope. |
| `application_id` | Exact application or explicit all-applications scope. |
| `environment` | Exact environment or explicit all-environments scope. |
| `operation` | Exact operation identifier; it is not inferred from a broad capability string. |
| `not_before`, `expires_at` | Time-bounded validity evaluated against server time. |
| `revoked_at` | Immediate deny after revocation. |
| `policy_version`, `grant_version` | Versions included in the decision and audit evidence. |
| `issuer`, `reason`, `delegation_id` | Trusted grant provenance and review trail. |

Wildcard scope is not implicit. A grant schema must encode all-scope values explicitly, and policy must restrict who can issue them. No generic platform-operator Boolean is an authorization input. Platform administration is represented by narrowly scoped, time-bound grants for named operations; break-glass additionally requires a reason, expiry, immutable audit, and action restrictions.

### 2.2 Decision and revalidation

The policy resolver performs exact matching over principal, capability, tenant, application, environment, operation, validity interval, revocation state, and policy version. A decision is allowed only if all required fields match and the grant remains current. Separation of duties requires an authoritative, non-null requester identity and a distinct authenticated approver wherever policy requires dual control.

Revalidate before reservation, intake acceptance, dispatch authorization, approval, activation, suspend/resume, and each cleanup stage. If the policy service is unavailable, the workflow enters a visible blocked/retryable state without performing the protected side effect. Policy or grant revocation invalidates any pending authorization decision; it does not rewrite immutable request or manifest contents.

### 2.3 Enforcement map

| Boundary | Trusted scope source | Required enforcement |
|---|---|---|
| REST request | Verified principal plus registry-resolved canonical tenant | Capability and exact tenant/app/environment/operation check; audit decision. |
| Bootstrap reservation | Verified operator grant and reservation payload | Dedicated reservation operation; no tenant credential or caller-claimed tenant is accepted as proof of ownership. |
| Repository read/write | Authenticated request context and persisted row scope | Tenant predicate always; application/environment predicates for scoped records; expected-version CAS for mutations. |
| Outbox dispatcher | Persisted dispatch/request scope | Verify tenant, request revision, execution generation, job relationship, current policy and lifecycle epoch before start. |
| Temporal workflow/activity | Persisted immutable request plus trusted workflow identity | Reauthorize before privileged side effect; compare request/policy/catalog versions and tenant lifecycle epoch. |
| Cleanup planner/worker | Persisted manifest, store-registry version, cleanup epoch | Require active fence, exact manifest scope/hash, valid approval, no hold, and current per-stage authorization. |

## 3. RLS and database role contract

### 3.1 Role boundaries

RLS is enabled and forced for every tenant-bearing onboarding table. No application, worker, or operator role receives `BYPASSRLS`. Table ownership must not be used as a routine application access path. A role that can assume another role must be reviewed as having that role's effective privileges.

The v0.1 migration grants CRUD to `CURRENT_USER`, as explicitly selected for that phase. This grant is table privilege only; it does not bypass RLS. Deployment must verify which database principal ran the migration and which principal each API/worker pool uses. The two must not be assumed identical. `CURRENT_USER` grants do not by themselves define safe operator/bootstrap access.

### 3.2 Policy classes

| Table class | Row scope | Access contract |
|---|---|---|
| `tenant_registry` | `canonical_id` | Tenant-scoped callers see only their canonical ID. Bootstrap and cross-tenant operator lookups require a separately authenticated, dedicated registry-admin path with no `BYPASSRLS`; no magic tenant ID or caller-controlled GUC bypass is permitted. Role provisioning and membership must be separate from the runtime tenant role. |
| `onboarding_requests` | `tenant_id` | RLS restricts tenant rows. Repository and authorization additionally constrain `application_id`, `environment`, revision, and operation. |
| `onboarding_dispatches` | `tenant_id` | RLS restricts tenant rows. Dispatch claim/recovery uses a dedicated worker path with explicit tenant context and row-level lease fencing; global polling must not use a tenant GUC bypass. |
| Future approvals, manifests, outcomes, fences | `tenant_id` | Forced RLS, exact tenant predicates, and composite tenant foreign keys for every tenant-owned relationship. |

The registry-admin path is a design prerequisite, not present in migration 023. Its role, privileges, authentication boundary, audit record, and integration tests must be reviewed before registry reservation or global registry lookup is enabled. The design must not silently add a permissive policy to migration 023.

### 3.3 Relationship integrity

Every reference from a tenant row to another tenant-owned row includes the tenant key in the foreign-key relationship. In particular, request and dispatch references to `background_jobs` must prove the referenced job has the same `tenant_id`; a UUID-only FK is insufficient. Where PostgreSQL requires a referenced composite unique key, add it explicitly. A nullable request `job_id` is allowed before job creation, but when present it must satisfy the tenant-scoped FK.

For the dispatch-to-job relationship, the target schema must expose `UNIQUE (tenant_id, id)` on `background_jobs`, and dispatch uses `FOREIGN KEY (tenant_id, job_id) REFERENCES background_jobs (tenant_id, id)`. Additive migration 023 currently has only the UUID job FK; Phase 0R must replace it with this composite relationship using a forward-compatible constraint migration and verify existing data before adding it.

The composite dispatch-to-request FK remains tenant/request/revision scoped. Application and environment remain enforced by authorization/repository predicates and must be included in any future cross-scope relationship that could otherwise attach work to a sibling application or environment.

### 3.4 Required RLS proof

Database integration tests must connect as the deployed runtime application role with `FORCE ROW LEVEL SECURITY` active and prove:

- tenant A cannot read, mutate, infer, or reference tenant B rows;
- a tenant-scoped grant cannot cross tenant, application, environment, or operation boundaries;
- cross-tenant composite FK attempts fail;
- bootstrap and registry-admin access work only through the separately authenticated admin path;
- workers cannot use global polling to evade RLS, and a stale worker cannot overwrite a current lease holder;
- the migration principal, runtime role, worker role, and any registry-admin role have the documented privileges and no unintended role memberships.

Unit tests and offline migration rendering do not substitute for this database-role integration gate.

## 4. Cleanup write fence

### 4.1 Fence record and state

Cleanup is tenant-scoped and, when supported by the ownership model, also application/environment-scoped. Before final inventory, one transaction locks the tenant lifecycle row, checks the expected lifecycle version, increments a monotonic `lifecycle_epoch`, records the cleanup manifest intent, and moves the tenant/scope to `CLEANUP_PENDING`. The fence record binds tenant, optional app/environment scope, manifest ID, epoch, requester, and state. A uniqueness/CAS rule permits only one active cleanup fence for a scope.

The fence blocks new intake revisions, configuration writes, provisioning dispatches, and incompatible lifecycle transitions within its scope. Every queued or running onboarding workflow must either be drained, canceled safely, or explicitly recorded as an unresolved operation before inventory may be declared final. A later cancellation/release increments the epoch again so workers carrying the old epoch cannot resume writes.

### 4.2 Inventory and execution protocol

1. Acquire the fence and persist its epoch before admitting the cleanup request. An application/environment-specific cleanup fence still advances the tenant-wide lifecycle epoch; this intentionally fences unrelated tenant operations until fine-grained epochs can prove non-interference.
2. Stop accepting conflicting writes and prevent new dispatches. Signal in-flight workflows to stop at a safe checkpoint; reconcile ambiguous provider operations by stable resource key.
3. Wait for all relevant work to reach a known terminal or explicitly blocked state. An unknown side-effect result blocks final inventory.
4. Enumerate all required stores after the drain. Bind every result and plan entry to the same tenant/app/environment scope, lifecycle epoch, registry version, policy version, and inventory window.
5. Obtain approval over the immutable manifest. Before each destructive stage, recheck the fence owner/epoch, manifest hash, approval consumption, policy/hold state, and expected lifecycle version.
6. Adapters must accept or independently verify the fence epoch when available. If an external system cannot enforce epochs, use idempotency/discover-before-create and re-enumerate/reconcile after the action. A non-fenceable class needs an explicit compensating control and cannot be omitted from evidence.
7. Do not release the fence until all required resources have verified outcomes or the request has a separately authorized residual/retention terminal result. A release or cancellation makes old-epoch workers stale.

### 4.3 Race and crash acceptance

Tests must interleave cleanup with new intake, dispatch claim, active workflow, late provider success after timeout, policy revocation, approval, and cleanup cancellation. They must prove that no stale epoch can create or reattach a resource after completion; a late or ambiguous write is rediscovered and incorporated into outcomes; and the tenant cannot be reported cleanup-complete while the fence is absent, stale, or held by another manifest.

## 5. Atomic approval consumption

### 5.1 Persisted approval contract

Approval persistence must include: approval ID; tenant and manifest IDs; exact manifest hash and canonical payload hash; token digest; nonce; authenticated requester and approver IDs; capability/policy version; scope; expiry; creation and consumption times; consuming purge-job/dispatch IDs; and revocation/invalidation reason where applicable. The raw bearer token is returned once and never persisted, logged, placed in a URL, or emitted in telemetry. The manifest and approval rows are immutable except for their defined lifecycle/consumption fields.

### 5.2 Transaction protocol

1. Authenticate the approver and resolve current scoped `tenant:approve`/`tenant:cleanup` grants. Require a persisted requester and enforce separation of duties.
2. Load and lock the tenant fence, manifest, approval, and expected request/lifecycle versions in a consistent lock order. Verify the fence epoch, manifest state/hash, policy/store-registry versions, no active hold, and server-time expiry.
3. Verify the presented token against the stored token digest using constant-time digest comparison; recompute and verify the canonical payload digest using the same versioned canonicalization as minting.
4. Conditionally consume exactly one approval, e.g. `UPDATE ... SET consumed_at = clock_timestamp() WHERE approval_id = :id AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at > clock_timestamp() AND manifest_hash = :expected_hash RETURNING ...`.
5. In that same transaction, create the purge job, audit linkage, and outbox dispatch intent and transition the manifest to the authorized execution state. If any insert, constraint, or CAS fails, roll back consumption and all associated records.
6. A replay, second concurrent approver request, wrong principal, expired/revoked approval, changed manifest, changed fence epoch, or stale policy version returns a stable denial/conflict and creates no job or dispatch.

The conditional consume is the one-time gate; an application-level read followed by an unconditional update is not sufficient. A property/unit test is not the concurrency proof: two simultaneous database transactions must demonstrate that exactly one commits a purge job and outbox intent.

## 6. Implemented-foundation remediation gate

The current committed authorization helper carries scope in its result but does not resolve tenant/application/environment/operation-scoped grants. The current control-store schema gives `background_jobs` references without the tenant composite guarantee and does not contain lifecycle fencing, execution-generation, or lease-fencing fields. Registry bootstrap/operator access is not implemented. These are design-to-implementation gaps, not evidence of a production exploit.

Before enabling onboarding intake or dispatch, a remediation phase must update the authorization implementation and matrix, add additive schema changes (without rewriting revision 023), establish the registry-admin/RLS deployment contract, add tenant-scoped job relationships, and add lifecycle/lease fences needed by later phases. See the implementation plan's Phase 0R.

## References

- [Luna onboarding design](./IAP-TENANT-ONBOARDING-DESIGN-Luna.md)
- [Workflow correctness specification](./IAP-TENANT-ONBOARDING-WORKFLOW-CORRECTNESS.md)
- [Deployment and deletion assurance specification](./IAP-TENANT-ONBOARDING-DEPLOYMENT-DELETION-ASSURANCE.md)
- [RFC 8785: JSON Canonicalization Scheme](https://www.rfc-editor.org/rfc/rfc8785)
