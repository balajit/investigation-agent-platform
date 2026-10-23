"""Opt-in actual-role RLS conformance for Phase 3/5 onboarding tables.

Proves, as the real iap_app/iap_worker/iap_migrator roles on a migrated
disposable database: worker INSERT with tenant context is allowed, worker
writes without tenant context are denied, cross-tenant reads return zero
rows, app reads its own tenant, and operator registry writes stay with the
migrator. Uses SET ROLE from the superuser connection; no scratch roles.
"""

from __future__ import annotations

import os
import socket
import uuid
from urllib.parse import urlparse

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
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


def _url():  # type: ignore[no-untyped-def]
    url = make_url(PG_URL)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    return url


@pytest.fixture
def admin():  # type: ignore[no-untyped-def]
    if not _reachable():
        pytest.skip("IAP onboarding PostgreSQL test server is unreachable")
    engine = create_engine(_url())
    try:
        yield engine
    finally:
        engine.dispose()


def _exec(engine, role, tenant, sql, params=None):  # type: ignore[no-untyped-def]
    with engine.connect() as connection:
        connection.execute(text(f"SET ROLE {role}"))
        if tenant is not None:
            connection.execute(text(f"SET app.tenant_id = '{tenant}'"))
        result = connection.execute(text(sql), params or {})
        connection.execute(text("RESET ROLE"))
        connection.commit()
        try:
            return result.all()
        except Exception:
            return []


@pytest.fixture
def pin(admin):  # type: ignore[no-untyped-def]
    tenant = "tenant-a"
    request_id = str(uuid.uuid4())
    with admin.connect() as connection:
        connection.execute(
            text(
                "INSERT INTO tenant_registry (id, canonical_id, display_slug, owner, status) "
                "VALUES (gen_random_uuid(), :tenant, :tenant, 'tester', 'RESERVED') "
                "ON CONFLICT DO NOTHING"
            ),
            {"tenant": tenant},
        )
        connection.execute(
            text(
                "INSERT INTO onboarding_requests (id, tenant_id, request_id, revision, "
                "idempotency_key, content_hash, schema_version, policy_version, "
                "catalog_version, application_id, environment, intake_json, actor_id) "
                "VALUES (gen_random_uuid(), :tenant, :request_id, 1, :idem, :hash, "
                "'tenant-intake/v1', 'p1', 'v0.1', 'app-1', 'prod', '{}', 'tester') "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "tenant": tenant,
                "request_id": request_id,
                "idem": f"iso-{request_id}",
                "hash": "c" * 64,
            },
        )
        connection.commit()
    return tenant, request_id


def test_worker_probe_evidence_insert_requires_tenant(admin, pin) -> None:  # type: ignore[no-untyped-def]
    tenant, request_id = pin
    evidence_id = str(uuid.uuid4())
    _exec(
        admin,
        "iap_worker",
        tenant,
        "INSERT INTO onboarding_probe_evidences (id, tenant_id, request_id, revision, "
        "application_id, environment, policy_version, catalog_version, capability_key, "
        "adapter, adapter_version, outcome, diagnostic_code, detail, auth_mode, "
        "probed_at, expires_at) VALUES (:id, :tenant, :request_id, 1, 'app-1', 'prod', "
        "'p1', 'v0.1', 'repository', 'sample-git-host', 'v0.1', 'VERIFIED', 'OK', '', "
        "'scoped-secret', now(), now() + interval '1 hour')",
        {"id": evidence_id, "tenant": tenant, "request_id": request_id},
    )
    rows = _exec(
        admin,
        "iap_worker",
        tenant,
        "SELECT id FROM onboarding_probe_evidences WHERE id = :id",
        {"id": evidence_id},
    )
    assert len(rows) == 1
    with pytest.raises(DBAPIError):
        _exec(
            admin,
            "iap_worker",
            None,
            "INSERT INTO onboarding_probe_evidences (id, tenant_id, request_id, revision, "
            "application_id, environment, policy_version, catalog_version, capability_key, "
            "adapter, adapter_version, outcome, diagnostic_code, detail, auth_mode, "
            "probed_at, expires_at) VALUES (:id, :tenant, :request_id, 1, 'app-1', 'prod', "
            "'p1', 'v0.1', 'repository', 'sample-git-host', 'v0.1', 'VERIFIED', 'OK', '', "
            "'scoped-secret', now(), now() + interval '1 hour')",
            {"id": str(uuid.uuid4()), "tenant": tenant, "request_id": request_id},
        )


