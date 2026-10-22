"""Tenant registry and onboarding control-store foundation (v0.1 Phase 1).

Creates only tenant_registry, onboarding_requests, and onboarding_dispatches.
Each tenant-bearing table uses FORCE RLS. CRUD grants target CURRENT_USER,
which requires migrations to run as the runtime application role; if production
uses a separate migrator role, deployment must apply a separately reviewed
GRANT as part of role provisioning.

Registry RLS intentionally uses the established app.tenant_id scope without an
operator bypass. Platform registry operations must run through a reviewed,
explicit database/access boundary; do not set a magic cross-tenant value in the
shared tenant context.
"""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "023_onboarding_control_store"
down_revision = "022_conclusion_next_check"
branch_labels = None
depends_on = None

_TABLES = ("tenant_registry", "onboarding_requests", "onboarding_dispatches")
_GRANT_TABLES = ("tenant_registry", "onboarding_requests", "onboarding_dispatches")


def _table_exists(table_name: str) -> bool:
    """Handle current-metadata baseline installs as well as upgraded databases."""
    if context.is_offline_mode():
        # Full-history SQL includes the metadata-driven baseline, which has
        # already emitted these tables. A script starting at an existing
        # revision must create the additions because no catalog is available.
        return (
            context.get_starting_revision_argument() is None
            or context.get_starting_revision_argument() == "base"
        )
    return sa.inspect(op.get_bind()).has_table(table_name)


def _create_table_if_missing(table_name: str, *elements: object) -> bool:
    """Create one onboarding table unless current-metadata baseline made it."""
    if _table_exists(table_name):
        return False
    op.create_table(table_name, *elements)
    return True


def _enable_tenant_rls(table_name: str, tenant_column: str = "tenant_id") -> None:
    """Enable forced tenant isolation and explicit CRUD grants to migrator/app role."""
    op.execute(sa.text(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            f"""
            DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_policies
                        WHERE schemaname=current_schema()
                          AND tablename='{table_name}'
                          AND policyname='{table_name}_tenant_isolation'
                ) THEN
                    CREATE POLICY {table_name}_tenant_isolation ON {table_name}
                        USING ({tenant_column} = current_setting('app.tenant_id', true))
                        WITH CHECK ({tenant_column} = current_setting('app.tenant_id', true));
                END IF;
            END $$;
            """
        )
    )
    if table_name in _GRANT_TABLES:
        # CURRENT_USER is intentional v0.1 behavior, not role discovery.
        # Production must run migrations as the runtime application role.
        op.execute(
            sa.text(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table_name} TO CURRENT_USER"
            )
        )


