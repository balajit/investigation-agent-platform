# Tenant Onboarding Workflow Correctness Specification

**Status:** Design contract; implementation and production qualification remain gated. This specification refines D4 and D6 in [the Luna onboarding design](./IAP-TENANT-ONBOARDING-DESIGN-Luna.md) and complements the [security/concurrency supplement](./IAP-TENANT-ONBOARDING-SECURITY-CONCURRENCY.md).

## 1. Authorities and durable records

| Concern | Authority | Durable evidence |
|---|---|---|
| Tenant lifecycle | Tenant registry/lifecycle record | Tenant state, lifecycle version, monotonic lifecycle epoch. |
| Accepted input | Onboarding control store | Immutable tenant/request/revision snapshot, content hash, policy/catalog/schema versions, actor and idempotency reservation. |
| Dispatch intent | PostgreSQL outbox/control store | Dispatch event, tenant/request/revision, execution generation, deterministic workflow ID, job ID, attempt/backoff, lease/fencing token, Temporal run reference and status. |
| Workflow execution | Temporal | Workflow history, execution outcome, signals/updates, activity retry history. |
| Queryable job state | PostgreSQL background job read model | Tenant-scoped status/progress and correlation to request/revision/generation. It is a read model, not execution authority. |
| External side effects | Owning provider | Provider resource key/result plus onboarding step idempotency and reconciliation evidence. |

An API acceptance response is permitted only after one transaction commits the accepted request revision, idempotency binding, initial job, audit linkage, and dispatch intent. The transaction commits all records or none. Reuse of an idempotency key with the same canonical request returns the original durable acceptance; reuse with a different canonical request returns a stable conflict and does not mutate the original.

## 2. Workflow identity and generations

### 2.1 Separate delivery retry from business retry

- **Outbox delivery retry:** Reattempt starting the same logical workflow. It reuses the same tenant/request/revision/generation identity and workflow ID. It never creates a new business execution.
- **Temporal retry:** Temporal retries an eligible workflow/activity according to the pinned retry policy. It remains the same business execution. External activity effects must be idempotent or discoverable by a stable step key.
- **Business recovery retry:** An authorized request to recover after a terminal, canceled, or otherwise non-resumable workflow. It creates a new monotonic `execution_generation` linked to the same immutable request revision and prior generation. Its workflow ID includes that generation. The retry record names the selected recovery point/steps and preserves original step evidence.
- **New input revision:** A changed intake creates a new immutable request revision and starts a new logical request execution. It is not a retry.

Current schema 023 has a unique workflow ID per tenant and a unique dispatch per tenant/request/revision; it has no execution-generation model. The Phase 0R schema remediation must add the generation model before business retries are enabled. Until then, no same-revision post-terminal retry route is enabled.

### 2.2 Workflow ID

Use an opaque deterministic ID derived from stable internal identifiers (tenant canonical ID, request ID, revision, execution generation), with a versioned encoding and no display names, credentials, provider IDs, or other sensitive data. The ID is not authorization evidence. Temporal workflow ID conflict and reuse policies must be explicitly configured and tested rather than relying on SDK/server defaults. Dispatcher retries target an open execution; closed-execution behavior is governed by the generation contract, not accidental Workflow ID reuse.

The returned Temporal run ID is useful correlation evidence but is not a stable business key; retries/continued executions can produce a new Run ID. Persist it for observability only.

## 3. Transactional acceptance invariant

For a newly accepted submission, the database transaction contains:

1. Immutable request revision with exact tenant/app/environment and schema, policy, catalog, and source versions.
2. Idempotency reservation bound to the canonical request hash and response identity.
3. Initial background-job read model row.
4. Audit record linking authenticated actor, authorization decision, request hash, and transition.
5. Dispatch/outbox intent with deterministic workflow identity and initial attempt/state.
6. Request transition to `QUEUED` using expected state version.

**Invariant:** all six commit atomically, or none do. No Temporal call, provider call, or external side effect occurs inside the database transaction. A transaction retry must re-evaluate uniqueness/CAS and return either the same acceptance or a stable conflict.

## 4. Dispatch claim, lease, and Temporal start

### 4.1 Claim

