# Tenant Onboarding Database Roles and Bootstrap

## Role boundary

Provision these login roles outside Alembic with `scripts/provision-onboarding-db-roles.sql`; that script sets role attributes and privileges, while credentials are assigned through the approved secret manager or secure operator procedure. No role receives `BYPASSRLS`. `iap_migrator` owns onboarding database objects and runs migrations. `iap_app` performs application DML subject to tenant RLS. `iap_worker` claims and updates only the dispatch lease/state columns and reads request rows only after setting transaction-local `app.tenant_id` from the claimed dispatch. `iap_registry_admin` is a human/operator path for registry lookup and reservation; it is not used by the dispatcher. App, worker, and registry-admin receive no DDL privileges.

Migration 023's `CURRENT_USER` ACL is revoked and superseded in additive migration 024. The migration clears direct ACL entries from non-owner roles on the seven onboarding tables before applying the approved grants, so a migration executed under an unexpected principal does not leave its historical broad grant behind. The application, worker, registry-admin, and migrator role logins are distinct; no role membership links runtime roles to the migrator.

Migration 024 additionally creates the agent-contract tables `onboarding_support_registry` (deployment-scoped, no tenant column; readable by app/worker/registry-admin, writable only by the migrator role) and `onboarding_coverage_reports` (tenant-scoped, FK-bound to the immutable request revision plus per-generation uniqueness; app SELECT/INSERT, worker and registry-admin SELECT). Both tables are covered by the same ACL scrub, FORCE RLS, and `updated_at` trigger maintenance as the other onboarding tables. No seed rows and no grant rows are created by any migration.

The sole cross-tenant worker policy is `TO iap_worker` on `onboarding_dispatches`. SELECT visibility uses `USING (true)` under FORCE RLS. UPDATE uses `USING (true)` and `WITH CHECK (lease_owner IS NULL OR lease_owner = current_setting('iap.worker_id', true))`; worker UPDATE grants remain column-limited. The check permits legitimate release/requeue writes that clear the owner and requires any retained/new owner to match the transaction-local worker identity. This formalizes the existing worker trust perimeter; it is not a control against worker-process compromise and does not independently constrain state transitions. No worker-specific policy exists on onboarding requests, intake, grants, audit, coverage, or future approval/manifest tables; worker reads on tenant-scoped tables go through the standard tenant policy with explicitly set context. Every dispatch claim, lease renewal/release, and stale fencing event is audited with the worker process identity from transaction-local `iap.worker_id`, the database `session_user`, and fencing token. `iap.worker_id` is audit attribution and an owner consistency check, not an authenticated identity.

The poller first runs a global expired-lease reconciliation claim (one expired `CLAIMED` row via `FOR UPDATE SKIP LOCKED`, then requeues it as `RETRYABLE`, releases the lease, and audits `STALE_FENCE`). It then claims the globally oldest due `PENDING` or `RETRYABLE` row; the claim increments the fencing token and assigns owner/expiry atomically. Renewal keeps the token; the next claimant increments it.

The registry-admin policy is role-targeted and applies to `tenant_registry` only. It uses neither a magic tenant GUC nor `BYPASSRLS`. `iap_registry_admin` can read grants for human administration; normal tenant grant reads remain tenant-context scoped, and platform checks use an exact-scope Boolean function rather than returning cross-tenant grant rows. The first platform grant uses literal tenant scope `__platform__`; no migration seeds a grant.

## Local opt-in role/RLS integration tests

The actual-role integration suite is opt-in and skips when the PostgreSQL TCP endpoint is unreachable:

```bash
IAP_RUN_ONBOARDING_DB_TESTS=1 IAP_TEST_PG_URL=postgresql://iap:iap@localhost:5432/iap_test .venv/bin/python -m pytest tests/integration/test_onboarding_postgres_roles.py -q
```

Provision the local test login/database with PostgreSQL's standard local administration tools, then grant the test login database creation/role-management privileges only on this disposable test instance:

```bash
createuser iap --createdb --createrole
createdb iap_test --owner=iap
```

The fixture additionally requires that login to create uniquely named LOGIN roles with passwords and to assume/drop those scratch roles. It generates ephemeral random scratch-role passwords in memory, uses them only in the test process's database connection URLs, and tears down roles/schema afterward. The role-provisioning script never sets any login password.

The test creates uniquely named scratch roles and schemas, enables and forces RLS, exercises worker polling, and tears down scratch objects. It does not use Testcontainers. Only run against a disposable test database where the supplied login is authorized to create/drop scratch roles and schemas.

The audit and platform-grant function tests create scratch-local mirrors explicitly marked in the test module. Migration 024 is the source of truth; offline migration contract tests independently pin its function definitions. The dual-person limitation test is intentionally a passing test: it demonstrates that direct `iap_migrator` grant DML can create a resolvable grant without an audit row.

## First platform-grant bootstrap

The initial platform grant is minted offline by two distinct authorized operators. One requests the grant with principal `sub`, capability, exact platform/application/environment/operation scope, validity interval, policy version, issuer, and reason. A second operator reviews and approves the exact canonical payload. The operator utility `scripts/iap-onboarding-grant.py` requires externally approved identities and the matching SHA-256 digest of the exact canonical payload, then atomically writes the grant and append-only dual-person audit row through the `iap_migrator` DSN. The utility does not authenticate those identities; the documented external operator approval procedure is authoritative, and the DSN must not be exposed to a service. Verify the grant through the reviewed grant-resolution path. The JWT tenant claim is not entitlement evidence. Revoke uses the same audited two-person procedure. No raw grant seed is stored in source control or migration history.

**Explicit v0.1 acceptance:** Dual-person mint/revoke control is procedural and enforced by the reviewed CLI/operator procedure, not by the database. A holder of the `iap_migrator` credential can insert or alter a grant directly without a paired audit row; the grant resolver can still accept that row if its scope and validity match. This residual is pinned by `test_dual_person_is_procedural_not_database_enforced`. The `iap_migrator` credential is restricted to the authorized procedure participants and is never exposed to a service. A SECURITY DEFINER mint function that enforces distinct authenticated identities and atomically writes the grant plus audit is a Phase-2-or-later hardening item.

**Canonical digest and scope:** The v0.1 approval digest uses the utility's self-consistent canonical payload serialization; it is not RFC 8785 JCS and is an explicit carve-out until a versioned JCS adapter and vectors are adopted. Platform grants bind exact application and environment values; v0.1 defines no wildcard convention. Do not infer all-applications authority from an empty or special value.

Do not mint the first grant by direct SQL without the dual-person record. The first platform grant must be created by the operator utility after the two-person payload review is complete.
