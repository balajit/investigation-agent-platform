"""Opt-in actual-role PostgreSQL tests for coverage report filing."""

from __future__ import annotations

import os
import secrets
import socket
import uuid
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from investigation_agent_platform.infrastructure.persistence.onboarding_coverage_repository import (
    SqlAlchemyOnboardingCoverageRepository,
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
        with socket.create_connection(
            (parsed.hostname or "localhost", parsed.port or 5432), timeout=1
        ):
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


def _report_body(request_id: uuid.UUID, *, report_id: uuid.UUID | None = None) -> dict[str, Any]:
    outputs: list[Mapping[str, Any]] = [
        {
            "content_hash": "a" * 64,
            "tool": "census",
            "tool_version": "1.0",
            "schema_version": "sniffer/v1",
        }
    ]
    from investigation_agent_platform.application.onboarding_agent.contracts.linkage import (
        input_manifest_hash,
        report_content_hash,
    )

    body: dict[str, Any] = {
        "report_id": str(report_id or uuid.uuid4()),
        "request_id": str(request_id),
        "revision": 1,
        "generation": 1,
        "report_version": "coverage/v1",
        "registry_version": "registry-1",
        "catalog_version": "catalog-1",
        "estate_sha": "estate-1",
        "input_manifest_hash": input_manifest_hash(
            sniffer_outputs=outputs,
            estate_sha="estate-1",
            registry_version="registry-1",
            catalog_version="catalog-1",
            generation=1,
        ),
        "analyzer_version": "analyzer-1",
        "verdicts": [{"service": "python", "tier": "generic"}],
        "bridges": [],
        "gaps": [],
        "remediation": [],
        "limitations": ["integration fixture"],
    }
    body["report_content_hash"] = report_content_hash(body)
    return body


@pytest.fixture
def filing_roles():  # type: ignore[no-untyped-def]
    if not _reachable():
        pytest.skip("IAP onboarding PostgreSQL test server is unreachable")
    admin = create_engine(_url())
    prefix = f"iap_filing_{uuid.uuid4().hex[:10]}"
    roles = {name: f"{prefix}_{name}" for name in ("worker", "app", "migrator")}
    passwords = {name: secrets.token_urlsafe(24) for name in roles}
    schema = f"{prefix}_schema"
    engines: dict[str, Any] = {}
    try:
        with admin.begin() as conn:
            for name, role in roles.items():
                conn.exec_driver_sql(
                    f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{passwords[name]}' "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                )
                conn.exec_driver_sql(f'GRANT "{role}" TO CURRENT_USER')
            conn.exec_driver_sql(f'CREATE SCHEMA "{schema}" AUTHORIZATION CURRENT_USER')
            conn.exec_driver_sql(
                f'GRANT USAGE ON SCHEMA "{schema}" TO "{roles["worker"]}", "{roles["app"]}", "{roles["migrator"]}"'
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
    worker = roles["worker"]
    app = roles["app"]
    migrator = roles["migrator"]
    with admin.begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql(
            "CREATE TABLE onboarding_requests (tenant_id text NOT NULL, request_id uuid NOT NULL, "
            "revision integer NOT NULL, PRIMARY KEY (tenant_id,request_id,revision))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE onboarding_coverage_reports (id uuid PRIMARY KEY, tenant_id text NOT NULL, "
            "request_id uuid NOT NULL, revision integer NOT NULL, generation integer NOT NULL, "
            "report_version varchar(32) NOT NULL, registry_version varchar(32) NOT NULL, "
            "catalog_version varchar(32) NOT NULL, estate_sha varchar(64) NOT NULL, "
            "input_manifest_hash varchar(64) NOT NULL, report_content_hash varchar(64) NOT NULL, "
            "analyzer_version varchar(64) NOT NULL, model_version varchar(128), report_json jsonb NOT NULL, "
            "created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), "
            "CONSTRAINT uq_coverage_report_request_generation UNIQUE (tenant_id,request_id,revision,generation), "
            "FOREIGN KEY (tenant_id,request_id,revision) REFERENCES onboarding_requests "
            "(tenant_id,request_id,revision))"
        )
        conn.exec_driver_sql(
            "CREATE TABLE onboarding_coverage_filing_audits (id uuid PRIMARY KEY, tenant_id text NOT NULL, "
            "request_id uuid NOT NULL, revision integer NOT NULL, generation integer NOT NULL, "
            "worker_id text NOT NULL, database_role text NOT NULL, fencing_token integer NOT NULL, "
            "report_content_hash text NOT NULL, action text NOT NULL CHECK(action='FILED'), "
            "FOREIGN KEY (tenant_id,request_id,revision,generation) REFERENCES onboarding_coverage_reports "
            "(tenant_id,request_id,revision,generation))"
        )
        for table in (
            "onboarding_requests",
            "onboarding_coverage_reports",
            "onboarding_coverage_filing_audits",
        ):
            conn.exec_driver_sql(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            conn.exec_driver_sql(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        conn.exec_driver_sql(
            "CREATE POLICY request_tenant ON onboarding_requests USING "
            "(tenant_id=current_setting('app.tenant_id',true)) WITH CHECK "
            "(tenant_id=current_setting('app.tenant_id',true))"
        )
        conn.exec_driver_sql(
            "CREATE POLICY coverage_tenant ON onboarding_coverage_reports USING "
            "(tenant_id=current_setting('app.tenant_id',true)) WITH CHECK "
            "(tenant_id=current_setting('app.tenant_id',true))"
        )
        conn.exec_driver_sql(
            f'CREATE POLICY coverage_worker_insert ON onboarding_coverage_reports FOR INSERT TO "{worker}" '
            "WITH CHECK (tenant_id=current_setting('app.tenant_id',true))"
        )
        conn.exec_driver_sql(
            f'CREATE POLICY filing_audit_tenant ON onboarding_coverage_filing_audits FOR SELECT TO "{app}" '
            "USING (tenant_id=current_setting('app.tenant_id',true))"
        )
        conn.exec_driver_sql(
            f'CREATE POLICY filing_audit_migrator_insert ON onboarding_coverage_filing_audits FOR INSERT TO "{migrator}" WITH CHECK (true)'
        )
        conn.exec_driver_sql(f'GRANT SELECT ON onboarding_requests TO "{worker}", "{app}"')
        conn.exec_driver_sql(f'GRANT SELECT, INSERT ON onboarding_coverage_reports TO "{worker}"')
        conn.exec_driver_sql(f'GRANT SELECT ON onboarding_coverage_reports TO "{app}"')
        conn.exec_driver_sql(f'GRANT SELECT ON onboarding_coverage_filing_audits TO "{app}"')
        conn.exec_driver_sql(
            f'GRANT INSERT, SELECT ON onboarding_coverage_filing_audits TO "{migrator}"'
        )
        conn.exec_driver_sql(
            f'CREATE FUNCTION "{schema}".record_onboarding_coverage_filing(p_tenant_id text,p_request_id uuid,'
            "p_revision integer,p_generation integer,p_fencing_token integer,p_report_content_hash text) "
            f'RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,"{schema}" AS $$ '
            "BEGIN IF p_tenant_id IS DISTINCT FROM current_setting('app.tenant_id',true) THEN "
            "RAISE EXCEPTION 'coverage filing audit tenant context mismatch'; END IF; "
            "IF current_setting('iap.worker_id',true) IS NULL THEN RAISE EXCEPTION 'worker required'; END IF; "
            f'INSERT INTO "{schema}".onboarding_coverage_filing_audits VALUES '
            "(gen_random_uuid(),p_tenant_id,p_request_id,p_revision,p_generation,"
            "current_setting('iap.worker_id',true),session_user::text,p_fencing_token,"
            "p_report_content_hash,'FILED'); END; $$"
        )
        conn.exec_driver_sql(
            f'ALTER FUNCTION "{schema}".record_onboarding_coverage_filing(text,uuid,integer,integer,integer,text) OWNER TO "{migrator}"'
        )
        conn.exec_driver_sql(
            f'REVOKE ALL ON FUNCTION "{schema}".record_onboarding_coverage_filing(text,uuid,integer,integer,integer,text) FROM PUBLIC'
        )
        conn.exec_driver_sql(
            f'GRANT EXECUTE ON FUNCTION "{schema}".record_onboarding_coverage_filing(text,uuid,integer,integer,integer,text) TO "{worker}"'
        )
        conn.exec_driver_sql(
            "INSERT INTO onboarding_requests VALUES "
            "('tenant-a','aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',1),"
            "('tenant-b','bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',1)"
        )


def test_worker_filing_is_tenant_scoped_and_audited(filing_roles) -> None:  # type: ignore[no-untyped-def]
    _admin, engines, _roles, schema = filing_roles
    worker = engines["worker"]
    report_id = uuid.uuid4()
    request_id = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    with worker.begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('app.tenant_id','tenant-a',true)")
        conn.exec_driver_sql("SELECT set_config('iap.worker_id','worker-a',true)")
        conn.exec_driver_sql(
            "INSERT INTO onboarding_coverage_reports "
            "(id,tenant_id,request_id,revision,generation,report_version,registry_version,"
            "catalog_version,estate_sha,input_manifest_hash,report_content_hash,analyzer_version,"
            "model_version,report_json) VALUES (%s,'tenant-a',%s,1,1,'v1','r1','c1','estate',"
            "'input','content','analyzer',NULL,'{}')",
            (str(report_id), str(request_id)),
        )
        conn.exec_driver_sql(
            f"SELECT \"{schema}\".record_onboarding_coverage_filing(%s,%s,1,1,12,'content')",
            ("tenant-a", str(request_id)),
        )
    with worker.begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('app.tenant_id','tenant-b',true)")
        assert (
            conn.exec_driver_sql("SELECT count(*) FROM onboarding_coverage_reports").scalar_one()
            == 0
        )
    with engines["app"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('app.tenant_id','tenant-a',true)")
        audit = conn.exec_driver_sql(
            "SELECT worker_id,database_role,fencing_token,action FROM onboarding_coverage_filing_audits"
        ).one()
        assert audit.worker_id == "worker-a"
        assert audit.database_role == filing_roles[2]["worker"]
        assert audit.fencing_token == 12
        assert audit.action == "FILED"


def test_worker_cannot_write_report_for_other_tenant(filing_roles) -> None:  # type: ignore[no-untyped-def]
    _admin, engines, _roles, schema = filing_roles
    connection = engines["worker"].connect()
    transaction = connection.begin()
    try:
        connection.exec_driver_sql(f'SET search_path TO "{schema}"')
        connection.exec_driver_sql("SELECT set_config('app.tenant_id','tenant-b',true)")
        connection.exec_driver_sql("SELECT set_config('iap.worker_id','worker-b',true)")
        with pytest.raises(DBAPIError):
            connection.exec_driver_sql(
                "INSERT INTO onboarding_coverage_reports VALUES "
                "(gen_random_uuid(),'tenant-a','aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',1,2,'input','content','{}')"
            )
    finally:
        transaction.rollback()
        connection.close()


@pytest.mark.asyncio
async def test_repository_idempotent_refile_records_one_audit(filing_roles) -> None:  # type: ignore[no-untyped-def]
    _admin, engines, roles, schema = filing_roles
    worker_url = engines["worker"].url.set(drivername="postgresql+asyncpg")
    engine = create_async_engine(worker_url)
    event.listen(
        engine.sync_engine,
        "connect",
        lambda dbapi_connection, _record: dbapi_connection.run_async(
            lambda raw_connection: raw_connection.execute(f'SET search_path TO "{schema}"')
        ),
    )
    try:
        request_id = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        repo = SqlAlchemyOnboardingCoverageRepository(
            async_sessionmaker(engine, expire_on_commit=False)
        )
        outputs: list[Mapping[str, Any]] = [
            {
                "content_hash": "a" * 64,
                "tool": "census",
                "tool_version": "1.0",
                "schema_version": "sniffer/v1",
            }
        ]
        first = await repo.file_report(
            tenant_id="tenant-a",
            request_id=request_id,
            revision=1,
            generation=1,
            report_body=_report_body(request_id),
            sniffer_outputs=outputs,
            estate_sha="estate-1",
            registry_version="registry-1",
            catalog_version="catalog-1",
            worker_id="worker-a",
            fencing_token=19,
        )
        second = await repo.file_report(
            tenant_id="tenant-a",
            request_id=request_id,
            revision=1,
            generation=1,
            report_body=_report_body(request_id, report_id=first.id),
            sniffer_outputs=outputs,
            estate_sha="estate-1",
            registry_version="registry-1",
            catalog_version="catalog-1",
            worker_id="worker-a",
            fencing_token=20,
        )
        assert first.id == second.id
    finally:
        await engine.dispose()

    with engines["app"].begin() as conn:
        conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        conn.exec_driver_sql("SELECT set_config('app.tenant_id','tenant-a',true)")
        audits = conn.exec_driver_sql(
            "SELECT worker_id,database_role,fencing_token,action FROM onboarding_coverage_filing_audits"
        ).all()
        assert len(audits) == 1
        assert audits[0].worker_id == "worker-a"
        assert audits[0].database_role == roles["worker"]
        assert audits[0].fencing_token == 19