Claim due rows in a short database transaction using row locking such as `FOR UPDATE SKIP LOCKED` or an equivalent atomic claim. Claim sets a monotonically increasing `lease_fencing_token`, `lease_owner`, `lease_expires_at`, attempt count, and state/version. A worker cannot claim a row whose retry time is in the future or whose current lease is valid.

Lease owner and fencing token are both checked on every subsequent update. Lease renewal is conditional on the same owner/token and current unexpired lease. A stale worker that loses the lease cannot acknowledge, retry, terminally fail, or overwrite the current owner's result. The external Temporal start call happens outside the row-lock transaction.

### 4.2 Start and acknowledgement

The dispatcher sends the deterministic workflow ID and pinned workflow input reference/version to Temporal. It stores no raw intake or secret material in logs. Outcomes:

| Result | Dispatch action |
|---|---|
| Start confirmed | Conditionally record Temporal run reference and `ACCEPTED` using current lease token; update job/request projections transactionally. |
| Same logical workflow already open | Query/describe the existing execution, verify identity and immutable input/generation, then acknowledge it as accepted and link the observed run. A conflict with different input is terminal integrity failure. |
| Timeout or connection loss with unknown start outcome | Keep intent retryable; do not create a new workflow ID. Reconcile/query by deterministic ID before another start. |
| Explicit transient Temporal failure | Record bounded error code, schedule backoff, release/expire lease conditionally. |
| Explicit permanent validation/authorization/identity conflict | Mark terminal with stable reason, preserve request/job/outbox evidence, alert/record operator action. |

Never mark accepted before Temporal confirms the start or a reconciler proves the same workflow is already running. Never report acceptance of a workflow execution based solely on a successful local database commit.

### 4.3 Retry budget and escalation

Retry policy is versioned and records maximum attempts, maximum elapsed retry age, exponential backoff/jitter, and permanent/transient classifications. A workflow start retry does not silently discard an event when a budget is exhausted. Exhaustion transitions to a visible terminal/escalated dispatch state with alert, operator remediation instructions, and retained durable intent. Manual repair is audited and reuses the same generation unless an authorized business-recovery generation is explicitly created.

## 5. State transition contract

Each business transition is a compare-and-set against expected state and `state_version`; an accepted transition increments version exactly once and records actor/capability, request revision, policy version, timestamp, and reason. The database update and audit linkage are atomic. Illegal edges fail with a stable conflict; clients do not write state strings directly.

### 5.1 Request lifecycle

| From | To | Required guard |
|---|---|---|
| `DRAFT` | `VALIDATING` | Exact immutable revision exists; schema and policy versions resolved. |
| `VALIDATING` | `AWAITING_APPROVAL` or `QUEUED` | All required validation/policy gates pass; approval route only if catalog demands approval. `QUEUED` is committed with job and dispatch intent. |
| `AWAITING_APPROVAL` | `QUEUED` | Authenticated approver, separation of duties, exact revision/policy hash, approval CAS. |
| `QUEUED` | `PROVISIONING` | Dispatch accepted or workflow has begun; job/request correlation matches. |
| `PROVISIONING` | `READINESS_CHECK` | All configured apply steps are terminal with explicit outcomes. |
| `READINESS_CHECK` | `ACTIVE` | Fresh mandatory readiness for the exact request/policy/catalog versions; tenant lifecycle CAS accepts current epoch and is not suspended, retired, or fenced. |
| Nonterminal execution state | `FAILED_RETRYABLE` | Retryable failure recorded; committed intent and evidence retained. |
| Nonterminal execution state | `FAILED_TERMINAL` | Permanent failure, retry budget exhaustion, or integrity/security violation; no hidden retry. |
| Nonterminal execution state | `CANCEL_REQUESTED` | Authorized expected-version transition; cancellation signal and durable audit intent committed. |
| `CANCEL_REQUESTED` | `CANCELED` | Workflow confirms safe stop and required compensations are terminal or individually represented as unresolved. |
| `FAILED_TERMINAL` / `CANCELED` | New generation | Authorized business recovery record; immutable original history preserved. |

Tenant lifecycle remains separate. A request state cannot itself activate, suspend, resume, or retire a tenant. `DEPROVISIONING/DEPROVISIONED` belongs to the deletion lifecycle and follows the deletion assurance specification.

