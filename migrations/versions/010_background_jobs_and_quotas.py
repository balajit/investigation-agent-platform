"""Alembic migration: Part 11.3 background jobs + quota counters.

Adds ``background_jobs`` (Temporal-backed job read model with OCC version;
strict tenant RLS, owning tenant only) and ``quota_counters`` (atomic
pre-dispatch quota ledger; strict tenant RLS). Both tables are new — no data
migration. Downgrade drops policies, indexes, and tables.
"""

# revision identifiers, used by Alembic.
revision = "010_background_jobs_and_quotas"
down_revision = "009_profile_revisions"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # type: ignore[import-not-found]
from alembic import op  # type: ignore[import-not-found]


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    try:
        return sa.inspect(bind).has_table(table)
    except Exception:
        # Offline (--sql) mode uses a MockConnection that cannot be
        # inspected; emit the DDL unconditionally so SQL scripts stay complete.
        return False


def upgrade() -> None:
    if not _table_exists("background_jobs"):
        op.create_table(
            "background_jobs",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("application_id", sa.String(128), nullable=True),
            sa.Column("kind", sa.String(64), nullable=False),
            sa.Column("contract_version", sa.String(32), nullable=False, server_default="1.0"),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_by", sa.String(256), nullable=False, server_default="system"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("parent_job_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("workflow_id", sa.String(256), nullable=True),
            sa.Column("run_id", sa.String(256), nullable=True),
            sa.Column("input_ref", sa.String(1024), nullable=True),
            sa.Column("result_ref", sa.String(1024), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("quota_class", sa.String(64), nullable=False, server_default="default"),
            sa.Column("retention_class", sa.String(64), nullable=False, server_default="default"),
            sa.Column("stages_json", sa.dialects.postgresql.JSONB, nullable=True),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        )
        op.create_index(
            "idx_bg_jobs_tenant_kind_status",
            "background_jobs",
            ["tenant_id", "kind", "status"],
        )
        op.create_index(
            "idx_bg_jobs_tenant_created",
            "background_jobs",
            ["tenant_id", "created_at"],
        )

    op.execute(sa.text("ALTER TABLE background_jobs ENABLE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE tablename='background_jobs' AND policyname='bg_jobs_tenant'
                ) THEN
                    CREATE POLICY bg_jobs_tenant ON background_jobs
                        USING (tenant_id = current_setting('app.tenant_id', true))
                        WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
                END IF;
            END $$;
            """
        )
    )
    op.execute(sa.text("ALTER TABLE background_jobs FORCE ROW LEVEL SECURITY"))

    if not _table_exists("quota_counters"):
        op.create_table(
            "quota_counters",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("application_id", sa.String(128), nullable=False, server_default=""),
            sa.Column("quota_class", sa.String(64), nullable=False),
            sa.Column("operation", sa.String(128), nullable=False),
            sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
            sa.UniqueConstraint(
                "tenant_id",
                "application_id",
                "quota_class",
                "operation",
                "window_start",
                name="uq_quota_counter_scope",
            ),
        )
        op.create_index(
            "idx_quota_counter_tenant",
            "quota_counters",
            ["tenant_id"],
        )

    op.execute(sa.text("ALTER TABLE quota_counters ENABLE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE tablename='quota_counters' AND policyname='quota_tenant'
                ) THEN
                    CREATE POLICY quota_tenant ON quota_counters
                        USING (tenant_id = current_setting('app.tenant_id', true))
                        WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
                END IF;
            END $$;
            """
        )
    )
    op.execute(sa.text("ALTER TABLE quota_counters FORCE ROW LEVEL SECURITY"))


def downgrade() -> None:
    op.execute(sa.text("DROP POLICY IF EXISTS quota_tenant ON quota_counters"))
    op.execute(sa.text("DROP TABLE IF EXISTS quota_counters"))
    op.execute(sa.text("DROP POLICY IF EXISTS bg_jobs_tenant ON background_jobs"))
    op.execute(sa.text("DROP TABLE IF EXISTS background_jobs"))
