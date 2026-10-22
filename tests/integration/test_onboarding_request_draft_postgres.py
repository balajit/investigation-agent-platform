"""Opt-in actual-role PostgreSQL tests for Phase 1B DRAFT persistence."""

from __future__ import annotations

import os
import secrets
import socket
import uuid
from typing import Any
from urllib.parse import urlparse

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from investigation_agent_platform.application.onboarding.intake import (
    RepositoryReference,
    TenantIntakeEnvelope,
    TenantIntakeV1,
)
from investigation_agent_platform.infrastructure.persistence.onboarding_request_repository import (
    DraftIdempotencyConflictError,
    SqlAlchemyOnboardingRequestRepository,
)

RUN_DB_TESTS = os.environ.get("IAP_RUN_ONBOARDING_DB_TESTS", "0") == "1"
PG_URL = os.environ.get("IAP_TEST_PG_URL", "postgresql://iap:iap@localhost:5432/iap_test")
pytestmark = pytest.mark.skipif(
    not RUN_DB_TESTS,
    reason="Opt-in actual-role onboarding DB tests (IAP_RUN_ONBOARDING_DB_TESTS=1)",
)


def _reachable() -> bool:
    parsed = urlparse(PG_URL)
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 5432), timeout=1):
            return True
    except OSError:
        return False


def _url(username: str | None = None, password: str | None = None):  # type: ignore[no-untyped-def]
    url = make_url(PG_URL)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    if username is not None:
        url = url.set(username=username, password=password)
    return url


@pytest.fixture
def draft_roles():  # type: ignore[no-untyped-def]
    if not _reachable():
        pytest.skip("IAP onboarding PostgreSQL test server is unreachable")
    admin = create_engine(_url())
    prefix = f"iap_draft_{uuid.uuid4().hex[:10]}"
    roles = {
        name: f"{prefix}_{name}" for name in ("app", "worker", "registry_admin", "migrator")
    }
    passwords = {name: secrets.token_urlsafe(24) for name in roles}
    schema = f"{prefix}_schema"
    engines: dict[str, Any] = {}
    try:
        with admin.begin() as conn:
            for name, role in roles.items():
                conn.exec_driver_sql(
                    f'CREATE ROLE "{role}" LOGIN PASSWORD \'{passwords[name]}\' '
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                )
                conn.exec_driver_sql(f'GRANT "{role}" TO CURRENT_USER')
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}" AUTHORIZATION CURRENT_USER')
            conn.exec_driver_sql(
                f'GRANT USAGE ON SCHEMA "{schema}" TO "{roles["app"]}", "{roles["worker"]}", '
                f'"{roles["registry_admin"]}", "{roles["migrator"]}"'
            )
            conn.exec_driver_sql(f'GRANT CREATE ON SCHEMA "{schema}" TO "{roles["migrator"]}"')
        for name, role in roles.items():
            engines[name] = create_engine(_url(role, passwords[name]))
        _create_schema(admin, schema, roles)
        yield admin, engines, roles, schema
    finally:
        with admin.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            for role in roles.values():
                conn.exec_driver_sql(f'REVOKE "{role}" FROM CURRENT_USER')
                conn.exec_driver_sql(f'DROP ROLE IF EXISTS "{role}"')
        for engine in engines.values():
            engine.dispose()
        admin.dispose()


