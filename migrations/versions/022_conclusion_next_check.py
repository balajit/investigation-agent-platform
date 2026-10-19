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


def upgrade() -> None:
    op.add_column(
        "investigation_conclusions",
        sa.Column("next_check", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("investigation_conclusions", "next_check")
