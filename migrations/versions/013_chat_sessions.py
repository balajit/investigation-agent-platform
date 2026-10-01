"""Alembic migration: Part 11.10 chat sessions + messages.

Adds ``chat_sessions`` (tenant-scoped durable sessions, optional
investigation binding, legal hold) and ``chat_messages`` (ordered messages
per session, token/cost accounting). Both tables are new — no data
migration. Downgrade drops policies, indexes, and tables.
"""

# revision identifiers, used by Alembic.
revision = "013_chat_sessions"
down_revision = "012_finding_clusters"
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
    if not _table_exists("chat_sessions"):
        op.create_table(
            "chat_sessions",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column(
                "investigation_id",
                sa.dialects.postgresql.UUID(as_uuid=True),
                nullable=True,
            ),
            sa.Column("application_id", sa.String(128), nullable=True),
            sa.Column("status", sa.String(32), nullable=False, server_default="ACTIVE"),
            sa.Column("classification", sa.String(32), nullable=False, server_default="INTERNAL"),
            sa.Column("allowed_provider", sa.String(32), nullable=False, server_default="openai"),
            sa.Column("authorization_reference", sa.String(256), nullable=False, server_default=""),
            sa.Column("legal_hold", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("contract_version", sa.String(32), nullable=False, server_default="1.0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "idx_chat_session_tenant_created",
            "chat_sessions",
            ["tenant_id", "created_at"],
        )
        op.create_index(
            "idx_chat_session_investigation",
            "chat_sessions",
            ["tenant_id", "investigation_id"],
        )
    _tenant_policy("chat_sessions", "chat_session_tenant")

    if not _table_exists("chat_messages"):
        op.create_table(
            "chat_messages",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("session_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("role", sa.String(16), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("estimated_cost_usd", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("provenance_json", sa.dialects.postgresql.JSONB, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "idx_chat_message_session_created",
            "chat_messages",
            ["session_id", "created_at"],
        )
        op.create_index(
            "idx_chat_message_tenant_session",
            "chat_messages",
            ["tenant_id", "session_id"],
        )
    _tenant_policy("chat_messages", "chat_message_tenant")


def downgrade() -> None:
    op.execute(sa.text("DROP POLICY IF EXISTS chat_message_tenant ON chat_messages"))
    op.execute(sa.text("DROP TABLE IF EXISTS chat_messages"))
    op.execute(sa.text("DROP POLICY IF EXISTS chat_session_tenant ON chat_sessions"))
    op.execute(sa.text("DROP TABLE IF EXISTS chat_sessions"))
