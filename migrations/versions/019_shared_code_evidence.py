"""Alembic migration: shareable code evidence (G6).

Code evidence is repository-scoped reference data, not investigation-scoped
observations. `lifecycle_scope` marks the split (observational default);
`evidence_bank_interest` records which investigations banked interest in a
canonical row so hydration serves shared rows and purge respects references.
"""

# revision identifiers, used by Alembic.
revision = "019_shared_code_evidence"
down_revision = "018_log_exemplars"
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
    # full current schema, so this column/table may already exist.
    if not _column_exists("evidence", "lifecycle_scope"):
        op.add_column(
            "evidence",
            sa.Column(
                "lifecycle_scope",
                sa.String(32),
                nullable=False,
                server_default="observational",
            ),
        )
    if _table_exists("evidence_bank_interest"):
        return
    op.create_table(
        "evidence_bank_interest",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column(
            "investigation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("investigations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("evidence_key", sa.String(256), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("tenant_id", "investigation_id", "evidence_key"),
    )
    op.create_index(
        "idx_bank_interest_evidence_key",
        "evidence_bank_interest",
        ["tenant_id", "evidence_key"],
    )


def downgrade() -> None:
    op.drop_index("idx_bank_interest_evidence_key", table_name="evidence_bank_interest")
    op.drop_table("evidence_bank_interest")
    op.drop_column("evidence", "lifecycle_scope")
