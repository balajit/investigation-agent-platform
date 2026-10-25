"""Add persisted caller enumeration diagnostics to conclusions."""

# revision identifiers, used by Alembic.
revision = "035_caller_coverage_diagnostics"
down_revision = "034_proof_tables_grants"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # type: ignore[import-not-found]
from alembic import op  # type: ignore[import-not-found]
from sqlalchemy.dialects import postgresql  # type: ignore[import-not-found]


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    try:
        return column in {item["name"] for item in sa.inspect(bind).get_columns(table)}
    except Exception:
        return False


def upgrade() -> None:
    for name, column in (
        (
            "proven_callers",
            sa.Column("proven_callers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        ),
        (
            "unresolved_callers",
            sa.Column("unresolved_callers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        ),
        ("caller_coverage", sa.Column("caller_coverage", sa.String(length=32), nullable=True)),
    ):
        if not _column_exists("investigation_conclusions", name):
            op.add_column("investigation_conclusions", column)


def downgrade() -> None:
    for name in ("caller_coverage", "unresolved_callers", "proven_callers"):
        if _column_exists("investigation_conclusions", name):
            op.drop_column("investigation_conclusions", name)
