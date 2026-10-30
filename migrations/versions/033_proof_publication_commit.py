"""Alembic migration: proof derivation graph + publication commits (Review Stage C1+C2).

Adds ``proof_derivations`` (persisted derivation-graph nodes; invalidation
closure is recomputed from these rows, never stored) and
``proof_publication_commits`` (one row per accepted commit; a derivation
commits at most once per tenant). Both tables carry the standard tenant
policy (ENABLE + FORCE RLS) from creation. Test-only writers (the commit
service test path); no production write path. Downgrade drops policies,
indexes, and tables.
"""

# revision identifiers, used by Alembic.
revision = "033_proof_publication_commit"
down_revision = "032_hypothesis_version"
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
    if not _table_exists("proof_derivations"):
        op.create_table(
            "proof_derivations",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column(
                "investigation_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False
            ),
            sa.Column("derivation_id", sa.String(256), nullable=False),
            sa.Column("proposition_id", sa.String(256), nullable=False),
            sa.Column("context_id", sa.String(256), nullable=False),
            sa.Column("rule_id", sa.String(128), nullable=False),
            sa.Column("rule_version", sa.String(64), nullable=False, server_default="v1"),
            sa.Column(
                "premise_ids", sa.dialects.postgresql.JSONB(), nullable=False, server_default="[]"
            ),
            sa.Column(
                "depends_on", sa.dialects.postgresql.JSONB(), nullable=False, server_default="[]"
            ),
            sa.Column("route", sa.String(64), nullable=False, server_default="mandatory"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["investigation_id"], ["investigations.id"], ondelete="CASCADE"
            ),
            sa.UniqueConstraint(
                "tenant_id", "derivation_id", name="uq_proof_derivation_tenant_der"
            ),
        )
        op.create_index(
            "idx_proof_derivation_tenant_inv",
            "proof_derivations",
            ["tenant_id", "investigation_id"],
        )
    _tenant_policy("proof_derivations", "tenant_isolation")
    if not _table_exists("proof_publication_commits"):
        op.create_table(
            "proof_publication_commits",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column(
                "investigation_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False
            ),
            sa.Column("derivation_id", sa.String(256), nullable=False),
            sa.Column("context_id", sa.String(256), nullable=False),
            sa.Column("gate_policy_version", sa.String(64), nullable=False),
            sa.Column("reconciliation_state", sa.String(64), nullable=False),
            sa.Column("decision", sa.String(16), nullable=False, server_default="COMMIT"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["investigation_id"], ["investigations.id"], ondelete="CASCADE"
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "derivation_id",
                "gate_policy_version",
                name="uq_proof_commit_tenant_der_policy",
            ),
        )
        op.create_index(
            "idx_proof_commit_tenant_inv",
            "proof_publication_commits",
            ["tenant_id", "investigation_id"],
        )
    _tenant_policy("proof_publication_commits", "tenant_isolation")


def downgrade() -> None:
    for table in ("proof_publication_commits", "proof_derivations"):
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
        op.drop_table(table)
