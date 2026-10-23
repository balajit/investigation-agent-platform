"""Alembic migration: conclusion verifier provenance + in-band fields.

Adds nullable verifier_version (NULL = legacy/pre-marker conclusions),
best_reason, against_evidence, provisional, and ranked_alternatives to
investigation_conclusions. All nullable/with defaults so historical rows
read as legacy without backfill.
"""

# revision identifiers, used by Alembic.
revision = "020_conclusion_verifier"
down_revision = "019_shared_code_evidence"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # type: ignore[import-not-found]
from alembic import op  # type: ignore[import-not-found]
from sqlalchemy.dialects import postgresql  # type: ignore[import-not-found]

_NEW_COLUMNS = (
    ("verifier_version", sa.Column("verifier_version", sa.String(16), nullable=True)),
    ("best_reason", sa.Column("best_reason", sa.Text(), nullable=True)),
    (
        "against_evidence",
        sa.Column(
            "against_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    ),
    (
        "provisional",
        sa.Column("provisional", sa.Boolean(), nullable=False, server_default="false"),
    ),
    (
        "ranked_alternatives",
        sa.Column(
            "ranked_alternatives",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    ),
)


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
    # full current schema, so these columns may already exist.
    for name, column in _NEW_COLUMNS:
        if not _column_exists("investigation_conclusions", name):
            op.add_column("investigation_conclusions", column)


def downgrade() -> None:
    op.drop_column("investigation_conclusions", "ranked_alternatives")
    op.drop_column("investigation_conclusions", "provisional")
    op.drop_column("investigation_conclusions", "against_evidence")
    op.drop_column("investigation_conclusions", "best_reason")
    op.drop_column("investigation_conclusions", "verifier_version")
