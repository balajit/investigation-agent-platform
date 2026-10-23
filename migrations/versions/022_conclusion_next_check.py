"""Alembic migration: conclusion re-verify directive.

Adds nullable next_check (NULL = established-clean or legacy conclusions)
to investigation_conclusions. Nullable so historical rows read without
backfill.
"""

# revision identifiers, used by Alembic.
revision = "022_conclusion_next_check"
down_revision = "021_analyzer_tables"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # type: ignore[import-not-found]
from alembic import op  # type: ignore[import-not-found]


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
    if _column_exists("investigation_conclusions", "next_check"):
        return
    op.add_column(
        "investigation_conclusions",
        sa.Column("next_check", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("investigation_conclusions", "next_check")
