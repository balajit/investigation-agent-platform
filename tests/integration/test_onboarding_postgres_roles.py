"""Opt-in actual-role RLS and dispatch claim tests for onboarding Phase 0R."""

from __future__ import annotations

import os
import secrets
import socket
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import sleep
from typing import Any
from urllib.parse import urlparse

import pytest
from sqlalchemy.exc import DBAPIError

RUN_DB_TESTS = os.environ.get("IAP_RUN_ONBOARDING_DB_TESTS", "0") == "1"
PG_URL = os.environ.get("IAP_TEST_PG_URL", "postgresql://iap:iap@localhost:5432/iap_test")
pytestmark = pytest.mark.skipif(
    not RUN_DB_TESTS,
    reason="Opt-in actual-role onboarding DB tests (IAP_RUN_ONBOARDING_DB_TESTS=1)",
)


def _reachable() -> bool:
    parsed = urlparse(PG_URL)
    try:
        with socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or 5432), timeout=1
        ):
            return True
    except OSError:
        return False


def _connect_url(username: str | None = None, password: str | None = None):  # type: ignore[no-untyped-def]
    from sqlalchemy.engine import make_url

    url = make_url(PG_URL)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    if username is not None:
        url = url.set(username=username, password=password)
    return url


def _create_function_mirrors(
    conn: Any, schema: str, worker_role: str, app_role: str, migrator_role: str
) -> None:
    """Create scratch-local mirrors of migration 024 security functions.

    Migration 024 is the source of truth; its offline contract tests independently
    pin these function bodies. Schema qualification keeps the mirrors isolated.
    """
    conn.exec_driver_sql(
        "CREATE TABLE onboarding_dispatches (id uuid PRIMARY KEY, tenant_id text NOT NULL, "
        "state text NOT NULL, lease_owner text, lease_fencing_token integer NOT NULL)"
    )
    conn.exec_driver_sql(
        "CREATE TABLE onboarding_dispatch_audits (id uuid PRIMARY KEY, tenant_id text NOT NULL, "
        "dispatch_id uuid NOT NULL, worker_id text NOT NULL, database_role text NOT NULL, "
        "action text NOT NULL, fencing_token integer NOT NULL)"
    )
    conn.exec_driver_sql(
        "CREATE TABLE onboarding_grants (principal_id text NOT NULL, capability text NOT NULL, "
        "tenant_id text NOT NULL, application_id text NOT NULL, environment text NOT NULL, "
        "operation text NOT NULL, revoked_at timestamptz, valid_from timestamptz NOT NULL, "
        "expires_at timestamptz NOT NULL, policy_version text NOT NULL)"
    )
    conn.exec_driver_sql("CREATE TABLE onboarding_grant_audits (id uuid PRIMARY KEY)")
    conn.exec_driver_sql("ALTER TABLE onboarding_dispatch_audits ENABLE ROW LEVEL SECURITY")
    conn.exec_driver_sql("ALTER TABLE onboarding_dispatch_audits FORCE ROW LEVEL SECURITY")
    conn.exec_driver_sql(
        f"CREATE POLICY dispatch_audit_migrator ON onboarding_dispatch_audits "
        f'FOR INSERT TO "{migrator_role}" WITH CHECK (true)'
    )
    # Test-only verification path: the test reads back audit rows as the
    # migrator role. Production 024 grants app-role tenant-scoped reads; the
    # migrator has no production read path, so this SELECT policy exists only
    # in the scratch schema to make the test's assertions possible.
    conn.exec_driver_sql(
        f"CREATE POLICY dispatch_audit_migrator_select ON onboarding_dispatch_audits "
        f'FOR SELECT TO "{migrator_role}" USING (true)'
    )
    conn.exec_driver_sql("ALTER TABLE onboarding_grants ENABLE ROW LEVEL SECURITY")
    conn.exec_driver_sql("ALTER TABLE onboarding_grants FORCE ROW LEVEL SECURITY")
    conn.exec_driver_sql(
        f"CREATE POLICY grants_migrator ON onboarding_grants "
        f'FOR ALL TO "{migrator_role}" USING (true) WITH CHECK (true)'
    )
    conn.exec_driver_sql("GRANT SELECT, INSERT, UPDATE ON onboarding_grants TO CURRENT_USER")
    conn.exec_driver_sql(
        f'CREATE FUNCTION "{schema}".record_onboarding_dispatch_audit('
        "p_tenant_id text, p_dispatch_id uuid, p_action text, p_fencing_token integer) "
        "RETURNS void LANGUAGE plpgsql SECURITY DEFINER "
        f'SET search_path = pg_catalog, "{schema}" AS $$ '
        "BEGIN "
        "IF p_action NOT IN ('CLAIM', 'RENEW', 'RELEASE', 'STALE_FENCE') THEN "
        "RAISE EXCEPTION 'invalid onboarding dispatch audit action'; END IF; "
        "IF p_tenant_id IS DISTINCT FROM current_setting('app.tenant_id', true) THEN "
        "RAISE EXCEPTION 'onboarding audit tenant context mismatch'; END IF; "
        "INSERT INTO onboarding_dispatch_audits "
        "(id, tenant_id, dispatch_id, worker_id, database_role, action, fencing_token) "
        "VALUES (gen_random_uuid(), p_tenant_id, p_dispatch_id, "
        "current_setting('iap.worker_id', true), session_user::text, p_action, p_fencing_token); "
        "END $$"
    )
    conn.exec_driver_sql(
        f'CREATE FUNCTION "{schema}".onboarding_has_platform_grant('
        "p_principal_id text, p_capability text, p_application_id text, p_environment text, "
        "p_operation text, p_policy_version text, p_at timestamptz) RETURNS boolean "
        "LANGUAGE sql STABLE SECURITY DEFINER "
        f'SET search_path = pg_catalog, "{schema}" AS $$ '
        "SELECT EXISTS (SELECT 1 FROM onboarding_grants g "
        "WHERE g.principal_id = p_principal_id AND g.capability = p_capability "
        "AND g.tenant_id = '__platform__' AND g.application_id = p_application_id "
        "AND g.environment = p_environment AND g.operation = p_operation "
        "AND g.policy_version = p_policy_version AND g.revoked_at IS NULL "
        "AND g.valid_from <= p_at AND p_at < g.expires_at) $$"
    )
    conn.exec_driver_sql(
        f'GRANT EXECUTE ON FUNCTION "{schema}".record_onboarding_dispatch_audit(text, uuid, text, integer) TO "{worker_role}"'
    )
    conn.exec_driver_sql(
        f'GRANT EXECUTE ON FUNCTION "{schema}".onboarding_has_platform_grant(text, text, text, text, text, text, timestamptz) TO "{app_role}"'
    )


