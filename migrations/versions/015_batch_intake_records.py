"""Alembic migration: Part 11.9 batch intake record rows.

Adds ``batch_intake_records`` (per-record state for one batch job:
external key, parameters hash, investigation/child-workflow mapping,
status, attempt, error). The batch lifecycle itself reuses the generic
``background_jobs`` row (kind ``batch-intake``) — this table exists only
because 500 paginated, resumable child rows cannot fit the job's 50-stage
bound. New table — no data migration. Downgrade drops policy, indexes, table.
"""

# revision identifiers, used by Alembic.
revision = "015_batch_intake"
down_revision = "014_reference_documents"
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


def _tenant_policy(table: str, policy: str) -> None:
    op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            f"""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE tablename='{table}' AND policyname='{policy}'
                ) THEN
                    CREATE POLICY {policy} ON {table}
                        USING (tenant_id = current_setting('app.tenant_id', true))
                        WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
                END IF;
            END $$;
            """
        )
    )
    op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))


def upgrade() -> None:
    if not _table_exists("batch_intake_records"):
        op.create_table(
            "batch_intake_records",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("job_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("record_index", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("external_key", sa.String(256), nullable=False),
            sa.Column("application_id", sa.String(128), nullable=False, server_default=""),
            sa.Column("status", sa.String(32), nullable=False, server_default="PENDING"),
            sa.Column(
                "investigation_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
            sa.Column("child_workflow_id", sa.String(256), nullable=True),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error", sa.String(2048), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "tenant_id",
                "job_id",
                "external_key",
                name="uq_batch_record_key",
            ),
        )
        op.create_index(
            "idx_batch_record_job_status",
            "batch_intake_records",
            ["tenant_id", "job_id", "status"],
        )
        op.create_index(
            "idx_batch_record_job_index",
            "batch_intake_records",
            ["tenant_id", "job_id", "record_index"],
        )
    _tenant_policy("batch_intake_records", "batch_record_tenant")


def downgrade() -> None:
    op.execute(sa.text("DROP POLICY IF EXISTS batch_record_tenant ON batch_intake_records"))
    op.execute(sa.text("DROP TABLE IF EXISTS batch_intake_records"))
