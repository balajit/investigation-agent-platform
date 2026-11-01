"""Create S7 continuation, conclusion history, and reasoning lease tables."""

import sqlalchemy as sa  # type: ignore[import-not-found]
from alembic import op  # type: ignore[import-not-found]
from sqlalchemy.dialects import postgresql  # type: ignore[import-not-found]

revision = "041_s7_continuations"
down_revision = "040_rls_remaining_tenant_tables"
branch_labels = None
depends_on = None


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    try:
        inspector = sa.inspect(bind)
        return table in inspector.get_table_names()
    except Exception:
        # Offline (--sql) mode: report missing so DDL is emitted.
        return False


def _tenant_rls(table: str) -> None:
    op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            f"""DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_policies
                               WHERE tablename = '{table}'
                               AND policyname = 'tenant_isolation') THEN
                    CREATE POLICY tenant_isolation ON {table}
                    USING (tenant_id = current_setting('app.tenant_id', true))
                    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
                END IF;
            END $$"""
        )
    )
    op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))


def upgrade() -> None:
    # Fresh-install guard (chain-rot repair, same convention as 037/038):
    # the 000 baseline creates the full current schema from ORM metadata,
    # so these tables (plus their constraints and indexes) may already
    # exist. RLS policies are applied regardless: create_all does not
    # create them.
    if not _table_exists("s7_continuations"):
        op.create_table(
            "s7_continuations",
            sa.Column("continuation_id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("investigation_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("version", sa.String(32), nullable=False),
            sa.Column("handoff_version", sa.String(16), nullable=False),
            sa.Column("source_handoff_state", sa.String(32), nullable=False),
            sa.Column("action", sa.String(64), nullable=False),
            sa.Column("parameters", postgresql.JSONB, nullable=False),
            sa.Column("target", sa.String(1024), nullable=False),
            sa.Column("price", sa.Numeric(10, 2, asdecimal=False), nullable=False),
            sa.Column("idempotency_key", sa.String(128), nullable=False),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("claimed_at", sa.DateTime(timezone=True)),
            sa.Column("claim_lease_expires_at", sa.DateTime(timezone=True)),
            sa.Column("last_run_id", sa.String(64)),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column("last_error_code", sa.String(64)),
            sa.UniqueConstraint(
                "tenant_id", "investigation_id", "idempotency_key",
                name="uq_s7_continuation_tenant_investigation_key",
            ),
            sa.CheckConstraint("price > 0", name="ck_s7_continuation_positive_price"),
            sa.CheckConstraint(
                "attempt_count >= 0 AND attempt_count <= 20",
                name="ck_s7_continuation_attempt_bounds",
            ),
        )
        op.create_index("idx_s7_continuations_tenant", "s7_continuations", ["tenant_id"])
        op.create_index("idx_s7_continuations_investigation", "s7_continuations", ["investigation_id"])
        op.create_index(
            "idx_s7_continuation_tenant_inv",
            "s7_continuations",
            ["tenant_id", "investigation_id"],
        )

    if not _table_exists("conclusion_history"):
        op.create_table(
            "conclusion_history",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column(
                "investigation_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("investigations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("superseded_by_continuation_id", postgresql.UUID(as_uuid=True)),
            sa.Column("status", sa.String(64), nullable=False),
            sa.Column("handoff_version", sa.String(16)),
            sa.Column("snapshot", postgresql.JSONB, nullable=False),
        )
        op.create_index("idx_conclusion_history_tenant", "conclusion_history", ["tenant_id"])
        op.create_index("idx_conclusion_history_investigation", "conclusion_history", ["investigation_id"])
        op.create_index(
            "idx_conclusion_history_tenant_inv",
            "conclusion_history",
            ["tenant_id", "investigation_id"],
        )
        op.create_index(
            "ix_conclusion_history_superseded_by_continuation_id",
            "conclusion_history",
            ["superseded_by_continuation_id"],
        )

    if not _table_exists("continuation_reasoning_leases"):
        op.create_table(
            "continuation_reasoning_leases",
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column(
                "investigation_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("investigations.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("continuation_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("attempt", sa.Integer, nullable=False),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("run_id", sa.String(64)),
        )
        op.create_index("idx_reasoning_lease_tenant", "continuation_reasoning_leases", ["tenant_id"])

    for table in (
        "s7_continuations",
        "conclusion_history",
        "continuation_reasoning_leases",
    ):
        _tenant_rls(table)


def downgrade() -> None:
    for table in (
        "continuation_reasoning_leases",
        "conclusion_history",
        "s7_continuations",
    ):
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
        op.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
        op.drop_table(table)
