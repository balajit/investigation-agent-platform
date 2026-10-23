# Onboarding Dispatch Runbook (Phase 2 + Phase 4 operations)

Operator procedures for the onboarding dispatch lifecycle. All SQL runs as
`iap_app` (reads, acceptance) or `iap_worker` (claims, fencing) with
`app.tenant_id` set; `iap_migrator` is DDL-only. Nothing here creates
tenants, grants, or workflow definitions — see
`docs/IAP-TENANT-ONBOARDING-DB-ROLES.md` for the role model.

## Request lifecycle over the API/CLI (Phase 4)

Normal path per request revision: create (DRAFT + accept to QUEUED, 202)
→ probes filed → readiness evaluated → approve (registry ACTIVE on READY)
→ workflow files the coverage report. Every step is tenant-scoped; the
CLI (`onboard --tenant T ...`) is a thin client over the same contracts.

```text
onboard --tenant T --idempotency-key K apply intake.json     # 202, returns request/job/dispatch ids
onboard --tenant T status --request-id R                     # state, readiness, blockers
onboard --tenant T capabilities                               # policy-resolved dispositions
```

## Cleanup (Phase 5): preview, approve, purge, verify

Deletion is a durable exact-plan workflow. Every destructive stage
revalidates the manifest hash and active hold status before acting.

```text
onboard --tenant T cleanup-plan --environment prod --deployment-id dep-1 \
  --policy-version p1 --requester req --approver approver \
  --backup-id backup-1 --expires-at 2026-12-01T00:00:00+00:00
# → manifest_id + plan_hash + per-store resource counts, or blocking codes.
# BLOCKED means: missing adapter, stale registry, partial enumeration,
# provider timeout, unknown ownership, or legal hold. Never override a
# blocker -- fix the cause and preview again (new plan hash, new approval).

onboard --tenant T cleanup-approve --manifest-id M --approver A \
  --requester R --policy-version p1 --registry-version reg-v1 \
  --expires-at ...                      # mints the one-time token (shown once)
onboard --tenant T cleanup-approve --manifest-id M --approver A \
  --token-value <token>                 # atomically consumes; exactly one winner
```

Approval rules (enforced, not advisory): the approver must differ from the
requester (separation of duties); the token is returned once and only its
digest is stored; expiry is checked against server time; concurrent
consumers converge on exactly one purge job; reuse, tampering, expiry, and
wrong-approver attempts fail closed and are auditable.

Purge execution runs the approved plan entries through store adapters
(idempotent per entry key) and appends append-only outcomes. Verify the
terminal state -- `COMPLETE`, `COMPLETE_WITH_RETENTION`,
`COMPLETE_WITH_APPROVED_RESIDUALS`, `INCOMPLETE`, `BLOCKED`, or `CANCELED`:
every entry must reach an explicit honest outcome. `RETAINED_BY_POLICY` and
approved residuals are never described as erased. Unknown or missing entries
are nonterminal by construction.

Backup and restore: a verified backup (`backup_id` + `backup_verified_at`)
is required before approval; purge revalidates holds and the plan hash
before each destructive stage. Restore drills follow the
deployment/deletion assurance spec; record drill evidence with the manifest.

Rollback: disable new cleanup requests and stop workers before destructive
stages. Preserve manifests, approvals, tombstones, outcomes, and backup
references for forward recovery. Never roll back by deleting the
audit/manifest.

## Draft service objectives (Phase 6, require SRE/product sign-off)

These targets are proposed defaults, not accepted SLOs. No load or
correctness gate is met until SRE accepts them and the evidence below exists.

- Dispatch lag: PENDING dispatch claimed within 60s (alert at 5min).
- Stuck workflow: heartbeat gap over 3 intervals pages on-call.
- Reconciler: expired leases requeued within 2 lease durations.
- Readiness: evaluation completes within 30s of probe filing.
- Purge: per-entry outcome appended within 5min of consume; terminal
  state computed within 10min of last outcome.

## Threat notes for the new surfaces

- Approval tokens are bearer secrets: returned once, never logged, stored
  as digests only. Treat token-bearing responses as secret material in
  dashboards and audit trails.
- Purge authorization is the consumed approval row, not the API caller's
  claim: execution requires a consumed, unexpired, unrevoked approval.
- Probe evidence is advisory input: VERIFIED means the scoped check passed,
  never that an estate is supported. Activation still requires the
  policy/catalog-pinned READY evaluation, never a single probe.
- Store adapters are the trust boundary for deletion completeness: an
  UNSUPPORTED required store blocks approval instead of completing.

