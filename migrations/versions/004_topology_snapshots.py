"""Alembic migration: Layer 3 topology snapshot audit table (prompt1_v1.md).

Adds ``topology_snapshots`` — tenant-scoped audit/reporting metadata for
Neo4j ingestion attempts. The graph itself lives in Neo4j; this table never
stores raw AST payloads, source content, or credentials. RLS is enabled to
match every other tenant-owned table (F-001/D6).
"""

# revision identifiers, used by Alembic.
revision = "004_topology_snapshots"
down_revision = "003_checkpoint_schema"
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
    if not _table_exists("topology_snapshots"):
        op.create_table(
            "topology_snapshots",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("application_id", sa.String(128), nullable=False),
            sa.Column("repository_id", sa.String(128), nullable=False),
            sa.Column("revision", sa.String(128), nullable=False),
            sa.Column("schema_version", sa.String(32), nullable=False, server_default="v1"),
            sa.Column("parser_version", sa.String(64), nullable=False, server_default="unknown"),
            sa.Column("payload_hash", sa.String(128), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="PENDING"),
            sa.Column("node_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("edge_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("error_summary", sa.Text, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint(
                "tenant_id",
                "repository_id",
                "revision",
                "payload_hash",
                name="uq_topology_snapshot_tenant_repo_rev_hash",
            ),
        )
        op.create_index(
            "idx_topology_snapshot_tenant_repo_rev",
            "topology_snapshots",
            ["tenant_id", "repository_id", "revision"],
        )

    op.execute(sa.text("ALTER TABLE topology_snapshots ENABLE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE tablename='topology_snapshots' AND policyname='tenant_isolation'
                ) THEN
                    CREATE POLICY tenant_isolation ON topology_snapshots
                        USING (tenant_id = current_setting('app.tenant_id', true))
                        WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
                END IF;
            END $$;
            """
        )
    )
    op.execute(sa.text("ALTER TABLE topology_snapshots FORCE ROW LEVEL SECURITY"))


def downgrade() -> None:
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON topology_snapshots"))
    op.drop_table("topology_snapshots")