def _create_schema(admin: Any, schema: str, roles: dict[str, str]) -> None:
    app = roles["app"]
    migrator = roles["migrator"]
    with admin.begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("CREATE TABLE tenant_registry (canonical_id text PRIMARY KEY)")
        conn.exec_driver_sql(
            "CREATE TABLE onboarding_requests (id uuid PRIMARY KEY, tenant_id text NOT NULL, "
            "request_id uuid NOT NULL, revision integer NOT NULL, idempotency_key varchar(256) NOT NULL, "
            "content_hash varchar(64) NOT NULL, schema_version varchar(32) NOT NULL, "
            "policy_version varchar(32) NOT NULL, catalog_version varchar(32) NOT NULL, "
            "application_id varchar(128) NOT NULL, environment varchar(64) NOT NULL, "
            "intake_json jsonb NOT NULL, actor_id varchar(256) NOT NULL, job_id uuid, "
            "state varchar(32) NOT NULL DEFAULT 'DRAFT', state_version integer NOT NULL DEFAULT 1, "
            "created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), "
            "CONSTRAINT uq_onboarding_request_revision UNIQUE (tenant_id,request_id,revision), "
            "CONSTRAINT uq_onboarding_request_idempotency UNIQUE (tenant_id,idempotency_key), "
            "CONSTRAINT uq_onboarding_request_content UNIQUE (tenant_id,request_id,content_hash), "
            "FOREIGN KEY (tenant_id) REFERENCES tenant_registry(canonical_id))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE onboarding_request_audits (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), "
            "tenant_id text NOT NULL, request_id uuid NOT NULL, revision integer NOT NULL, "
            "actor_sub varchar(256) NOT NULL, database_role varchar(128) NOT NULL, action varchar(16) NOT NULL, "
            "content_hash varchar(64) NOT NULL, idempotency_key varchar(256) NOT NULL, "
            "created_at timestamptz NOT NULL DEFAULT now(), "
            "CONSTRAINT fk_onboarding_request_audit_revision FOREIGN KEY (tenant_id,request_id,revision) "
            "REFERENCES onboarding_requests(tenant_id,request_id,revision) ON DELETE RESTRICT, "
            "CONSTRAINT ck_request_audit_action CHECK (action IN ('CREATE')))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE background_jobs (id uuid PRIMARY KEY, tenant_id text NOT NULL)"
        )
        conn.exec_driver_sql(
            "CREATE TABLE onboarding_dispatches (id uuid PRIMARY KEY, tenant_id text NOT NULL)"
        )
        for table, key_column in (
            ("tenant_registry", "canonical_id"),
            ("onboarding_requests", "tenant_id"),
            ("onboarding_request_audits", "tenant_id"),
        ):
            conn.exec_driver_sql(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            conn.exec_driver_sql(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            conn.exec_driver_sql(
                f"CREATE POLICY {table}_tenant ON {table} USING "
                f"({key_column}=current_setting('app.tenant_id',true)) WITH CHECK "
                f"({key_column}=current_setting('app.tenant_id',true))"
            )
        conn.exec_driver_sql(
            f'CREATE POLICY request_audit_migrator_insert ON onboarding_request_audits '
            f'FOR INSERT TO "{migrator}" WITH CHECK (true)'
        )
        conn.exec_driver_sql(f'GRANT SELECT, INSERT ON tenant_registry, onboarding_requests TO "{app}"')
        conn.exec_driver_sql(f'GRANT SELECT ON onboarding_request_audits TO "{app}"')
        conn.exec_driver_sql(f'GRANT SELECT ON onboarding_requests TO "{roles["worker"]}"')
        conn.exec_driver_sql(f'GRANT SELECT ON tenant_registry TO "{roles["registry_admin"]}"')
        conn.exec_driver_sql(f'GRANT SELECT, INSERT ON onboarding_request_audits TO "{migrator}"')
        conn.exec_driver_sql("GRANT SELECT ON background_jobs, onboarding_dispatches TO CURRENT_USER")
        conn.exec_driver_sql(
            f'CREATE FUNCTION "{schema}".record_onboarding_request_audit('
            "p_tenant_id text,p_request_id uuid,p_revision integer,p_actor_sub text,"
            "p_action text,p_content_hash text,p_idempotency_key text) RETURNS void "
            f'LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,"{schema}" AS $$ '
            "BEGIN IF p_tenant_id IS DISTINCT FROM current_setting('app.tenant_id',true) THEN "
            "RAISE EXCEPTION 'onboarding request audit tenant context mismatch'; END IF; "
            "IF p_action <> 'CREATE' THEN RAISE EXCEPTION 'invalid onboarding request audit action'; END IF; "
            "INSERT INTO onboarding_request_audits "
            "(tenant_id,request_id,revision,actor_sub,database_role,action,content_hash,idempotency_key) "
            "VALUES(p_tenant_id,p_request_id,p_revision,p_actor_sub,session_user::text,p_action,"
            "p_content_hash,p_idempotency_key); END; $$"
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
        conn.exec_driver_sql("INSERT INTO tenant_registry VALUES ('tenant-a'),('tenant-b')")


def _draft() -> TenantIntakeEnvelope:
    return TenantIntakeEnvelope(
        intake=TenantIntakeV1(
            application_id="example-application",
            environment="test",
            repositories=(RepositoryReference(provider_key="sample-git-host", repository_id="example-team/example-service"),),
        )
    )


@pytest.mark.asyncio
async def test_actual_app_role_draft_replay_conflict_and_side_effect_bounds(draft_roles) -> None:  # type: ignore[no-untyped-def]
    admin, engines, roles, schema = draft_roles
    app_engine = create_async_engine(engines["app"].url.set(drivername="postgresql+asyncpg"))
    event.listen(
        app_engine.sync_engine,
        "connect",
        lambda dbapi_connection, _record: dbapi_connection.run_async(
            lambda raw_connection: raw_connection.execute(f'SET search_path TO "{schema}"')
        ),
    )
    try:
        repository = SqlAlchemyOnboardingRequestRepository(
            async_sessionmaker(app_engine, expire_on_commit=False)
        )
        first = await repository.create_draft(
            tenant_id="tenant-a",
            actor_sub="verified-sub-a",
            idempotency_key="db-draft-key",
            intake=_draft(),
        )
        replay = await repository.create_draft(
            tenant_id="tenant-a",
            actor_sub="verified-sub-a",
            idempotency_key="db-draft-key",
            intake=_draft(),
        )
        assert first.id == replay.id
        assert first.state == "DRAFT" and first.job_id is None
        with pytest.raises(DraftIdempotencyConflictError):
            await repository.create_draft(
                tenant_id="tenant-a",
                actor_sub="verified-sub-a",
                idempotency_key="db-draft-key",
                intake=TenantIntakeEnvelope(
                    intake=TenantIntakeV1(
                        application_id="example-application", environment="production"
                    )
                ),
            )
    finally:
        await app_engine.dispose()

    with engines["app"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('app.tenant_id','tenant-a',true)")
        assert conn.exec_driver_sql("SELECT count(*) FROM onboarding_requests").scalar_one() == 1
        assert conn.exec_driver_sql("SELECT state,actor_id,job_id FROM onboarding_requests").one() == (
            "DRAFT",
            "verified-sub-a",
            None,
        )
        audit = conn.exec_driver_sql(
            "SELECT actor_sub,database_role,action,content_hash,idempotency_key "
            "FROM onboarding_request_audits"
        ).one()
        assert audit.actor_sub == "verified-sub-a"
        assert audit.database_role == roles["app"]
        assert audit.action == "CREATE"
        assert audit.idempotency_key == "db-draft-key"

    with admin.begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        assert conn.exec_driver_sql("SELECT count(*) FROM background_jobs").scalar_one() == 0
        assert conn.exec_driver_sql("SELECT count(*) FROM onboarding_dispatches").scalar_one() == 0

    with engines["app"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('app.tenant_id','tenant-b',true)")
        assert conn.exec_driver_sql("SELECT count(*) FROM onboarding_requests").scalar_one() == 0
        assert conn.exec_driver_sql("SELECT count(*) FROM onboarding_request_audits").scalar_one() == 0

    with engines["app"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        with pytest.raises(DBAPIError):
            conn.exec_driver_sql("INSERT INTO onboarding_request_audits DEFAULT VALUES")
    with engines["worker"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        with pytest.raises(DBAPIError):
            conn.exec_driver_sql(
                f'SELECT "{schema}".record_onboarding_request_audit('
                "'tenant-a','aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',1,'sub','CREATE','hash','key')"
            )
    with engines["registry_admin"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        with pytest.raises(DBAPIError):
            conn.exec_driver_sql(
                f'SELECT "{schema}".record_onboarding_request_audit('
                "'tenant-a','aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',1,'sub','CREATE','hash','key')"
            )
