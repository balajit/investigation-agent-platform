"""Alembic migration: structured S7 handoff on conclusions.

Adds nullable handoff (NULL = legacy/pre-marker row, never backfilled)
to investigation_conclusions. Stores the S7Handoff classification
(version + state + target + next fetch + resume pointer); the
operator-facing steps stay duplicated in recommended_next_steps.
"""

# revision identifiers, used by Alembic.
revision = "038_conclusion_s7_handoff"
down_revision = "037_conclusion_eliminated_rivals"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # type: ignore[import-not-found]
from alembic import op  # type: ignore[import-not-found]
from sqlalchemy.dialects import postgresql  # type: ignore[import-not-found]


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
    # full current schema, so this column may already exist.
    if _column_exists("investigation_conclusions", "handoff"):
        return
    op.add_column(
        "investigation_conclusions",
        sa.Column("handoff", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("investigation_conclusions", "handoff")
