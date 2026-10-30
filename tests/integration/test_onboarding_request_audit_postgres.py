"""Opt-in actual-role checks for migration-026 DRAFT request audit."""

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
def request_audit_roles():  # type: ignore[no-untyped-def]
    parsed = urlparse(PG_URL)
    try:
        with socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or 5432), timeout=1
        ):
            pass
    except OSError:
        pytest.skip("IAP onboarding PostgreSQL test server is unreachable")

    admin = create_engine(_url())
    prefix = f"iap_req_audit_{uuid.uuid4().hex[:10]}"
    roles = {name: f"{prefix}_{name}" for name in ("app", "worker", "registry_admin", "migrator")}
    passwords = {name: secrets.token_urlsafe(24) for name in roles}
    schema = f"{prefix}_schema"
    try:
        with admin.begin() as conn:
            for name, role in roles.items():
                conn.exec_driver_sql(
                    f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{passwords[name]}' "
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
    app, migrator = roles["app"], roles["migrator"]
    with admin.begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("CREATE TABLE tenant_registry (canonical_id text PRIMARY KEY)")
        conn.exec_driver_sql(
            "CREATE TABLE onboarding_requests (id uuid PRIMARY KEY, tenant_id text NOT NULL, "
            "request_id uuid NOT NULL, revision integer NOT NULL, idempotency_key text NOT NULL, "
            "content_hash text NOT NULL, state text NOT NULL, actor_id text NOT NULL, job_id uuid, "
            "UNIQUE (tenant_id,request_id,revision), UNIQUE (tenant_id,idempotency_key), "
            "UNIQUE (tenant_id,request_id,content_hash))"
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
            ("onboarding_request_audits", "tenant_id"),
        ):
            conn.exec_driver_sql(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            conn.exec_driver_sql(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            conn.exec_driver_sql(
                f'CREATE POLICY {table}_tenant ON {table} FOR SELECT TO "{app}" '
                f"USING ({tenant_column}=current_setting('app.tenant_id',true))"
            )
            if table == "onboarding_requests":
                conn.exec_driver_sql(
                    f'CREATE POLICY {table}_app_insert ON {table} FOR INSERT TO "{app}" '
                    "WITH CHECK (tenant_id=current_setting('app.tenant_id',true) AND state='DRAFT' AND job_id IS NULL)"
                )
            if table == "onboarding_request_audits":
                conn.exec_driver_sql(
                    f'CREATE POLICY {table}_migrator_insert ON {table} FOR INSERT TO "{migrator}" WITH CHECK (true)'
                )
        conn.exec_driver_sql(
            f'GRANT SELECT, INSERT ON tenant_registry, onboarding_requests TO "{app}"'
        )
        conn.exec_driver_sql(f'GRANT SELECT ON onboarding_request_audits TO "{app}"')
        conn.exec_driver_sql(f'GRANT SELECT, INSERT ON onboarding_request_audits TO "{migrator}"')
        conn.exec_driver_sql("INSERT INTO tenant_registry VALUES ('tenant-a'),('tenant-b')")
        conn.exec_driver_sql(
            f'CREATE FUNCTION "{schema}".record_onboarding_request_audit('
            "p_tenant_id text,p_request_id uuid,p_revision integer,p_actor_sub text,"
            "p_action text,p_content_hash text,p_idempotency_key text) RETURNS void "
            f'LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,"{schema}" AS $$ '
            "BEGIN IF p_tenant_id IS DISTINCT FROM current_setting('app.tenant_id',true) THEN "
            "RAISE EXCEPTION 'onboarding request audit tenant context mismatch'; END IF; "
            "IF p_action NOT IN ('CREATE','ACCEPT') THEN RAISE EXCEPTION 'invalid onboarding request audit action'; END IF; "
            "INSERT INTO onboarding_request_audits(tenant_id,request_id,revision,actor_sub,database_role,"
            "action,content_hash,idempotency_key) VALUES(p_tenant_id,p_request_id,p_revision,p_actor_sub,"
            "session_user::text,p_action,p_content_hash,p_idempotency_key); END; $$"
        )
        conn.exec_driver_sql(
            f'ALTER FUNCTION "{schema}".record_onboarding_request_audit(text,uuid,integer,text,text,text,text) OWNER TO "{migrator}"'
        )
        conn.exec_driver_sql(
            f'REVOKE ALL ON FUNCTION "{schema}".record_onboarding_request_audit(text,uuid,integer,text,text,text,text) FROM PUBLIC'
        )
        conn.exec_driver_sql(
            f'GRANT EXECUTE ON FUNCTION "{schema}".record_onboarding_request_audit(text,uuid,integer,text,text,text,text) TO "{app}"'
        )


def test_app_executes_request_audit_while_other_roles_are_denied(request_audit_roles) -> None:  # type: ignore[no-untyped-def]
    admin, roles, schema = request_audit_roles
    with admin.begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql(
            "INSERT INTO onboarding_requests VALUES "
            "('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa','tenant-a',"
            "'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',1,'key-a','hash-a','DRAFT','sub-a',NULL)"
        )

    with admin.begin() as conn:
        conn.exec_driver_sql(f'SET ROLE "{roles["app"]}"')
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('app.tenant_id','tenant-a',true)")
        conn.exec_driver_sql(
            f'SELECT "{schema}".record_onboarding_request_audit('
            "'tenant-a','bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',1,'verified-sub','CREATE','hash-a','key-a')"
        )
        audit = conn.exec_driver_sql(
            "SELECT actor_sub,database_role,action FROM onboarding_request_audits"
        ).one()
        assert audit.actor_sub == "verified-sub"
        assert audit.database_role == "iap"
        assert audit.action == "CREATE"
        assert conn.exec_driver_sql("SELECT count(*) FROM onboarding_requests").scalar_one() == 1

    with admin.begin() as conn:
        conn.exec_driver_sql(f'SET ROLE "{roles["app"]}"')
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('app.tenant_id','tenant-b',true)")
        assert conn.exec_driver_sql("SELECT count(*) FROM onboarding_requests").scalar_one() == 0
        assert (
            conn.exec_driver_sql("SELECT count(*) FROM onboarding_request_audits").scalar_one() == 0
        )

    for role in (roles["app"], roles["worker"], roles["registry_admin"]):
        with admin.begin() as conn:
            conn.exec_driver_sql(f'SET ROLE "{role}"')
            conn.exec_driver_sql(f'SET search_path TO "{schema}"')
            with pytest.raises(DBAPIError):
                conn.exec_driver_sql("INSERT INTO onboarding_request_audits DEFAULT VALUES")
    for role in (roles["worker"], roles["registry_admin"]):
        with admin.begin() as conn:
            conn.exec_driver_sql(f'SET ROLE "{role}"')
            conn.exec_driver_sql(f'SET search_path TO "{schema}"')
            with pytest.raises(DBAPIError):
                conn.exec_driver_sql(
                    f'SELECT "{schema}".record_onboarding_request_audit('
                    "'tenant-a','bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',1,'sub','CREATE','hash-a','key-a')"
                )


def test_accept_action_is_recorded_and_other_actions_are_rejected(request_audit_roles) -> None:  # type: ignore[no-untyped-def]
    admin, roles, schema = request_audit_roles
    with admin.begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql(
            "INSERT INTO onboarding_requests VALUES "
            "('cccccccc-cccc-4ccc-8ccc-cccccccccccc','tenant-a',"
            "'dddddddd-dddd-4ddd-8ddd-dddddddddddd',1,'accept-key','accept-hash','DRAFT','sub-a',NULL)"
        )

    with admin.begin() as conn:
        conn.exec_driver_sql(f'SET ROLE "{roles["app"]}"')
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('app.tenant_id','tenant-a',true)")
        conn.exec_driver_sql(
            f'SELECT "{schema}".record_onboarding_request_audit('
            "'tenant-a','dddddddd-dddd-4ddd-8ddd-dddddddddddd',1,'verified-sub','ACCEPT','accept-hash','accept-key')"
        )
        row = conn.exec_driver_sql(
            "SELECT actor_sub,database_role,action,content_hash,idempotency_key "
            "FROM onboarding_request_audits WHERE action='ACCEPT'"
        ).one()
        assert row.actor_sub == "verified-sub"
        assert row.database_role == "iap"
        assert row.action == "ACCEPT"
        assert row.content_hash == "accept-hash"
        assert row.idempotency_key == "accept-key"

    for action in ("CLAIM", "APPROVE", "accept"):
        with admin.begin() as conn:
            conn.exec_driver_sql(f'SET ROLE "{roles["app"]}"')
            conn.exec_driver_sql(f'SET search_path TO "{schema}"')
            conn.exec_driver_sql("SELECT set_config('app.tenant_id','tenant-a',true)")
            with pytest.raises(DBAPIError, match="invalid onboarding request audit action"):
                conn.exec_driver_sql(
                    f'SELECT "{schema}".record_onboarding_request_audit('
                    "'tenant-a','dddddddd-dddd-4ddd-8ddd-dddddddddddd',1,'verified-sub',%s,'accept-hash','accept-key')",
                    (action,),
                )

    with admin.begin() as conn:
        conn.exec_driver_sql(f'SET ROLE "{roles["app"]}"')
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('app.tenant_id','tenant-a',true)")
        assert (
            conn.exec_driver_sql("SELECT count(*) FROM onboarding_request_audits").scalar_one() == 1
        )
