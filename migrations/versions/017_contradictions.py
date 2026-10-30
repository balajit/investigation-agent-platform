"""Alembic migration: investigation contradictions table (P0-3).

Persists model-reported contradictions so verification and the conclusion
gate evaluate them instead of receiving hardcoded empty lists.
"""

# revision identifiers, used by Alembic.
revision = "017_contradictions"
down_revision = "3354e515779a"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # type: ignore[import-not-found]
from alembic import op  # type: ignore[import-not-found]
from sqlalchemy.dialects import postgresql  # type: ignore[import-not-found]


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    try:
        return sa.inspect(bind).has_table(table)
    except Exception:
        # Offline (--sql) mode: report missing so DDL is emitted.
        return False


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    try:
        inspector = sa.inspect(bind)
        columns = inspector.get_columns(table)
    except Exception:
        # Missing table, or offline (--sql) mode: report missing so DDL is emitted.
        return False
    return column in {col["name"] for col in columns}


def upgrade() -> None:
    # Fresh-install guard (chain-rot repair): the 000 baseline creates the
    # full current schema, so this table/column may already exist.
    if not _table_exists("investigation_contradictions"):
        op.create_table(
            "investigation_contradictions",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("investigation_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("statement", sa.Text(), nullable=False),
            sa.Column("severity", sa.String(32), nullable=False, server_default="MEDIUM"),
            sa.Column("evidence_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
            sa.Column("hypothesis_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
            sa.Column(
                "resolution_status", sa.String(32), nullable=False, server_default="UNRESOLVED"
            ),
            sa.Column("resolved_by", sa.String(128), nullable=True),
            sa.Column("resolution_rationale", sa.Text(), nullable=True),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("superseded_by", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(
                ["investigation_id"], ["investigations.id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("idx_contradiction_tenant", "investigation_contradictions", ["tenant_id"])
        op.create_index(
            "idx_contradiction_tenant_inv",
            "investigation_contradictions",
            ["tenant_id", "investigation_id"],
        )
    # P0-3: validated causal steps live on the hypothesis row.
    if not _column_exists("hypotheses", "causal_steps_json"):
        op.add_column(
            "hypotheses",
            sa.Column("causal_steps_json", postgresql.JSONB(), nullable=False, server_default="[]"),
        )


def downgrade() -> None:
    op.drop_column("hypotheses", "causal_steps_json")
    op.drop_index("idx_contradiction_tenant_inv", table_name="investigation_contradictions")
    op.drop_index("idx_contradiction_tenant", table_name="investigation_contradictions")
    op.drop_table("investigation_contradictions")