def upgrade() -> None:
    tenant_created = _create_table_if_missing(
        "tenant_registry",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("canonical_id", sa.String(64), nullable=False, unique=True),
        sa.Column("display_slug", sa.String(128), nullable=False, unique=True),
        sa.Column("owner", sa.String(256), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="RESERVED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('RESERVED', 'ACTIVE', 'SUSPENDED', 'RETIRED')",
            name="ck_tenant_registry_status",
        ),
        sa.CheckConstraint(
            "canonical_id = lower(btrim(canonical_id)) "
            "AND canonical_id ~ '^[a-z0-9][a-z0-9-]{0,63}$'",
            name="ck_tenant_registry_canonical_id_normalized",
        ),
        sa.CheckConstraint(
            "display_slug = lower(btrim(display_slug)) "
            "AND display_slug ~ '^[a-z0-9][a-z0-9._-]{0,127}$'",
            name="ck_tenant_registry_display_slug_normalized",
        ),
    )
    if tenant_created:
        op.create_index("ix_tenant_registry_status", "tenant_registry", ["status"])
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION onboarding_reject_canonical_id_change() RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'tenant registry rows must be retired, not deleted';
                END IF;
                IF NEW.canonical_id IS DISTINCT FROM OLD.canonical_id THEN
                    RAISE EXCEPTION 'tenant canonical_id is immutable';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_trigger
                    WHERE tgname='tenant_registry_canonical_id_immutable'
                      AND tgrelid='tenant_registry'::regclass
                      AND NOT tgisinternal
                ) THEN
                    CREATE TRIGGER tenant_registry_canonical_id_immutable
                    BEFORE UPDATE OF canonical_id OR DELETE ON tenant_registry
                    FOR EACH ROW EXECUTE FUNCTION onboarding_reject_canonical_id_change();
                END IF;
            END $$;
            """
        )
    )

    requests_created = _create_table_if_missing(
        "onboarding_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenant_registry.canonical_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("policy_version", sa.String(32), nullable=False),
        sa.Column("catalog_version", sa.String(32), nullable=False),
        sa.Column("application_id", sa.String(128), nullable=False),
        sa.Column("environment", sa.String(64), nullable=False),
        sa.Column("intake_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("actor_id", sa.String(256), nullable=False),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="Tenant-scoped BackgroundJobORM reference; repository verifies tenant ownership.",
        ),
        sa.Column("state", sa.String(32), nullable=False, server_default="DRAFT"),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "tenant_id", "request_id", "revision", name="uq_onboarding_request_revision"
        ),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_onboarding_request_idempotency"
        ),
        sa.UniqueConstraint("tenant_id", "request_id", "content_hash", name="uq_onboarding_request_content"),
        sa.CheckConstraint("revision >= 1", name="ck_onboarding_request_revision_positive"),
        sa.CheckConstraint("state_version >= 1", name="ck_onboarding_request_state_version_positive"),
        sa.CheckConstraint(
            "state IN ('DRAFT', 'VALIDATING', 'AWAITING_APPROVAL', 'QUEUED', "
            "'PROVISIONING', 'READINESS_CHECK', 'ACTIVE', 'FAILED_RETRYABLE', "
            "'FAILED_TERMINAL', 'CANCEL_REQUESTED', 'CANCELED')",
            name="ck_onboarding_request_state",
        ),
    )
    if requests_created:
        op.create_index(
            "ix_onboarding_request_tenant_state",
            "onboarding_requests",
            ["tenant_id", "state", "updated_at"],
        )
        op.create_index(
            "ix_onboarding_request_scope",
            "onboarding_requests",
            ["tenant_id", "application_id", "environment", "revision"],
        )
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION onboarding_reject_request_revision_change() RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'onboarding request revisions are immutable';
                END IF;
                IF ROW(
                    NEW.tenant_id,
                    NEW.request_id,
                    NEW.revision,
                    NEW.idempotency_key,
                    NEW.content_hash,
                    NEW.schema_version,
                    NEW.policy_version,
                    NEW.catalog_version,
                    NEW.application_id,
                    NEW.environment,
                    NEW.intake_json,
                    NEW.actor_id
                ) IS DISTINCT FROM ROW(
                    OLD.tenant_id,
                    OLD.request_id,
                    OLD.revision,
                    OLD.idempotency_key,
                    OLD.content_hash,
                    OLD.schema_version,
                    OLD.policy_version,
                    OLD.catalog_version,
                    OLD.application_id,
                    OLD.environment,
                    OLD.intake_json,
                    OLD.actor_id
                ) THEN
                    RAISE EXCEPTION 'onboarding request revision content is immutable';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
    )
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_trigger
                    WHERE tgname='onboarding_request_revision_immutable'
                      AND tgrelid='onboarding_requests'::regclass
                      AND NOT tgisinternal
                ) THEN
                    CREATE TRIGGER onboarding_request_revision_immutable
                    BEFORE UPDATE OR DELETE ON onboarding_requests
                    FOR EACH ROW EXECUTE FUNCTION onboarding_reject_request_revision_change();
                END IF;
            END $$;
            """
        )
    )

    dispatch_created = _create_table_if_missing(
        "onboarding_dispatches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(64),
            sa.ForeignKey("tenant_registry.canonical_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("background_jobs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("workflow_id", sa.String(256), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("temporal_run_id", sa.String(256), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "request_id", "revision"],
            ["onboarding_requests.tenant_id", "onboarding_requests.request_id", "onboarding_requests.revision"],
            ondelete="RESTRICT",
            name="fk_onboarding_dispatch_request_revision",
        ),
        sa.UniqueConstraint(
            "tenant_id", "workflow_id", name="uq_onboarding_dispatch_workflow"
        ),
        sa.CheckConstraint("revision >= 1", name="ck_onboarding_dispatch_revision_positive"),
        sa.CheckConstraint("attempts >= 0", name="ck_onboarding_dispatch_attempts_nonnegative"),
        sa.CheckConstraint(
            "state IN ('PENDING', 'CLAIMED', 'ACCEPTED', 'RETRYABLE', 'TERMINAL')",
            name="ck_onboarding_dispatch_state",
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_onboarding_dispatch_lease_pair",
        ),
        sa.UniqueConstraint(
            "tenant_id", "request_id", "revision", name="uq_onboarding_dispatch_request_revision"
        ),
    )
    if dispatch_created:
        op.create_index(
            "ix_onboarding_dispatch_pending",
            "onboarding_dispatches",
            ["state", "next_attempt_at", "created_at"],
        )
        op.create_index(
            "ix_onboarding_dispatch_tenant_request",
            "onboarding_dispatches",
            ["tenant_id", "request_id", "revision"],
        )

    _enable_tenant_rls("tenant_registry", tenant_column="canonical_id")
    for table_name in ("onboarding_requests", "onboarding_dispatches"):
        _enable_tenant_rls(table_name)


def downgrade() -> None:
    # The schema is additive and may already have been created by the baseline.
    # Keep tables, data, RLS, grants, and triggers for safe forward recovery.
    # Rollback disables feature routes/workers; it never erases durable intent.
    return None
