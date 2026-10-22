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
PHASE0R_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "migrations" / "versions" / "024_onboarding_phase0r.py"
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
        self.constraints: list[tuple[str, str, tuple[str, ...]]] = []

    def get_bind(self) -> object:
        return object()

    def create_table(self, table_name: str, *elements: object) -> None:
        self.created_tables[table_name] = elements

    def create_index(self, index_name: str, table_name: str, columns: list[str]) -> None:
        self.created_indexes.append((index_name, table_name, tuple(columns)))

    def create_unique_constraint(self, name: str, table_name: str, columns: list[str]) -> None:
        self.constraints.append((name, table_name, tuple(columns)))

    def create_foreign_key(
        self,
        name: str,
        source_table: str,
        target_table: str,
        source_columns: list[str],
        target_columns: list[str],
        **_kwargs: Any,
    ) -> None:
        self.constraints.append((name, source_table, tuple(source_columns + target_columns)))

    def add_column(self, table_name: str, column: sa.Column[Any]) -> None:
        self.created_tables.setdefault(table_name, tuple())
        self.created_tables[table_name] += (column,)

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


def test_migration_adds_only_three_v01_control_store_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _migration_module()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(migration, "_table_exists", lambda _table: False)

    _run_upgrade(migration, operations, monkeypatch)

    assert set(operations.created_tables) == EXPECTED_NEW_TABLES
    assert len(operations.created_tables) == 3
    assert {table for _, table, _ in operations.created_indexes} <= EXPECTED_NEW_TABLES


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
        assert (
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO CURRENT_USER" in statements
        )


def test_offline_migration_without_start_revision_emits_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch.setattr(migration, "context", _OfflineMigrationContext("022_conclusion_next_check"))
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
        assert (
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO CURRENT_USER" in statements
        )


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
    registry_constraints = {item.name for item in operations.created_tables["tenant_registry"]}
    assert "ck_tenant_registry_canonical_id_normalized" in registry_constraints
    assert "ck_tenant_registry_display_slug_normalized" in registry_constraints

    request_constraints = {item.name for item in operations.created_tables["onboarding_requests"]}
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

    assert columns["tenant_registry"] <= set(TenantRegistryORM.__table__.c.keys())
    assert set(OnboardingRequestORM.__table__.c.keys()) == columns["onboarding_requests"]
    assert columns["onboarding_dispatches"] <= set(OnboardingDispatchORM.__table__.c.keys())
    assert any(
        constraint.name == "fk_onboarding_dispatch_request_revision"
        for constraint in OnboardingDispatchORM.__table__.constraints
    )
    assert any(
        foreign_key.target_fullname in {"background_jobs.id", "background_jobs.tenant_id"}
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
        assert (
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO CURRENT_USER" in statements
        )
    assert (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE tenant_registry TO CURRENT_USER"
        in statements
    )


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


def test_request_job_reference_is_nullable_and_tenant_composite_fk() -> None:
    from investigation_agent_platform.infrastructure.persistence.models import OnboardingRequestORM

    column = OnboardingRequestORM.__table__.c.job_id
    assert column.nullable is True
    request_job_fk = next(
        constraint
        for constraint in OnboardingRequestORM.__table__.constraints
        if isinstance(constraint, sa.ForeignKeyConstraint)
        and constraint.name == "fk_onboarding_request_tenant_job"
    )
    assert tuple(request_job_fk.column_keys) == ("tenant_id", "job_id")


def test_migration_chain_has_single_v01_head() -> None:
    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["026_onboarding_request_audit"]
    revision = script.get_revision("026_onboarding_request_audit")
    assert revision is not None
    assert revision.down_revision == "025_onboarding_coverage_filing"


def test_phase026_request_audit_contract_is_additive_and_app_callable() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "026_onboarding_request_audit.py"
    )
    source = path.read_text(encoding="utf-8")
    assert 'revision = "026_onboarding_request_audit"' in source
    assert 'down_revision = "025_onboarding_coverage_filing"' in source
    assert '"onboarding_request_audits"' in source
    assert "fk_onboarding_request_audit_revision" in source
    assert "onboarding_request_audit_tenant_read" in source
    assert "onboarding_request_audit_migrator_insert" in source
    assert "p_actor_sub text" in source
    assert "session_user::text" in source
    assert "p_action <> 'CREATE'" in source
    assert "GRANT EXECUTE ON FUNCTION record_onboarding_request_audit" in source
    assert "TO iap_app" in source
    assert "GRANT INSERT ON onboarding_request_audits TO iap_app" not in source