## 6. Cancellation and compensation

Cancellation is a durable request, not an assumption that external work stopped. The service first commits `CANCEL_REQUESTED`, expected version, actor authorization, and cancellation audit; it then signals the exact workflow execution. Repeated signals are idempotent. A cancel response describes the request as pending until Temporal acknowledges the signal and activities reach safe checkpoints.

Activities check cancellation before beginning a side effect and after any long-running provider call. If provider success is ambiguous, discover by stable provider key before retry or compensation. Compensation is an explicit, separately idempotent step; the workflow does not claim rollback of irreversible external effects. Each action yields a typed outcome (`DONE`, `NOT_STARTED`, `COMPENSATED`, `RETAINED_BY_POLICY`, `RESIDUAL`, `UNKNOWN`, or `BLOCKED`) and evidence. Unknown/uncompensated effects remain visible and recoverable.

Activation and privileged side effects revalidate request revision, policy/catalog version, authorization, expected tenant lifecycle version, and lifecycle epoch immediately before acting. A stale or canceled workflow cannot overwrite a newer suspension, retirement, cleanup fence, or request revision.

## 7. Reconciliation protocol

Reconciliation is a durable, idempotent service. Each scan records its cursor/window and result counts and processes bounded batches. It compares PostgreSQL dispatch/job/request projections with Temporal executions by deterministic workflow ID and generation, then checks provider side effects by stable step key where outcomes are ambiguous.

| Observed mismatch | Reconciliation action |
|---|---|
| Due dispatch without active lease | Claim and dispatch according to policy. |
| Expired lease | Increment/fence token, reclaim; stale owner updates are rejected. Query Temporal before retrying start. |
| Temporal execution exists, dispatch pending | Verify workflow ID, generation, and pinned input; repair dispatch/job projection and record observed run. |
| Dispatch accepted, execution absent/closed unexpectedly | Inspect Temporal history/status; classify transient, terminal, or authorized recovery. Never blindly start a new generation. |
| Queued job with no dispatch | Integrity failure: do not fabricate intent; alert and require audited repair or explicit rejection. |
| Dispatch with missing/mismatched request, tenant, job, or revision | Quarantine/terminally block; do not start. |
| Workflow terminal, projection stale | Rebuild projection from authoritative workflow outcome with version-conditional update and audit linkage. |
| External side-effect outcome unknown | Discover by idempotency/resource key; append the verified result or leave explicitly `UNKNOWN` and escalate. |

Reconciliation must be safe under multiple replicas, repeated scans, process restarts, and partial database/Temporal/provider outages. No reconciliation path silently deletes intent, rewrites an immutable request, changes generation, or invents success.

## 8. Required failure-injection evidence

The existing five strict-xfail scaffolds become real deterministic harness tests. Add duplicate delivery, concurrent claim, lease renewal/loss, `AlreadyStarted`, ambiguous start timeout, closed workflow identity reuse, terminal recovery generation, cancellation at each side-effect boundary, projection repair, and provider success/local failure cases.

Required outcomes:

- Failure before acceptance commit leaves none of the six acceptance records and starts no workflow.
- Failure after commit/before dispatch is repaired from durable intent.
- Failure after Temporal start/before acknowledgement is reconciled to the original execution without duplicate business work.
- Expired lease invalidates the old worker's write authority.
- Temporal outage preserves intent and produces bounded retries plus visible escalation.
- Concurrent identical API submissions yield one accepted transaction and replay; changed-body key reuse conflicts.
- Business retry after a terminal workflow creates a new audited generation and cannot replay completed side effects without idempotency/discovery.
- Cancellation, suspension, policy revocation, and cleanup fencing prevent stale activation or mutation.

## References

- [Temporal Workflow ID and Run ID semantics](https://docs.temporal.io/workflow-execution/workflowid-runid)
- [Security and concurrency supplement](./IAP-TENANT-ONBOARDING-SECURITY-CONCURRENCY.md)
- [Deployment and deletion assurance specification](./IAP-TENANT-ONBOARDING-DEPLOYMENT-DELETION-ASSURANCE.md)
- [RFC 8785: JSON Canonicalization Scheme](https://www.rfc-editor.org/rfc/rfc8785)
