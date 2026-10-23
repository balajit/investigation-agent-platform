# Onboarding Dispatch Runbook (Phase 2)

Operator procedures for the onboarding dispatch lifecycle. All SQL runs as
`iap_app` (reads, acceptance) or `iap_worker` (claims, fencing) with
`app.tenant_id` set; `iap_migrator` is DDL-only. Nothing here creates
tenants, grants, or workflow definitions — see
`docs/IAP-TENANT-ONBOARDING-DB-ROLES.md` for the role model.

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