def test_phase0r_schema_contract_declares_grants_fencing_and_worker_policy() -> None:
    from investigation_agent_platform.infrastructure.persistence.models import (
        BackgroundJobORM,
        OnboardingDispatchAuditORM,
        OnboardingDispatchORM,
        OnboardingGrantAuditORM,
        OnboardingGrantORM,
    )

    grant_columns = set(OnboardingGrantORM.__table__.c.keys())
    assert {
        "principal_id",
        "capability",
        "tenant_id",
        "application_id",
        "environment",
        "operation",
        "valid_from",
        "expires_at",
        "revoked_at",
        "policy_version",
        "issuer_id",
    } <= grant_columns
    assert OnboardingDispatchORM.__table__.c.lease_fencing_token is not None
    assert OnboardingDispatchORM.__table__.c.execution_generation is not None
    assert any(
        constraint.name == "uq_background_jobs_tenant_id_id"
        for constraint in BackgroundJobORM.__table__.constraints
    )
    assert any(
        constraint.name == "fk_onboarding_dispatch_tenant_job"
        for constraint in OnboardingDispatchORM.__table__.constraints
    )
    assert OnboardingDispatchAuditORM.__tablename__ == "onboarding_dispatch_audits"
    assert OnboardingGrantAuditORM.__tablename__ == "onboarding_grant_audits"

    migration_text = PHASE0R_MIGRATION_PATH.read_text(encoding="utf-8")
    assert "023_onboarding_control_store" in migration_text
    assert "TO iap_worker USING (true)" in migration_text
    assert (
        "lease_owner IS NULL OR lease_owner = current_setting('iap.worker_id', true)"
        in migration_text
    )
    assert "ON onboarding_dispatches TO iap_worker" in migration_text
    assert "GRANT UPDATE (state, lease_owner, lease_fencing_token" in migration_text
    assert "ON onboarding_requests TO iap_worker" in migration_text
    assert "BYPASSRLS" not in migration_text
    assert "database_role" in migration_text
    assert "current_setting('iap.worker_id', true)" in migration_text
    assert "aclexplode" in migration_text
    assert "onboarding_grant_audits" in migration_text
    assert "state IN ('PENDING', 'RETRYABLE')" in (
        Path(__file__).resolve().parents[2]
        / "src"
        / "investigation_agent_platform"
        / "infrastructure"
        / "persistence"
        / "onboarding_dispatch_repository.py"
    ).read_text(encoding="utf-8")
    assert "claim_and_reconcile" in (
        Path(__file__).resolve().parents[2]
        / "src"
        / "investigation_agent_platform"
        / "infrastructure"
        / "persistence"
        / "onboarding_dispatch_repository.py"
    ).read_text(encoding="utf-8")


def test_phase0r_schema_contract_declares_support_registry_and_coverage_reports() -> None:
    from investigation_agent_platform.infrastructure.persistence.models import (
        CoverageReportORM,
        SupportRegistryORM,
    )

    assert SupportRegistryORM.__tablename__ == "onboarding_support_registry"
    assert CoverageReportORM.__tablename__ == "onboarding_coverage_reports"

    registry_columns = set(SupportRegistryORM.__table__.c.keys())
    assert {
        "capability_key",
        "version",
        "strategy",
        "trust_state",
        "owner",
    } <= registry_columns
    assert any(
        constraint.name == "uq_support_registry_capability_version"
        for constraint in SupportRegistryORM.__table__.constraints
    )
    assert any(
        constraint.name == "ck_support_registry_strategy"
        for constraint in SupportRegistryORM.__table__.constraints
    )

    report_columns = set(CoverageReportORM.__table__.c.keys())
    assert {
        "tenant_id",
        "request_id",
        "revision",
        "generation",
        "report_version",
        "registry_version",
        "catalog_version",
        "estate_sha",
        "input_manifest_hash",
        "report_content_hash",
        "analyzer_version",
        "report_json",
    } <= report_columns
    assert any(
        constraint.name == "uq_coverage_report_request_generation"
        for constraint in CoverageReportORM.__table__.constraints
    )
    assert any(
        constraint.name == "fk_coverage_report_request_revision"
        for constraint in CoverageReportORM.__table__.constraints
    )

    migration_text = PHASE0R_MIGRATION_PATH.read_text(encoding="utf-8")
    assert "onboarding_support_registry" in migration_text
    assert "onboarding_coverage_reports" in migration_text
    assert "ck_support_registry_strategy" in migration_text
    assert "uq_coverage_report_request_generation" in migration_text
    assert "fk_coverage_report_request_revision" in migration_text
    assert "onboarding_coverage_report_tenant" in migration_text
    assert "onboarding_support_registry_read" in migration_text


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
    pytest.fail(
        "production application role provisioning contract is not available in this repository"
    )
