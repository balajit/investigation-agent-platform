"""Alembic migration: hypothesis optimistic-concurrency version (WP-C2).

Adds non-nullable ``version`` (default 1) to ``hypotheses`` — the Stage C
entry criterion for atomic publication. Existing rows backfill to 1 via
server_default; new rows start at 1 and every guarded update bumps by one.
"""

# revision identifiers, used by Alembic.
revision = "032_hypothesis_version"
down_revision = "031_onboarding_deletion"
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
    # full current schema from ORM metadata, so the column may already exist.
    if not _column_exists("hypotheses", "version"):
        op.add_column(
            "hypotheses",
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        )


def downgrade() -> None:
    op.drop_column("hypotheses", "version")
