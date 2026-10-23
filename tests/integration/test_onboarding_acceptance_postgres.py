"""Opt-in actual-role checks for Phase 2 acceptance and cancellation paths."""

from __future__ import annotations

import os
import secrets
import socket
import uuid
from typing import Any
from urllib.parse import urlparse

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

RUN_DB_TESTS = os.environ.get("IAP_RUN_ONBOARDING_DB_TESTS", "0") == "1"
PG_URL = os.environ.get("IAP_TEST_PG_URL", "postgresql://iap:iap@localhost:5432/iap_test")
pytestmark = pytest.mark.skipif(
    not RUN_DB_TESTS,
    reason="Opt-in actual-role onboarding DB tests (IAP_RUN_ONBOARDING_DB_TESTS=1)",
)


def _url(username: str | None = None, password: str | None = None):  # type: ignore[no-untyped-def]
    url = make_url(PG_URL)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    if username is not None:
        url = url.set(username=username, password=password)
    return url


@pytest.fixture
def acceptance_roles():  # type: ignore[no-untyped-def]
    parsed = urlparse(PG_URL)
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 5432), timeout=1):
            pass
    except OSError:
        pytest.skip("IAP onboarding PostgreSQL test server is unreachable")

    admin = create_engine(_url())
    prefix = f"iap_accept_{uuid.uuid4().hex[:10]}"
    roles = {
        name: f"{prefix}_{name}"
        for name in ("app", "worker", "registry_admin", "migrator", "nobody")
    }
    passwords = {name: secrets.token_urlsafe(24) for name in roles}
    schema = f"{prefix}_schema"
    try:
        with admin.begin() as conn:
            for name, role in roles.items():
                conn.exec_driver_sql(
                    f'CREATE ROLE "{role}" LOGIN PASSWORD \'{passwords[name]}\' '
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                )
                conn.exec_driver_sql(f'GRANT "{role}" TO CURRENT_USER')
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}" AUTHORIZATION CURRENT_USER')
            conn.exec_driver_sql(f'ALTER SCHEMA "{schema}" OWNER TO "{roles["migrator"]}"')
            conn.exec_driver_sql(
                f'GRANT USAGE ON SCHEMA "{schema}" TO "{roles["app"]}", "{roles["worker"]}", '
                f'"{roles["registry_admin"]}", "{roles["migrator"]}"'
            )
        _create_schema(admin, schema, roles)
        yield admin, roles, schema
    finally:
        with admin.begin() as conn:
            conn.exec_driver_sql(f'SET ROLE "{roles["migrator"]}"')
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        with admin.begin() as conn:
            conn.exec_driver_sql("RESET ROLE")
            for role in roles.values():
                conn.exec_driver_sql(f'REVOKE "{role}" FROM CURRENT_USER')
                conn.exec_driver_sql(f'DROP ROLE IF EXISTS "{role}"')
        admin.dispose()


