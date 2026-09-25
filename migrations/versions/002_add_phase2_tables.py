"""Alembic migration: durable checkpoint/transition tenant scoping + Phase 2 tables.

Adds:
- tenant_id + RLS to investigation_checkpoints and investigation_transitions
  (F-011/F-012 — these repositories are now durable and must be tenant-isolated
  identically to every other table).
- idempotency_keys (F-013/F-014 — durable, request-hash-bound idempotency).
- outbox_events (F-058 — transactional outbox for domain event publication).
- action_executions (F-004/F-068 — durable action authorization/audit trail).
"""

# revision identifiers, used by Alembic.
revision = "002_add_phase2_tables"
down_revision = "001_add_rls"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # type: ignore[import-not-found]
from alembic import op  # type: ignore[import-not-found]

_RLS_TABLES = ["investigation_checkpoints", "investigation_transitions"]


def _enable_rls(table: str) -> None:
    op.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64)"))
    op.execute(sa.text(f"CREATE INDEX IF NOT EXISTS idx_{table}_tenant ON {table}(tenant_id)"))
    op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(f"""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename='{table}' AND policyname='tenant_isolation') THEN
                CREATE POLICY tenant_isolation ON {table}
                    USING (tenant_id = current_setting('app.tenant_id', true))
                    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
            END IF;
        END $$;
    """)
    )
    op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    try:
        return sa.inspect(bind).has_table(table)
    except Exception:
        # Offline (--sql) mode uses a MockConnection that cannot be inspected;
        # emit the DDL unconditionally so SQL scripts stay complete.
        return False


def upgrade() -> None:
    for table in _RLS_TABLES:
        _enable_rls(table)

    # F-035: durable content-addressed evidence dedup.
    op.execute(
        sa.text("ALTER TABLE evidence ADD COLUMN IF NOT EXISTS fingerprint VARCHAR(128) DEFAULT ''")
    )
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_evidence_tenant_fingerprint') THEN "
            "ALTER TABLE evidence ADD CONSTRAINT uq_evidence_tenant_fingerprint UNIQUE (tenant_id, fingerprint); "
            "END IF; END $$;"
        )
    )

    # Fresh installs already have these tables via the 000 baseline; only
    # create what is actually missing so upgrade head is idempotent.
    if not _table_exists("idempotency_keys"):
        op.create_table(
            "idempotency_keys",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("idempotency_key", sa.String(256), nullable=False),
            sa.Column("request_hash", sa.String(128), nullable=False),
            sa.Column("response_status", sa.Integer, nullable=False),
            sa.Column("response_json", sa.dialects.postgresql.JSONB, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_idempotency_tenant_key"),
        )
        op.create_index("idx_idempotency_tenant", "idempotency_keys", ["tenant_id"])

    if not _table_exists("outbox_events"):
        op.create_table(
            "outbox_events",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("investigation_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("event_type", sa.String(128), nullable=False),
            sa.Column("idempotency_key", sa.String(256), nullable=False),
            sa.Column("payload_json", sa.dialects.postgresql.JSONB, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("dispatch_attempts", sa.Integer, nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text, nullable=True),
            sa.UniqueConstraint(
                "tenant_id", "idempotency_key", name="uq_outbox_tenant_idempotency_key"
            ),
        )
        op.create_index("idx_outbox_undispatched", "outbox_events", ["dispatched_at", "created_at"])

    if not _table_exists("action_executions"):
        op.create_table(
            "action_executions",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column(
                "investigation_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False
            ),
            sa.Column("action_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("action_type", sa.String(64), nullable=False),
            sa.Column("principal_id", sa.String(128), nullable=False),
            sa.Column("policy_version", sa.String(32), nullable=False, server_default="v1"),
            sa.Column("result_status", sa.String(32), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("action_id", name="uq_action_execution_action_id"),
        )
        op.create_index(
            "idx_action_exec_tenant_inv", "action_executions", ["tenant_id", "investigation_id"]
        )

    for table in ("idempotency_keys", "outbox_events", "action_executions"):
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(f"""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename='{table}' AND policyname='tenant_isolation') THEN
                    CREATE POLICY tenant_isolation ON {table}
                        USING (tenant_id = current_setting('app.tenant_id', true))
                        WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
                END IF;
            END $$;
        """)
        )
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))


def downgrade() -> None:
    op.execute(
        sa.text("ALTER TABLE evidence DROP CONSTRAINT IF EXISTS uq_evidence_tenant_fingerprint")
    )
    for table in ("idempotency_keys", "outbox_events", "action_executions"):
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
        op.drop_table(table)
    for table in _RLS_TABLES:
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
        op.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
