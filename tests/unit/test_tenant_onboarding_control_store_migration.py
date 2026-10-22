"""v0.1 migration contract and deferred dispatch failure-injection scaffolds."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "migrations"
    / "versions"
    / "023_onboarding_control_store.py"
)
EXPECTED_NEW_TABLES = {
    "tenant_registry",
    "onboarding_requests",
    "onboarding_dispatches",
}


def _migration_module() -> Any:
    spec = importlib.util.spec_from_file_location("migration_023_onboarding", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingOperations:
    def __init__(self) -> None:
        self.created_tables: dict[str, tuple[object, ...]] = {}
        self.created_indexes: list[tuple[str, str, tuple[str, ...]]] = []
        self.statements: list[str] = []

    def get_bind(self) -> object:
        return object()

    def create_table(self, table_name: str, *elements: object) -> None:
        self.created_tables[table_name] = elements

    def create_index(
        self, index_name: str, table_name: str, columns: list[str]
    ) -> None:
        self.created_indexes.append((index_name, table_name, tuple(columns)))

    def execute(self, statement: object) -> None:
        self.statements.append(str(statement))


class _OfflineMigrationContext:
    def __init__(self, starting_revision: str | None) -> None:
        self.starting_revision = starting_revision

    def is_offline_mode(self) -> bool:
        return True

    def get_starting_revision_argument(self) -> str | None:
        return self.starting_revision


def _run_upgrade(
    migration: Any,
    operations: _RecordingOperations,
    monkeypatch: pytest.MonkeyPatch,
    *,
    offline_start: str | None = None,
) -> None:
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: offline_start is not None)
    if offline_start is not None:
        monkeypatch.setattr(
            migration.context, "get_starting_revision_argument", lambda: offline_start
        )
    monkeypatch.setattr(migration, "op", operations)
    migration.upgrade()


def test_migration_adds_only_three_v01_control_store_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _migration_module()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(migration, "_table_exists", lambda _table: False)

    _run_upgrade(migration, operations, monkeypatch)

    assert set(operations.created_tables) == EXPECTED_NEW_TABLES
    assert len(operations.created_tables) == 3
    assert {
        table
        for _, table, _ in operations.created_indexes
    } <= EXPECTED_NEW_TABLES


def test_migration_skips_baseline_tables_and_still_applies_security(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    operations = _RecordingOperations()
    existing = set(EXPECTED_NEW_TABLES)
    monkeypatch.setattr(migration, "_table_exists", lambda table: table in existing)

    _run_upgrade(migration, operations, monkeypatch)

    assert operations.created_tables == {}
    statements = "\n".join(operations.statements)
    for table in sorted(EXPECTED_NEW_TABLES):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in statements
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in statements
        assert f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO CURRENT_USER" in statements


def test_offline_migration_without_start_revision_emits_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _migration_module()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "context", _OfflineMigrationContext(None))
    monkeypatch.setattr(migration, "_table_exists", lambda _table: False)
    _run_upgrade(migration, operations, monkeypatch)

    assert set(operations.created_tables) == EXPECTED_NEW_TABLES


def test_offline_migration_from_existing_revision_skips_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    operations = _RecordingOperations()
    monkeypatch.setattr(
        migration, "context", _OfflineMigrationContext("022_conclusion_next_check")
    )
    monkeypatch.setattr(migration, "_table_exists", lambda _table: True)
    _run_upgrade(
        migration,
        operations,
        monkeypatch,
        offline_start="022_conclusion_next_check",
    )

    assert operations.created_tables == {}
    statements = "\n".join(operations.statements)
    for table in EXPECTED_NEW_TABLES:
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in statements
        assert f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO CURRENT_USER" in statements


def test_migration_enforces_registry_and_revision_constraints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(migration, "_table_exists", lambda _table: False)

    _run_upgrade(migration, operations, monkeypatch)

    columns = {
        table: {item.name for item in elements if isinstance(item, sa.Column)}
        for table, elements in operations.created_tables.items()
    }
    assert {"id", "canonical_id", "display_slug", "status"} <= columns["tenant_registry"]
    assert {
        "tenant_id",
        "request_id",
        "revision",
        "idempotency_key",
        "content_hash",
        "intake_json",
        "state_version",
        "job_id",
    } <= columns["onboarding_requests"]
    assert {
        "tenant_id",
        "request_id",
        "revision",
        "job_id",
        "workflow_id",
        "state",
        "attempts",
        "lease_owner",
        "lease_expires_at",
    } <= columns["onboarding_dispatches"]
    registry_constraints = {
        item.name for item in operations.created_tables["tenant_registry"]
    }
    assert "ck_tenant_registry_canonical_id_normalized" in registry_constraints
    assert "ck_tenant_registry_display_slug_normalized" in registry_constraints

    request_constraints = {
        item.name for item in operations.created_tables["onboarding_requests"]
    }
    dispatch_constraints = {
        item.name for item in operations.created_tables["onboarding_dispatches"]
    }
    assert "uq_onboarding_request_revision" in request_constraints
    assert "uq_onboarding_request_idempotency" in request_constraints
    assert "uq_onboarding_request_content" in request_constraints
    assert "fk_onboarding_dispatch_request_revision" in dispatch_constraints
    assert "uq_onboarding_dispatch_request_revision" in dispatch_constraints
    assert "uq_onboarding_dispatch_workflow" in dispatch_constraints

    dispatch_columns = operations.created_tables["onboarding_dispatches"]
    tenant_revision_fk = next(
        item
        for item in dispatch_columns
        if isinstance(item, sa.ForeignKeyConstraint)
        and item.name == "fk_onboarding_dispatch_request_revision"
    )
    assert tenant_revision_fk.name == "fk_onboarding_dispatch_request_revision"
    assert tuple(tenant_revision_fk.column_keys) == (
        "tenant_id",
        "request_id",
        "revision",
    )
    assert tuple(element.target_fullname for element in tenant_revision_fk.elements) == (
        "onboarding_requests.tenant_id",
        "onboarding_requests.request_id",
        "onboarding_requests.revision",
    )

    from investigation_agent_platform.infrastructure.persistence.models import (
        BackgroundJobORM,
        OnboardingDispatchORM,
        OnboardingRequestORM,
        TenantRegistryORM,
    )

    assert set(TenantRegistryORM.__table__.c.keys()) == columns["tenant_registry"]
    assert set(OnboardingRequestORM.__table__.c.keys()) == columns["onboarding_requests"]
    assert set(OnboardingDispatchORM.__table__.c.keys()) == columns["onboarding_dispatches"]
    assert any(
        constraint.name == "fk_onboarding_dispatch_request_revision"
        for constraint in OnboardingDispatchORM.__table__.constraints
    )
    assert any(
        foreign_key.target_fullname == "background_jobs.id"
        for foreign_key in OnboardingDispatchORM.__table__.c.job_id.foreign_keys
    )
    assert any(
        constraint.name == "uq_onboarding_request_revision"
        for constraint in OnboardingRequestORM.__table__.constraints
    )
    assert "background_jobs" in BackgroundJobORM.__table__.metadata.tables


def test_orm_server_defaults_match_control_store_migration() -> None:
    from investigation_agent_platform.infrastructure.persistence.models import (
        OnboardingDispatchORM,
        OnboardingRequestORM,
        TenantRegistryORM,
    )

    assert str(TenantRegistryORM.__table__.c.owner.server_default.arg) == ""
    assert str(TenantRegistryORM.__table__.c.status.server_default.arg) == "RESERVED"
    assert TenantRegistryORM.__table__.c.created_at.server_default is not None
    assert TenantRegistryORM.__table__.c.updated_at.server_default is not None
    assert str(OnboardingRequestORM.__table__.c.state.server_default.arg) == "DRAFT"
    assert str(OnboardingRequestORM.__table__.c.state_version.server_default.arg) == "1"
    assert OnboardingRequestORM.__table__.c.created_at.server_default is not None
    assert OnboardingRequestORM.__table__.c.updated_at.server_default is not None
    assert str(OnboardingDispatchORM.__table__.c.state.server_default.arg) == "PENDING"
    assert str(OnboardingDispatchORM.__table__.c.attempts.server_default.arg) == "0"
    assert OnboardingDispatchORM.__table__.c.created_at.server_default is not None
    assert OnboardingDispatchORM.__table__.c.updated_at.server_default is not None


def test_migration_enables_forced_rls_and_current_user_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(migration, "_table_exists", lambda _table: False)

    _run_upgrade(migration, operations, monkeypatch)

    statements = "\n".join(operations.statements)
    for table in sorted(EXPECTED_NEW_TABLES):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in statements
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in statements
    assert "tenant_registry_tenant_isolation" in statements
    assert "USING (canonical_id = current_setting('app.tenant_id', true))" in statements
    assert "onboarding_requests_tenant_isolation" in statements
    assert "USING (tenant_id = current_setting('app.tenant_id', true))" in statements
    assert "CREATE POLICY" in statements
    for table in ("onboarding_requests", "onboarding_dispatches"):
        assert f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO CURRENT_USER" in statements
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE tenant_registry TO CURRENT_USER" in statements


def test_baseline_metadata_contains_exact_v01_tables() -> None:
    from investigation_agent_platform.infrastructure.persistence.models import Base

    expected_columns = {
        "tenant_registry": {
            "canonical_id",
            "display_slug",
            "owner",
            "status",
            "created_at",
            "updated_at",
        },
        "onboarding_requests": {
            "tenant_id",
            "request_id",
            "revision",
            "idempotency_key",
            "content_hash",
            "schema_version",
            "policy_version",
            "catalog_version",
            "application_id",
            "environment",
            "intake_json",
            "actor_id",
            "job_id",
            "state",
            "state_version",
        },
        "onboarding_dispatches": {
            "tenant_id",
            "event_id",
            "request_id",
            "revision",
            "job_id",
            "workflow_id",
            "schema_version",
            "state",
            "attempts",
            "next_attempt_at",
            "lease_owner",
            "lease_expires_at",
            "temporal_run_id",
            "last_error_code",
        },
    }
    for table_name, columns in expected_columns.items():
        assert table_name in Base.metadata.tables
        assert columns <= set(Base.metadata.tables[table_name].columns.keys())
    assert not any(name.startswith("onboarding_steps") for name in Base.metadata.tables)
    assert not any("deletion_manifest" in name for name in Base.metadata.tables)
    assert "onboarding_dispatches" in Base.metadata.tables


def test_migration_role_grant_is_explicitly_current_user() -> None:
    migration_text = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "TO CURRENT_USER" in migration_text
    assert "migrations to run as the runtime application role" in migration_text
    assert "CREATE ROLE" not in migration_text


def test_downgrade_preserves_durable_onboarding_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = _migration_module()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    assert operations.created_tables == {}
    assert operations.statements == []


def test_request_job_reference_is_nullable_and_not_cross_tenant_fk() -> None:
    from investigation_agent_platform.infrastructure.persistence.models import OnboardingRequestORM

    column = OnboardingRequestORM.__table__.c.job_id
    assert column.nullable is True
    assert not column.foreign_keys


def test_migration_chain_has_single_v01_head() -> None:
    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["023_onboarding_control_store"]
    revision = script.get_revision("023_onboarding_control_store")
    assert revision is not None
    assert revision.down_revision == "022_conclusion_next_check"


@pytest.mark.parametrize(
    "crash_point",
    [
        "before_commit",
        "after_commit_before_dispatch",
        "after_temporal_start_before_ack",
        "lease_expiry",
        "temporal_outage",
    ],
)
@pytest.mark.xfail(
    strict=True,
    reason="Phase 2 dispatcher/reconciler implementation must satisfy these crash invariants",
)
def test_dispatch_crash_recovery_scaffold(crash_point: str) -> None:
    """Fail loudly until a dispatcher harness can exercise each crash boundary."""
    pytest.fail(f"dispatch crash injection is not implemented for {crash_point}")


@pytest.mark.xfail(
    strict=True,
    reason="v0.1 deployment must run migrations as the runtime app role or provision grants separately",
)
def test_production_migrator_role_is_distinct_from_current_runtime_role() -> None:
    """Release gate: prove deployment role grants when migrator and app roles differ."""
    pytest.fail("production application role provisioning contract is not available in this repository")
