"""Alembic migration: Part 6 knowledge layer tables (Slice 0).

Adds ``knowledge_artifacts`` (RLS with a visibility carve-out: owning
tenant OR ``visibility = 'SHARED_CODE_ISSUE'`` — the policy, not
application code, enforces the D8 sharing rule), ``investigation_sessions``
(strict RLS, owning tenant only), and ``code_issue_index`` (deliberately NO
RLS: fingerprint + opaque UUID + session number + timestamp identify no
tenant by construction; enforced by the two-tenant determinism test).
Also adds nullable ``code_issue_fingerprint`` to ``investigations``.
"""

# revision identifiers, used by Alembic.
revision = "005_knowledge_layer"
down_revision = "004_topology_snapshots"
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
    op.execute(
        sa.text(
            "ALTER TABLE investigations ADD COLUMN IF NOT EXISTS "
            "code_issue_fingerprint VARCHAR(128)"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_inv_fingerprint "
            "ON investigations (code_issue_fingerprint)"
        )
    )

    if not _table_exists("knowledge_artifacts"):
        op.create_table(
            "knowledge_artifacts",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("application_id", sa.String(128), nullable=False),
            sa.Column(
                "investigation_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False
            ),
            sa.Column("kind", sa.String(64), nullable=False),
            sa.Column("statement", sa.Text, nullable=False),
            sa.Column("confidence", sa.Float, nullable=False, server_default="1.0"),
            sa.Column("refresh_policy", sa.String(32), nullable=False, server_default="IMMUTABLE"),
            sa.Column("reverify_json", sa.dialects.postgresql.JSONB, nullable=True),
            sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
            sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "source_evidence_ids",
                sa.dialects.postgresql.JSONB,
                nullable=False,
                server_default="[]",
            ),
            sa.Column(
                "source_log_refs", sa.dialects.postgresql.JSONB, nullable=False, server_default="[]"
            ),
            sa.Column(
                "code_refs", sa.dialects.postgresql.JSONB, nullable=False, server_default="[]"
            ),
            sa.Column("supersedes_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("status", sa.String(32), nullable=False, server_default="ACTIVE"),
            sa.Column(
                "store_refs", sa.dialects.postgresql.JSONB, nullable=False, server_default="{}"
            ),
            sa.Column("visibility", sa.String(32), nullable=False, server_default="TENANT"),
            sa.Column("code_issue_fingerprint", sa.String(128), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "idx_knowledge_tenant_app_kind_status",
            "knowledge_artifacts",
            ["tenant_id", "application_id", "kind", "status"],
        )
        op.create_index(
            "idx_knowledge_fingerprint",
            "knowledge_artifacts",
            ["code_issue_fingerprint", "status"],
        )

    # Visibility carve-out policy: owning tenant sees all own rows; any
    # authenticated tenant sees SHARED_CODE_ISSUE rows (session membership
    # is additionally verified at the application layer before use).
    op.execute(sa.text("ALTER TABLE knowledge_artifacts ENABLE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE tablename='knowledge_artifacts' AND policyname='knowledge_visibility'
                ) THEN
                    CREATE POLICY knowledge_visibility ON knowledge_artifacts
                        USING (
                            tenant_id = current_setting('app.tenant_id', true)
                            OR visibility = 'SHARED_CODE_ISSUE'
                        )
                        WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
                END IF;
            END $$;
            """
        )
    )
    op.execute(sa.text("ALTER TABLE knowledge_artifacts FORCE ROW LEVEL SECURITY"))

    if not _table_exists("investigation_sessions"):
        op.create_table(
            "investigation_sessions",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "investigation_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False
            ),
            sa.Column("session_number", sa.Integer, nullable=False),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "log_refs", sa.dialects.postgresql.JSONB, nullable=False, server_default="[]"
            ),
            sa.Column(
                "trace_refs", sa.dialects.postgresql.JSONB, nullable=False, server_default="[]"
            ),
            sa.Column("status", sa.String(32), nullable=False, server_default="OPEN"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("investigation_id", "session_number", name="uq_session_inv_number"),
        )
        op.create_index(
            "idx_sessions_inv", "investigation_sessions", ["investigation_id", "session_number"]
        )

    op.execute(sa.text("ALTER TABLE investigation_sessions ENABLE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies WHERE tablename='investigation_sessions'
                    AND policyname='tenant_isolation'
                ) THEN
                    CREATE POLICY tenant_isolation ON investigation_sessions
                        USING (tenant_id = current_setting('app.tenant_id', true))
                        WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
                END IF;
            END $$;
            """
        )
    )
    op.execute(sa.text("ALTER TABLE investigation_sessions FORCE ROW LEVEL SECURITY"))

    # Tenant-free coordination index: deliberately NO RLS. Columns identify
    # no tenant by construction (see CodeIssueIndex port contract).
    if not _table_exists("code_issue_index"):
        op.create_table(
            "code_issue_index",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("code_issue_fingerprint", sa.String(128), nullable=False),
            sa.Column("session_number", sa.Integer, nullable=False),
            sa.Column(
                "investigation_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False
            ),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "code_issue_fingerprint",
                "session_number",
                name="uq_code_issue_session",
            ),
        )
        op.create_index(
            "idx_code_issue_fingerprint",
            "code_issue_index",
            ["code_issue_fingerprint", "session_number"],
        )


def downgrade() -> None:
    op.execute(sa.text("DROP POLICY IF EXISTS knowledge_visibility ON knowledge_artifacts"))
    op.execute(sa.text("DROP POLICY IF EXISTS tenant_isolation ON investigation_sessions"))
    op.drop_table("code_issue_index")
    op.drop_table("investigation_sessions")
    op.drop_table("knowledge_artifacts")
    op.execute(sa.text("ALTER TABLE investigations DROP COLUMN IF EXISTS code_issue_fingerprint"))
