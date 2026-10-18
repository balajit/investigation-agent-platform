"""Alembic migration: Part 11.5 input requirements + fulfillments.

Adds ``input_requirements`` (durable workflow-blocked-on-caller-data rows;
strict tenant RLS, owning tenant only) and ``input_fulfillments`` (immutable
audit rows carrying content digest + metadata, never raw payload; strict
tenant RLS). Both tables are new — no data migration. Downgrade drops
policies, indexes, and tables.
"""

# revision identifiers, used by Alembic.
revision = "011_input_requirements"
down_revision = "010_background_jobs_and_quotas"
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
    if not _table_exists("input_requirements"):
        op.create_table(
            "input_requirements",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column(
                "investigation_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False
            ),
            sa.Column("requirement_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("reason", sa.String(512), nullable=False),
            sa.Column("json_schema", sa.dialects.postgresql.JSONB, nullable=True),
            sa.Column("classification", sa.String(32), nullable=False, server_default="INTERNAL"),
            sa.Column("resume_status", sa.String(32), nullable=False),
            sa.Column("promote_to_evidence", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("state", sa.String(32), nullable=False, server_default="PENDING"),
            sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("provenance_json", sa.dialects.postgresql.JSONB, nullable=True),
        )
        op.create_index(
            "idx_input_req_tenant_inv",
            "input_requirements",
            ["tenant_id", "investigation_id"],
        )

    op.execute(sa.text("ALTER TABLE input_requirements ENABLE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE tablename='input_requirements' AND policyname='input_req_tenant'
                ) THEN
                    CREATE POLICY input_req_tenant ON input_requirements
                        USING (tenant_id = current_setting('app.tenant_id', true))
                        WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
                END IF;
            END $$;
            """
        )
    )
    op.execute(sa.text("ALTER TABLE input_requirements FORCE ROW LEVEL SECURITY"))

    if not _table_exists("input_fulfillments"):
        op.create_table(
            "input_fulfillments",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column(
                "investigation_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False
            ),
            sa.Column("requirement_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("requirement_version", sa.Integer(), nullable=False),
            sa.Column("fulfilled_by", sa.String(256), nullable=False),
            sa.Column("content_digest", sa.String(128), nullable=False),
            sa.Column("content_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "tenant_id",
                "requirement_id",
                "requirement_version",
                name="uq_fulfillment_req_version",
            ),
        )
        op.create_index(
            "idx_fulfillment_tenant_inv",
            "input_fulfillments",
            ["tenant_id", "investigation_id"],
        )

    op.execute(sa.text("ALTER TABLE input_fulfillments ENABLE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE tablename='input_fulfillments' AND policyname='fulfillment_tenant'
                ) THEN
                    CREATE POLICY fulfillment_tenant ON input_fulfillments
                        USING (tenant_id = current_setting('app.tenant_id', true))
                        WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
                END IF;
            END $$;
            """
        )
    )
    op.execute(sa.text("ALTER TABLE input_fulfillments FORCE ROW LEVEL SECURITY"))


def downgrade() -> None:
    op.execute(sa.text("DROP POLICY IF EXISTS fulfillment_tenant ON input_fulfillments"))
    op.execute(sa.text("DROP TABLE IF EXISTS input_fulfillments"))
    op.execute(sa.text("DROP POLICY IF EXISTS input_req_tenant ON input_requirements"))
    op.execute(sa.text("DROP TABLE IF EXISTS input_requirements"))