def test_cross_tenant_reads_return_zero_rows(admin, pin) -> None:  # type: ignore[no-untyped-def]
    rows = _exec(admin, "iap_app", "tenant-other", "SELECT id FROM onboarding_probe_evidences")
    assert rows == []
    rows = _exec(admin, "iap_app", "tenant-other", "SELECT id FROM onboarding_readiness_reports")
    assert rows == []


def test_app_reads_own_tenant_evidence(admin, pin) -> None:  # type: ignore[no-untyped-def]
    tenant, _ = pin
    rows = _exec(admin, "iap_app", tenant, "SELECT count(*) FROM onboarding_probe_evidences")
    assert rows[0][0] >= 1


def test_worker_outcome_insert_and_select(admin, pin) -> None:  # type: ignore[no-untyped-def]
    tenant, _ = pin
    manifest_id = str(uuid.uuid4())
    _exec(
        admin,
        "iap_migrator",
        None,
        "INSERT INTO onboarding_deletion_manifests (id, tenant_id, manifest_id, "
        "environment, plan_version, store_registry_version, policy_version, "
        "manifest_json, plan_hash, state, requester_id, expires_at) VALUES "
        "(gen_random_uuid(), :tenant, :manifest, 'prod', 1, 'reg-v1', 'p1', '{}', "
        "'d' || repeat('e', 63), 'IN_PROGRESS', 'req-1', now() + interval '1 day')",
        {"tenant": tenant, "manifest": manifest_id},
    )
    _exec(
        admin,
        "iap_worker",
        tenant,
        "INSERT INTO onboarding_deletion_outcomes (id, tenant_id, manifest_id, "
        "entry_key, store_id, action, state, attempt) VALUES (gen_random_uuid(), "
        ":tenant, :manifest, 's1:t:r1', 's1', 'DELETE', 'DONE', 1)",
        {"tenant": tenant, "manifest": manifest_id},
    )
    rows = _exec(
        admin,
        "iap_worker",
        tenant,
        "SELECT state FROM onboarding_deletion_outcomes WHERE manifest_id = :manifest",
        {"manifest": manifest_id},
    )
    assert [row[0] for row in rows] == ["DONE"]
    other = _exec(
        admin,
        "iap_worker",
        "tenant-other",
        "SELECT state FROM onboarding_deletion_outcomes WHERE manifest_id = :manifest",
        {"manifest": manifest_id},
    )
    assert other == []


def test_registry_writes_stay_with_migrator(admin) -> None:  # type: ignore[no-untyped-def]
    deployment = f"dep-{uuid.uuid4().hex[:8]}"
    _exec(
        admin,
        "iap_migrator",
        None,
        "INSERT INTO onboarding_store_registry (id, deployment_id, store_id, "
        "store_type, adapter_version, registry_version) VALUES (gen_random_uuid(), "
        ":dep, 's9', 'object', 'v0.1', 'reg-v1')",
        {"dep": deployment},
    )
    with pytest.raises(DBAPIError):
        _exec(
            admin,
            "iap_app",
            "tenant-a",
            "INSERT INTO onboarding_store_registry (id, deployment_id, store_id, "
            "store_type, adapter_version, registry_version) VALUES (gen_random_uuid(), "
            ":dep, 's9', 'object', 'v0.1', 'reg-v1')",
            {"dep": deployment},
        )
    rows = _exec(
        admin,
        "iap_app",
        "tenant-a",
        "SELECT store_id FROM onboarding_store_registry WHERE deployment_id = :dep",
        {"dep": deployment},
    )
    assert [row[0] for row in rows] == ["s9"]