def _create_schema(admin: Any, schema: str, roles: dict[str, str]) -> None:
    app = roles["app"]
    worker = roles["worker"]
    with admin.begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("CREATE TABLE tenant_registry (canonical_id text PRIMARY KEY)")
        conn.exec_driver_sql(
            "CREATE TABLE background_jobs (id uuid PRIMARY KEY, tenant_id text NOT NULL)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE onboarding_requests (id uuid PRIMARY KEY, tenant_id text NOT NULL, "
            "request_id uuid NOT NULL, revision integer NOT NULL, idempotency_key text NOT NULL, "
            "content_hash text NOT NULL, state text NOT NULL, state_version integer NOT NULL DEFAULT 1, "
            "actor_id text NOT NULL, job_id uuid, "
            "updated_at timestamptz NOT NULL DEFAULT now(), "
            "UNIQUE (tenant_id,request_id,revision), UNIQUE (tenant_id,idempotency_key))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE onboarding_dispatches (id uuid PRIMARY KEY, tenant_id text NOT NULL, "
            "event_id uuid NOT NULL UNIQUE, request_id uuid NOT NULL, revision integer NOT NULL, "
            "job_id uuid NOT NULL, workflow_id text NOT NULL, generation integer NOT NULL DEFAULT 1, "
            "state text NOT NULL DEFAULT 'PENDING', lease_owner text, lease_fencing_token integer NOT NULL DEFAULT 0, "
            "lease_expires_at timestamptz, temporal_run_id text, last_error_code text, "
            "UNIQUE (tenant_id,workflow_id), UNIQUE (tenant_id,request_id,revision,generation))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE onboarding_request_audits (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
            "tenant_id text NOT NULL, request_id uuid NOT NULL, revision integer NOT NULL, "
            "actor_sub text NOT NULL, database_role text NOT NULL, action text NOT NULL, "
            "content_hash text NOT NULL, idempotency_key text NOT NULL, created_at timestamptz DEFAULT now(), "
            "FOREIGN KEY (tenant_id,request_id,revision) REFERENCES onboarding_requests "
            "(tenant_id,request_id,revision), CHECK(action IN ('CREATE','ACCEPT')))"
        )
        for table, tenant_column in (
            ("tenant_registry", "canonical_id"),
            ("onboarding_requests", "tenant_id"),
            ("onboarding_dispatches", "tenant_id"),
            ("onboarding_request_audits", "tenant_id"),
        ):
            conn.exec_driver_sql(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            conn.exec_driver_sql(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            conn.exec_driver_sql(
                f"CREATE POLICY {table}_tenant ON {table} FOR ALL TO PUBLIC "
                f"USING ({tenant_column}=current_setting('app.tenant_id',true)) "
                f"WITH CHECK ({tenant_column}=current_setting('app.tenant_id',true))"
            )
        conn.exec_driver_sql(
            f'CREATE POLICY dispatches_worker_update ON onboarding_dispatches FOR UPDATE TO "{worker}" '
            "USING (true) WITH CHECK (lease_owner IS NULL OR lease_owner=current_setting('iap.worker_id',true))"
        )
        conn.exec_driver_sql(
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_registry, onboarding_requests, onboarding_dispatches, background_jobs TO "{app}"'
        )
        conn.exec_driver_sql(f'GRANT SELECT ON onboarding_request_audits TO "{app}"')
        conn.exec_driver_sql(f'GRANT SELECT ON onboarding_dispatches TO "{worker}"')
        conn.exec_driver_sql(f'GRANT SELECT ON onboarding_requests TO "{worker}"')
        conn.exec_driver_sql(
            f'GRANT UPDATE (state, state_version, updated_at) ON onboarding_requests TO "{worker}"'
        )
        # NOTE: gen_random_uuid() needs no explicit grant (executable by PUBLIC);
        # granting it would block role teardown with dependency errors.
        conn.exec_driver_sql("INSERT INTO tenant_registry VALUES ('tenant-a'), ('tenant-b')")
        conn.exec_driver_sql(
            f'CREATE FUNCTION "{schema}".record_onboarding_request_audit('
            "p_tenant_id text,p_request_id uuid,p_revision integer,p_actor_sub text,"
            "p_action text,p_content_hash text,p_idempotency_key text) RETURNS void "
            f'LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog,"{schema}" AS $$ '
            "BEGIN IF p_tenant_id IS DISTINCT FROM current_setting('app.tenant_id',true) THEN "
            "RAISE EXCEPTION 'onboarding request audit tenant context mismatch'; END IF; "
            "IF p_action NOT IN ('CREATE','ACCEPT') THEN RAISE EXCEPTION 'invalid onboarding request audit action'; END IF; "
            "INSERT INTO onboarding_request_audits(tenant_id,request_id,revision,actor_sub,database_role,"
            "action,content_hash,idempotency_key) VALUES(p_tenant_id,p_request_id,p_revision,p_actor_sub,"
            "session_user::text,p_action,p_content_hash,p_idempotency_key); END; $$"
        )
        conn.exec_driver_sql(
            f'GRANT EXECUTE ON FUNCTION "{schema}".record_onboarding_request_audit(text,uuid,integer,text,text,text,text) TO "{app}"'
        )


def test_acceptance_transaction_commits_all_or_nothing(acceptance_roles) -> None:  # type: ignore[no-untyped-def]
    admin, roles, schema = acceptance_roles
    request_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    with admin.begin() as conn:
        conn.exec_driver_sql(f'SET ROLE "{roles["app"]}"')
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('app.tenant_id','tenant-a',true)")
        conn.exec_driver_sql(
            "INSERT INTO onboarding_requests (id,tenant_id,request_id,revision,idempotency_key,"
            "content_hash,state,state_version,actor_id) VALUES "
            f"(gen_random_uuid(),'tenant-a','{request_id}',1,'acc-key-1','"
            + "c" * 64
            + "','DRAFT',1,'sub-a')"
        )
        updated = conn.exec_driver_sql(
            "UPDATE onboarding_requests SET state='QUEUED', state_version=state_version+1,"
            f"job_id=gen_random_uuid() WHERE tenant_id='tenant-a' AND request_id='{request_id}' "
            "AND revision=1 AND state='DRAFT' RETURNING id, job_id, state_version"
        ).one()
        assert updated.state_version == 2
        job_id = updated.job_id
        conn.exec_driver_sql(
            "INSERT INTO background_jobs (id,tenant_id) VALUES "
            f"('{job_id}','tenant-a')"
        )
        dispatch_id = str(uuid.uuid4())
        conn.exec_driver_sql(
            "INSERT INTO onboarding_dispatches (id,tenant_id,event_id,request_id,revision,"
            "job_id,workflow_id,generation,state,lease_fencing_token) VALUES "
            f"('{dispatch_id}','tenant-a',gen_random_uuid(),'{request_id}',1,'{job_id}',"
            f"'onboarding-tenant-a-{request_id}-r1g1',1,'PENDING',0)"
        )
        conn.exec_driver_sql(
            f'SELECT "{schema}".record_onboarding_request_audit('
            f"'tenant-a','{request_id}',1,'sub-a','ACCEPT','" + "c" * 64 + "','acc-key-1')"
        )
        assert conn.exec_driver_sql(
            "SELECT count(*) FROM onboarding_dispatches"
        ).scalar_one() == 1
        assert (
            conn.exec_driver_sql(
                "SELECT count(*) FROM onboarding_request_audits WHERE action='ACCEPT'"
            ).scalar_one()
            == 1
        )


def test_worker_cancel_transition_allowed_and_cross_tenant_denied(acceptance_roles) -> None:  # type: ignore[no-untyped-def]
    admin, roles, schema = acceptance_roles
    request_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    with admin.begin() as conn:
        conn.exec_driver_sql(f'SET ROLE "{roles["app"]}"')
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('app.tenant_id','tenant-a',true)")
        conn.exec_driver_sql(
            "INSERT INTO onboarding_requests (id,tenant_id,request_id,revision,idempotency_key,"
            "content_hash,state,state_version,actor_id) VALUES "
            f"(gen_random_uuid(),'tenant-a','{request_id}',1,'cancel-key-1','" + "d" * 64 + "','QUEUED',1,'sub-a')"
        )

    with admin.begin() as conn:
        conn.exec_driver_sql(f'SET ROLE "{roles["worker"]}"')
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('app.tenant_id','tenant-a',true)")
        changed = conn.exec_driver_sql(
            f"UPDATE onboarding_requests SET state='CANCELED', state_version=state_version+1 "
            f"WHERE tenant_id='tenant-a' AND request_id='{request_id}' AND revision=1 "
            "AND state IN ('QUEUED','CANCEL_REQUESTED') RETURNING id"
        ).first()
        assert changed is not None
        assert (
            conn.exec_driver_sql(
                f"SELECT state FROM onboarding_requests WHERE request_id='{request_id}'"
            ).scalar_one()
            == "CANCELED"
        )

    with admin.begin() as conn:
        conn.exec_driver_sql(f'SET ROLE "{roles["worker"]}"')
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('app.tenant_id','tenant-b',true)")
        assert (
            conn.exec_driver_sql(
                f"SELECT count(*) FROM onboarding_requests WHERE request_id='{request_id}'"
            ).scalar_one()
            == 0
        )
        with pytest.raises(DBAPIError):
            conn.exec_driver_sql("INSERT INTO onboarding_requests DEFAULT VALUES")


def test_worker_cannot_take_non_cancellation_columns(acceptance_roles) -> None:  # type: ignore[no-untyped-def]
    admin, roles, _schema = acceptance_roles
    with admin.begin() as conn:
        conn.exec_driver_sql(f'SET ROLE "{roles["worker"]}"')
        conn.exec_driver_sql("SELECT set_config('app.tenant_id','tenant-a',true)")
        with pytest.raises(DBAPIError, match="denied|permission"):
            conn.exec_driver_sql(
                "UPDATE onboarding_requests SET content_hash='"
                + "e" * 64
                + "' WHERE tenant_id='tenant-a'"
            )


def test_no_grant_role_cannot_touch_control_store(acceptance_roles) -> None:  # type: ignore[no-untyped-def]
    admin, roles, schema = acceptance_roles
    with admin.begin() as conn:
        conn.exec_driver_sql(f'SET ROLE "{roles["nobody"]}"')
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('app.tenant_id','tenant-a',true)")
        with pytest.raises(DBAPIError):
            conn.exec_driver_sql("SELECT count(*) FROM onboarding_requests")
        with pytest.raises(DBAPIError):
            conn.exec_driver_sql("SELECT count(*) FROM onboarding_dispatches")
