"""Alembic migration: log exemplar table (log-group persistence).

Persists per-page log groupings alongside raw banked rows so humans can
match groups to rows by direct ID equality (member_evidence_ids ⨝
evidence.id). Exemplar rows are presentation, never citations: the verifier
binds claims to raw member IDs only.
"""

# revision identifiers, used by Alembic.
revision = "018_log_exemplars"
down_revision = "017_contradictions"
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


def upgrade() -> None:
    # Fresh-install guard (chain-rot repair): the 000 baseline creates the
    # full current schema, so this table may already exist.
    if _table_exists("log_exemplars"):
        return
    op.create_table(
        "log_exemplars",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("investigation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("member_evidence_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("span_first", sa.DateTime(timezone=True), nullable=True),
        sa.Column("span_last", sa.DateTime(timezone=True), nullable=True),
        sa.Column("representative_snippet", sa.Text(), nullable=False, server_default=""),
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
    op.create_index(
        "idx_logexemplar_tenant", "log_exemplars", ["tenant_id"]
    )
    op.create_index(
        "idx_logexemplar_tenant_inv",
        "log_exemplars",
        ["tenant_id", "investigation_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_logexemplar_tenant_inv", table_name="log_exemplars")
    op.drop_index("idx_logexemplar_tenant", table_name="log_exemplars")
    op.drop_table("log_exemplars")