Retry moves only RETRYABLE dispatches due now; TERMINAL rows never move.
Cancel marks CANCEL_REQUESTED; the workflow heartbeat routes it to the
cancel path and converges the row to CANCELED. Suspend/resume bump the
registry lifecycle epoch, fencing stale workers. Approve requires a fresh
READY report pinned to the same policy/catalog versions, otherwise 409
NOT_READY with blocking codes — advisory mode never applies here: the
approve path always enforces. Deletion preview/approval stay disabled
(CLEANUP_NOT_ENABLED) until Phase 5.


## Normal operation

1. The dispatcher claims the oldest due PENDING/RETRYABLE dispatch
   (`claim_and_reconcile`, which fences one expired lease first), then
   starts the deterministic workflow id
   `onboarding-{tenant}-{request_id}-r{revision}g{gen}`.
2. On a clean start it records ACCEPTED + the run id (CAS on the fencing
   token). A superseded holder writes nothing; the reconciler owns recovery.
3. Retries back off exponentially (`retry_backoff_seconds`, capped at one
   hour); attempts beyond `MAX_DISPATCH_ATTEMPTS` (default 5) go TERMINAL.

## Stuck dispatch (PENDING, never started)

Symptom: `state='PENDING'` older than expected, no run id.

1. Confirm no workflow exists: describe `workflow_id` via the Temporal
   tooling. If one is running, do nothing — the dispatcher will adopt it on
   the next claim cycle (`AlreadyStarted` → describe → adopt).
2. If none exists, the next dispatcher pass claims it normally. No manual
   start: there must never be two starters for one deterministic id.

## Expired lease (CLAIMED, lease_expires_at past)

The reconciler fences one expired lease per pass (`STALE_FENCE` audit) and
returns it to RETRYABLE. Manual equivalent, as the worker role:

```sql
SELECT record_onboarding_dispatch_audit(:tenant, :dispatch, 'STALE_FENCE', :token);
-- then release the row to RETRYABLE (see repository release_for_retry)
```

Never clear a live lease by hand: a fencing-token mismatch on the next
write is the safety net, not an inconvenience to bypass.

## Ambiguous start (timeout with unknown outcome)

1. Describe the workflow id. Running → adopt (record the run id under the
   current fencing token; superseded holders fail closed).
2. Not found → release to RETRYABLE with backoff; the next claim retries
   the start. The deterministic id makes the retry idempotent.
3. A duplicate start that raises `AlreadyStarted` follows the same
   describe-first rule — never assume the error tells you who won.

## Terminal dispatch

`TERMINAL` is final for that generation. A new business recovery needs an
explicit new generation (`execution_generation = max + 1`), which requires
the latest dispatch to be terminal. Check `last_error_code` first:
`retry_budget_exhausted` means the input or downstream is broken, not the
dispatcher — fix the cause before generating again, or the new generation
will burn its attempts the same way.

## Cancellation

1. Mark the request `CANCEL_REQUESTED` (tenant-scoped CAS; `complete_request_cancellation`
   moves QUEUED/CANCEL_REQUESTED to CANCELED idempotently).
2. Signal cancellation to the running workflow; on missing workflow, complete
   cancellation directly (nothing to cancel).
3. Verify: dispatch TERMINAL with `last_error_code='canceled'`, request
   CANCELED. Retries must never resurrect a canceled request — the
   dispatcher refuses to start canceled revisions (checked at claim time).

## What the reconciler never does

- Start workflows (only the dispatcher starts; a second starter would race
  fencing instead of honoring it).
- Create grants, tenants, or jobs outside acceptance (all writes are
  tenant-scoped CAS operations on existing rows).
- Silently drop work: every path either repairs (requeue, adopt, cancel) or
  escalates to TERMINAL with a recorded reason, which surfaces in the
  `terminal_mirrored` / `retry_exhausted` counts.

## PostgreSQL verification (disposable only)

Never verify against a persistent database. Pattern (see the disposable-DB
gate entries in the recommendations ledger):

```text
docker run -d --rm -p 5543:5432 postgres:16   # or pgvector variant if needed
# verify what connected: SELECT version_num FROM alembic_version (expect none)
# provision roles per scripts/provision-onboarding-db-roles.sql
# 010 DDL verbatim → alembic stamp 022 → alembic upgrade head
# run opt-in suites with IAP_TEST_PG_URL pointed at the container
# docker rm -f <name>; verify absent with docker ps
```

Any `alembic stamp`/`upgrade` against `localhost:5432`, `localhost:5452`,
or an inherited `IAP_DATABASE_URI`/`IAP_TEST_PG_URL` is blocked by the
pg_guard hook — that refusal is the system working, not an error to rout around.