def _platform_grant_result(
    conn: Any, schema: str, *, principal_id: str = "principal-a", **overrides: str
) -> bool:
    values = {
        "capability": "tenant:onboard",
        "application_id": "app-a",
        "environment": "production",
        "operation": "create",
        "policy_version": "policy-v1",
    }
    values.update(overrides)
    result = conn.exec_driver_sql(
        f'SELECT "{schema}".onboarding_has_platform_grant(%s, %s, %s, %s, %s, %s, now())',
        (
            principal_id,
            values["capability"],
            values["application_id"],
            values["environment"],
            values["operation"],
            values["policy_version"],
        ),
    ).scalar_one()
    return bool(result)


def test_dispatch_audit_function_enforces_context_and_database_attribution(pg_roles) -> None:  # type: ignore[no-untyped-def]
    _admin, roles, engines, schema = pg_roles
    dispatch_id = uuid.uuid4()
    with engines["migrator"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        _create_function_mirrors(conn, schema, roles["worker"], roles["app"], roles["migrator"])
        conn.exec_driver_sql(
            "INSERT INTO onboarding_dispatches VALUES (%s, 'tenant-a', 'CLAIMED', 'worker-a', 4)",
            (str(dispatch_id),),
        )

    mismatch_connection = engines["worker"].connect()
    mismatch_transaction = mismatch_connection.begin()
    mismatch_connection.exec_driver_sql(f'SET search_path TO "{schema}"')
    mismatch_connection.exec_driver_sql("SELECT set_config('app.tenant_id', 'tenant-a', true)")
    mismatch_connection.exec_driver_sql("SELECT set_config('iap.worker_id', 'worker-a', true)")
    try:
        with pytest.raises(DBAPIError):
            mismatch_connection.exec_driver_sql(
                f'SELECT "{schema}".record_onboarding_dispatch_audit(%s, %s, %s, %s)',
                ("tenant-b", str(dispatch_id), "CLAIM", 4),
            )
    finally:
        mismatch_transaction.rollback()
        mismatch_connection.close()

    with engines["worker"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('app.tenant_id', 'tenant-a', true)")
        conn.exec_driver_sql("SELECT set_config('iap.worker_id', 'worker-a', true)")
        conn.exec_driver_sql(
            f'SELECT "{schema}".record_onboarding_dispatch_audit(%s, %s, %s, %s)',
            ("tenant-a", str(dispatch_id), "CLAIM", 4),
        )
    with engines["worker"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        with pytest.raises(DBAPIError):
            conn.exec_driver_sql("SELECT * FROM onboarding_dispatch_audits")

    with engines["migrator"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        audit = (
            conn.exec_driver_sql(
                "SELECT tenant_id, dispatch_id, worker_id, database_role, action, fencing_token "
                "FROM onboarding_dispatch_audits"
            )
            .mappings()
            .one()
        )
        assert audit["tenant_id"] == "tenant-a"
        assert audit["dispatch_id"] == dispatch_id
        assert audit["worker_id"] == "worker-a"
        assert audit["database_role"] == roles["worker"]
        assert audit["action"] == "CLAIM"
        assert audit["fencing_token"] == 4


def test_platform_grant_function_exact_scope_expiry_revocation_and_policy(pg_roles) -> None:  # type: ignore[no-untyped-def]
    _admin, roles, engines, schema = pg_roles
    with engines["migrator"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        _create_function_mirrors(conn, schema, roles["worker"], roles["app"], roles["migrator"])
        conn.exec_driver_sql(
            "INSERT INTO onboarding_grants VALUES "
            "('principal-a', 'tenant:onboard', '__platform__', 'app-a', 'production', "
            "'create', NULL, now() - interval '1 minute', now() + interval '1 hour', 'policy-v1')"
        )

    with engines["app"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        assert _platform_grant_result(conn, schema) is True
        assert _platform_grant_result(conn, schema, principal_id="principal-other") is False
        assert _platform_grant_result(conn, schema, capability="tenant:read") is False
        assert _platform_grant_result(conn, schema, operation="delete") is False
        assert _platform_grant_result(conn, schema, policy_version="policy-v2") is False
        assert _platform_grant_result(conn, schema, application_id="app-b") is False
        assert _platform_grant_result(conn, schema, environment="staging") is False
        assert (
            conn.exec_driver_sql(
                f'SELECT "{schema}".onboarding_has_platform_grant('
                "'principal-a', 'tenant:onboard', 'app-a', 'production', 'create', 'policy-v1', "
                "now() + interval '2 hours')"
            ).scalar_one()
            is False
        )

    with engines["migrator"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("UPDATE onboarding_grants SET revoked_at = now()")
    with engines["app"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        assert _platform_grant_result(conn, schema) is False


def test_dual_person_is_procedural_not_database_enforced(pg_roles) -> None:  # type: ignore[no-untyped-def]
    """Pin v0.1's accepted limitation: direct migrator DML bypasses paired audit."""
    _admin, roles, engines, schema = pg_roles
    with engines["migrator"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        _create_function_mirrors(conn, schema, roles["worker"], roles["app"], roles["migrator"])
        conn.exec_driver_sql(
            "INSERT INTO onboarding_grants VALUES "
            "('principal-a', 'tenant:onboard', '__platform__', 'app-a', 'production', "
            "'create', NULL, now() - interval '1 minute', now() + interval '1 hour', 'policy-v1')"
        )
        assert (
            conn.exec_driver_sql("SELECT count(*) FROM onboarding_grant_audits").scalar_one() == 0
        )

    with engines["app"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        assert _platform_grant_result(conn, schema) is True


@pytest.fixture
def pg_roles():  # type: ignore[no-untyped-def]
    # Scratch-schema fidelity rules (learned 2026-10-22): the mirror must
    # reproduce the production RLS posture it claims to prove, or the test
    # proves nothing. That means: ENABLE + FORCE RLS on every table that has
    # it in 023/024, the 023-equivalent tenant policy wherever 024 grants a
    # role access without its own policy, and tenant GUC set before any
    # migrator seed INSERT (migrator has no bypass). A missing policy fails
    # closed with an InsufficientPrivilege that looks like a product bug —
    # check the mirror first.
    if not _reachable():
        pytest.skip("IAP onboarding PostgreSQL test server is unreachable")
    from sqlalchemy import create_engine

    admin_engine = create_engine(_connect_url())
    prefix = f"iap_onboarding_{uuid.uuid4().hex[:10]}"
    roles = {
        key: f"{prefix}_{suffix}"
        for key, suffix in {
            "migrator": "migrator",
            "app": "app",
            "worker": "worker",
            "registry_admin": "registry_admin",
        }.items()
    }
    passwords = {key: secrets.token_urlsafe(24) for key in roles}
    engines = {}
    schema = f"{prefix}_schema"
    try:
        with admin_engine.begin() as conn:
            for key, role in roles.items():
                conn.exec_driver_sql(
                    f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{passwords[key]}' "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                )
                conn.exec_driver_sql(f'GRANT "{role}" TO CURRENT_USER')
        with admin_engine.begin() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}" AUTHORIZATION CURRENT_USER')
            conn.exec_driver_sql(f'ALTER SCHEMA "{schema}" OWNER TO "{roles["migrator"]}"')
            conn.exec_driver_sql(
                f'GRANT USAGE ON SCHEMA "{schema}" TO "{roles["app"]}", '
                f'"{roles["worker"]}", "{roles["registry_admin"]}"'
            )
        for key, role in roles.items():
            engines[key] = create_engine(_connect_url(role, passwords[key]))
        yield admin_engine, roles, engines, schema
    finally:
        migrator_engine = engines.get("migrator")
        if migrator_engine is not None:
            with migrator_engine.begin() as conn:
                conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        with admin_engine.begin() as conn:
            for role in roles.values():
                conn.exec_driver_sql(f'REVOKE "{role}" FROM CURRENT_USER')
                conn.exec_driver_sql(f'DROP ROLE IF EXISTS "{role}"')
        for engine in engines.values():
            engine.dispose()
        admin_engine.dispose()


def test_actual_roles_dispatch_claim_scope_and_privileges(pg_roles) -> None:  # type: ignore[no-untyped-def]
    _admin, roles, engines, schema = pg_roles
    migrator = engines["migrator"]
    with migrator.begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql(
            "CREATE TABLE tenant_registry (canonical_id text PRIMARY KEY, display_slug text NOT NULL)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE onboarding_requests (tenant_id text NOT NULL, request_id uuid NOT NULL, "
            "intake_json jsonb NOT NULL, PRIMARY KEY (tenant_id, request_id))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE onboarding_dispatches (id uuid PRIMARY KEY, tenant_id text NOT NULL, "
            "state text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), lease_owner text, "
            "lease_fencing_token integer NOT NULL DEFAULT 0, lease_expires_at timestamptz, "
            "attempts integer NOT NULL DEFAULT 0, next_attempt_at timestamptz, temporal_run_id text, "
            "last_error_code text, updated_at timestamptz NOT NULL DEFAULT now())"
        )
        conn.exec_driver_sql("CREATE TABLE unrelated_secret_store (secret_value text NOT NULL)")
        conn.exec_driver_sql("INSERT INTO unrelated_secret_store VALUES ('do-not-read')")
        conn.exec_driver_sql(
            "CREATE TABLE onboarding_grants (principal_id text NOT NULL, capability text NOT NULL, "
            "tenant_id text NOT NULL, application_id text NOT NULL, environment text NOT NULL, "
            "operation text NOT NULL, revoked_at timestamptz, valid_from timestamptz NOT NULL, "
            "expires_at timestamptz NOT NULL, policy_version text NOT NULL, issuer_id text NOT NULL, "
            "approved_by text NOT NULL, approval_digest text NOT NULL)"
        )
        conn.exec_driver_sql("CREATE TABLE onboarding_dispatch_audits (id uuid PRIMARY KEY)")
        conn.exec_driver_sql("CREATE TABLE onboarding_grant_audits (id uuid PRIMARY KEY)")
        conn.exec_driver_sql("ALTER TABLE tenant_registry ENABLE ROW LEVEL SECURITY")
        conn.exec_driver_sql("ALTER TABLE tenant_registry FORCE ROW LEVEL SECURITY")
        conn.exec_driver_sql(
            "CREATE POLICY registry_tenant ON tenant_registry USING "
            "(canonical_id = current_setting('app.tenant_id', true)) WITH CHECK "
            "(canonical_id = current_setting('app.tenant_id', true))"
        )
        conn.exec_driver_sql(
            f'CREATE POLICY registry_admin ON tenant_registry TO "{roles["registry_admin"]}" '
            "USING (true) WITH CHECK (true)"
        )
        conn.exec_driver_sql("ALTER TABLE onboarding_dispatches ENABLE ROW LEVEL SECURITY")
        conn.exec_driver_sql("ALTER TABLE onboarding_dispatches FORCE ROW LEVEL SECURITY")
        conn.exec_driver_sql(
            "CREATE POLICY dispatch_tenant ON onboarding_dispatches USING "
            "(tenant_id = current_setting('app.tenant_id', true)) WITH CHECK "
            "(tenant_id = current_setting('app.tenant_id', true))"
        )
        conn.exec_driver_sql(
            f'CREATE POLICY dispatch_worker ON onboarding_dispatches TO "{roles["worker"]}" USING (true) '
            "WITH CHECK (lease_owner IS NULL OR lease_owner = current_setting('iap.worker_id', true))"
        )
        conn.exec_driver_sql("ALTER TABLE onboarding_requests ENABLE ROW LEVEL SECURITY")
        conn.exec_driver_sql("ALTER TABLE onboarding_requests FORCE ROW LEVEL SECURITY")
        conn.exec_driver_sql(
            "CREATE POLICY request_tenant ON onboarding_requests USING "
            "(tenant_id = current_setting('app.tenant_id', true)) WITH CHECK "
            "(tenant_id = current_setting('app.tenant_id', true))"
        )
        conn.exec_driver_sql("ALTER TABLE onboarding_grants ENABLE ROW LEVEL SECURITY")
        conn.exec_driver_sql("ALTER TABLE onboarding_grants FORCE ROW LEVEL SECURITY")
        conn.exec_driver_sql(
            f'CREATE POLICY grant_tenant ON onboarding_grants TO "{roles["app"]}" USING '
            "(tenant_id = current_setting('app.tenant_id', true))"
        )
        conn.exec_driver_sql(
            f'CREATE POLICY grant_admin ON onboarding_grants TO "{roles["registry_admin"]}" USING (true)'
        )
        conn.exec_driver_sql(
            "GRANT SELECT (id, tenant_id, state, created_at, lease_owner, lease_fencing_token, "
            "lease_expires_at, attempts, next_attempt_at, temporal_run_id, last_error_code, updated_at) "
            f'ON onboarding_dispatches TO "{roles["worker"]}"'
        )
        conn.exec_driver_sql(
            "GRANT UPDATE (state, lease_owner, lease_fencing_token, lease_expires_at, attempts, "
            "next_attempt_at, temporal_run_id, last_error_code, updated_at) "
            f'ON onboarding_dispatches TO "{roles["worker"]}"'
        )
        conn.exec_driver_sql(f'GRANT SELECT ON onboarding_requests TO "{roles["worker"]}"')
        conn.exec_driver_sql(
            f'GRANT SELECT, INSERT ON tenant_registry TO "{roles["registry_admin"]}"'
        )
        conn.exec_driver_sql(
            f'GRANT UPDATE (display_slug) ON tenant_registry TO "{roles["registry_admin"]}"'
        )
        conn.exec_driver_sql(f'GRANT SELECT, INSERT ON tenant_registry TO "{roles["app"]}"')
        conn.exec_driver_sql(
            f'GRANT SELECT ON onboarding_grants TO "{roles["app"]}", "{roles["registry_admin"]}"'
        )
        conn.exec_driver_sql(f'GRANT SELECT, INSERT ON onboarding_requests TO "{roles["app"]}"')
        conn.exec_driver_sql(f'GRANT SELECT, INSERT ON onboarding_dispatches TO "{roles["app"]}"')

    with engines["app"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('app.tenant_id', 'tenant-a', true)")
        conn.exec_driver_sql("INSERT INTO tenant_registry VALUES ('tenant-a', 'tenant-a')")
        conn.exec_driver_sql(
            "INSERT INTO onboarding_requests VALUES "
            "('tenant-a', gen_random_uuid(), '{\"value\":\"a\"}')"
        )
        conn.exec_driver_sql(
            "INSERT INTO onboarding_dispatches (id, tenant_id, state) "
            "VALUES (gen_random_uuid(), 'tenant-a', 'PENDING')"
        )
        conn.exec_driver_sql("SELECT set_config('app.tenant_id', 'tenant-b', true)")
        conn.exec_driver_sql("INSERT INTO tenant_registry VALUES ('tenant-b', 'tenant-b')")
        conn.exec_driver_sql(
            "INSERT INTO onboarding_requests VALUES "
            "('tenant-b', gen_random_uuid(), '{\"value\":\"b\"}')"
        )
        conn.exec_driver_sql(
            "INSERT INTO onboarding_dispatches (id, tenant_id, state) "
            "VALUES (gen_random_uuid(), 'tenant-b', 'PENDING')"
        )

    claim_sql = (
        "WITH candidate AS (SELECT id FROM onboarding_dispatches WHERE state IN ('PENDING', 'RETRYABLE') "
        "ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1) "
        "UPDATE onboarding_dispatches d SET state='CLAIMED', lease_owner='worker-a', "
        "lease_fencing_token=d.lease_fencing_token+1, lease_expires_at=now()+interval '1 minute', "
        "attempts=d.attempts+1 FROM candidate WHERE d.id=candidate.id "
        "RETURNING d.tenant_id, d.lease_fencing_token"
    )
    worker = engines["worker"]
    with worker.begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('iap.worker_id', 'worker-a', true)")
        for _ in range(2):
            conn.exec_driver_sql("SELECT set_config('app.tenant_id', 'tenant-a', true)")
            assert (
                conn.exec_driver_sql("SELECT count(*) FROM onboarding_requests").scalar_one() == 1
            )
        # Worker requests are visible only after setting the tenant context.
        conn.exec_driver_sql("SELECT set_config('app.tenant_id', 'tenant-b', true)")
        assert conn.exec_driver_sql("SELECT count(*) FROM onboarding_requests").scalar_one() == 1

    # Each dispatch state change must use the same row tenant context; the
    # cross-tenant worker policy is still dispatch-table scoped.
    with worker.begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('iap.worker_id', 'worker-a', true)")
        conn.exec_driver_sql("SELECT set_config('app.tenant_id', 'tenant-a', true)")
        first = conn.exec_driver_sql(claim_sql).first()
    with worker.begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('iap.worker_id', 'worker-a', true)")
        conn.exec_driver_sql("SELECT set_config('app.tenant_id', 'tenant-b', true)")
        second = conn.exec_driver_sql(claim_sql).first()
    assert first is not None and second is not None
    assert first.tenant_id == "tenant-a" and second.tenant_id == "tenant-b"
    assert first.lease_fencing_token == second.lease_fencing_token == 1
    with worker.begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('iap.worker_id', 'worker-a', true)")
        conn.exec_driver_sql("SELECT set_config('app.tenant_id', 'tenant-a', true)")
        assert conn.exec_driver_sql(claim_sql).first() is None
        conn.exec_driver_sql("SELECT set_config('app.tenant_id', 'tenant-b', true)")
        assert conn.exec_driver_sql(claim_sql).first() is None

    with worker.begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('iap.worker_id', 'worker-a', true)")
        with pytest.raises(DBAPIError):
            conn.exec_driver_sql("UPDATE onboarding_dispatches SET workflow_id = 'forbidden'")
    with worker.begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('iap.worker_id', 'worker-a', true)")
        with pytest.raises(DBAPIError):
            conn.exec_driver_sql("SELECT * FROM unrelated_secret_store")
        with pytest.raises(DBAPIError):
            conn.exec_driver_sql("SELECT * FROM onboarding_grants")
        with pytest.raises(DBAPIError):
            conn.exec_driver_sql(
                "INSERT INTO onboarding_dispatch_audits VALUES (gen_random_uuid())"
            )
    with worker.begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('iap.worker_id', 'worker-a', true)")
        assert conn.exec_driver_sql("SELECT count(*) FROM onboarding_requests").scalar_one() == 0
        conn.exec_driver_sql("SELECT set_config('app.tenant_id', 'tenant-a', true)")
        assert conn.exec_driver_sql("SELECT count(*) FROM onboarding_requests").scalar_one() == 1
    with worker.begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('iap.worker_id', 'worker-a', true)")
        with pytest.raises(DBAPIError):
            conn.exec_driver_sql("SELECT * FROM tenant_registry")
    with worker.begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('iap.worker_id', 'worker-a', true)")
        with pytest.raises(DBAPIError):
            conn.exec_driver_sql("UPDATE onboarding_dispatches SET tenant_id='tenant-z'")
        with pytest.raises(DBAPIError):
            conn.exec_driver_sql(
                "UPDATE onboarding_dispatches SET lease_owner='worker-b' WHERE tenant_id='tenant-a'"
            )

    app = engines["app"]
    with app.begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('app.tenant_id', 'tenant-a', true)")
        assert conn.exec_driver_sql("SELECT count(*) FROM tenant_registry").scalar_one() == 1
        assert conn.exec_driver_sql("SELECT count(*) FROM onboarding_requests").scalar_one() == 1
        assert conn.exec_driver_sql("SELECT count(*) FROM onboarding_grants").scalar_one() == 0
    for role in ("app", "worker", "registry_admin"):
        with engines[role].begin() as conn:
            conn.exec_driver_sql(f'SET search_path TO "{schema}"')
            with pytest.raises(DBAPIError):
                conn.exec_driver_sql("CREATE TABLE forbidden_ddl (id integer)")

    for role in ("app", "worker", "registry_admin"):
        with engines[role].begin() as conn:
            conn.exec_driver_sql(f'SET search_path TO "{schema}"')
            conn.exec_driver_sql("SET row_security = off")
            with pytest.raises(DBAPIError):
                conn.exec_driver_sql("SELECT * FROM onboarding_dispatches")
            with pytest.raises(DBAPIError):
                conn.exec_driver_sql("SELECT * FROM onboarding_requests")

    with engines["migrator"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        # NOTE: unrelated_secret_store already exists from fixture setup;
        # migrator DDL is proven by migrator_ddl_allowed below.
        conn.exec_driver_sql("CREATE TABLE migrator_ddl_allowed (id integer)")
        conn.exec_driver_sql("INSERT INTO onboarding_grant_audits VALUES (gen_random_uuid())")

    registry_admin = engines["registry_admin"]
    with registry_admin.begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        assert conn.exec_driver_sql("SELECT count(*) FROM tenant_registry").scalar_one() == 2
        assert conn.exec_driver_sql("SELECT count(*) FROM onboarding_grants").scalar_one() == 0
        with pytest.raises(DBAPIError):
            conn.exec_driver_sql("SELECT * FROM onboarding_dispatches")
        with pytest.raises(DBAPIError):
            conn.exec_driver_sql("SELECT * FROM onboarding_dispatch_audits")


def test_worker_skip_locked_has_one_winner(pg_roles) -> None:  # type: ignore[no-untyped-def]
    _admin, roles, engines, schema = pg_roles
    with engines["migrator"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("CREATE TABLE unrelated_secret_store (secret_value text NOT NULL)")
        conn.exec_driver_sql("INSERT INTO unrelated_secret_store VALUES ('secret')")
        conn.exec_driver_sql(
            "CREATE TABLE onboarding_dispatches (id uuid PRIMARY KEY, tenant_id text NOT NULL, "
            "state text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), lease_owner text, "
            "lease_fencing_token integer NOT NULL DEFAULT 0, lease_expires_at timestamptz, "
            "attempts integer NOT NULL DEFAULT 0, next_attempt_at timestamptz, temporal_run_id text, "
            "last_error_code text, updated_at timestamptz NOT NULL DEFAULT now())"
        )
        conn.exec_driver_sql("ALTER TABLE onboarding_dispatches ENABLE ROW LEVEL SECURITY")
        conn.exec_driver_sql("ALTER TABLE onboarding_dispatches FORCE ROW LEVEL SECURITY")
        conn.exec_driver_sql(
            "CREATE POLICY dispatch_tenant ON onboarding_dispatches USING "
            "(tenant_id = current_setting('app.tenant_id', true)) WITH CHECK "
            "(tenant_id = current_setting('app.tenant_id', true))"
        )
        conn.exec_driver_sql(
            f'CREATE POLICY dispatch_worker ON onboarding_dispatches TO "{roles["worker"]}" USING (true) '
            "WITH CHECK (lease_owner IS NULL OR lease_owner = current_setting('iap.worker_id', true))"
        )
        conn.exec_driver_sql(
            "GRANT SELECT (id, tenant_id, state, created_at, lease_owner, lease_fencing_token, "
            "lease_expires_at, attempts, next_attempt_at, temporal_run_id, last_error_code, updated_at) "
            f'ON onboarding_dispatches TO "{roles["worker"]}"'
        )
        conn.exec_driver_sql(
            "GRANT UPDATE (state, lease_owner, lease_fencing_token, lease_expires_at, attempts, "
            "next_attempt_at, temporal_run_id, last_error_code, updated_at) "
            f'ON onboarding_dispatches TO "{roles["worker"]}"'
        )
        # Seed rows need the tenant GUC: the tenant policy applies to all
        # roles including the table-owning migrator under FORCE RLS.
        conn.exec_driver_sql("SELECT set_config('app.tenant_id', 'tenant-a', true)")
        conn.exec_driver_sql(
            "INSERT INTO onboarding_dispatches (id, tenant_id, state) "
            "VALUES (gen_random_uuid(), 'tenant-a', 'PENDING')"
        )

    barrier = Barrier(2)
    # Mirrors the production claim predicate: due PENDING or RETRYABLE rows.
    claim_sql = (
        "WITH candidate AS (SELECT id FROM onboarding_dispatches WHERE state IN ('PENDING', 'RETRYABLE') "
        "ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1) "
        "UPDATE onboarding_dispatches d SET state='CLAIMED', lease_owner='claimant', "
        "lease_fencing_token=d.lease_fencing_token+1, lease_expires_at=now()+interval '1 minute', "
        "attempts=d.attempts+1 FROM candidate WHERE d.id=candidate.id "
        "RETURNING d.id, d.tenant_id, d.lease_fencing_token"
    )

    def attempt() -> Any:
        # NOTE: no failing statement may execute inside this transaction.
        # PostgreSQL turns COMMIT-after-error into a silent rollback, so a
        # deliberate negative check here would undo the claim it just made
        # and the test would pass the race while losing the row. Negative
        # checks run on separate connections below.
        connection = engines["worker"].connect()
        transaction = connection.begin()
        connection.exec_driver_sql(f'SET search_path TO "{schema}"')
        connection.exec_driver_sql("SELECT set_config('iap.worker_id', 'claimant', true)")
        barrier.wait()
        result = connection.exec_driver_sql(claim_sql).first()
        sleep(0.1)
        transaction.commit()
        connection.close()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(result is not None for result in results) == 1
    with engines["worker"].connect() as connection:
        connection.exec_driver_sql(f'SET search_path TO "{schema}"')
        with pytest.raises(DBAPIError):
            connection.exec_driver_sql("SELECT * FROM unrelated_secret_store")

    with engines["migrator"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('app.tenant_id', 'tenant-c', true)")
        conn.exec_driver_sql(
            "INSERT INTO onboarding_dispatches (id, tenant_id, state) "
            "VALUES (gen_random_uuid(), 'tenant-c', 'PENDING')"
        )

    with engines["worker"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('iap.worker_id', 'claimant', true)")
        conn.exec_driver_sql("SELECT set_config('app.tenant_id', 'tenant-c', true)")
        first_claim = conn.exec_driver_sql(claim_sql).first()
        assert first_claim is not None
        assert first_claim.tenant_id == "tenant-c"
        assert first_claim.lease_fencing_token == 1
        conn.exec_driver_sql(
            "UPDATE onboarding_dispatches SET lease_expires_at = now() - interval '1 second' "
            "WHERE tenant_id='tenant-c' AND lease_owner='claimant' "
            "AND lease_fencing_token=1"
        )
        stale_requeue = conn.exec_driver_sql(
            "WITH candidate AS (SELECT id FROM onboarding_dispatches WHERE tenant_id='tenant-c' "
            "AND state='CLAIMED' AND lease_expires_at <= now() FOR UPDATE SKIP LOCKED LIMIT 1) "
            "UPDATE onboarding_dispatches d SET state='RETRYABLE', lease_owner=NULL, "
            "lease_expires_at=NULL FROM candidate WHERE d.id=candidate.id RETURNING d.id"
        ).first()
        assert stale_requeue is not None

    with engines["worker"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('iap.worker_id', 'claimant', true)")
        with pytest.raises(DBAPIError):
            conn.exec_driver_sql(
                "UPDATE onboarding_dispatches SET lease_owner='another-worker' "
                "WHERE lease_owner='claimant'"
            )
    with engines["worker"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('iap.worker_id', 'claimant', true)")
        conn.exec_driver_sql("SELECT set_config('app.tenant_id', 'tenant-c', true)")
        second_claim = conn.exec_driver_sql(claim_sql).first()
        assert second_claim is not None
        assert second_claim.tenant_id == "tenant-c"
        assert second_claim.lease_fencing_token == 2
        stale_update = conn.exec_driver_sql(
            "UPDATE onboarding_dispatches SET state='ACCEPTED' WHERE tenant_id='tenant-c' "
            "AND lease_owner='claimant' AND lease_fencing_token=1 RETURNING id"
        ).first()
        assert stale_update is None
        renewed = conn.exec_driver_sql(
            "UPDATE onboarding_dispatches SET lease_expires_at=now()+interval '2 minutes' "
            "WHERE tenant_id='tenant-c' AND lease_owner='claimant' AND lease_fencing_token=2 "
            "RETURNING lease_fencing_token"
        ).first()
        assert renewed is not None and renewed.lease_fencing_token == 2
