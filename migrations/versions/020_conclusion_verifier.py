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


def upgrade() -> None:
    op.add_column(
        "investigation_conclusions",
        sa.Column("verifier_version", sa.String(16), nullable=True),
    )
    op.add_column(
        "investigation_conclusions",
        sa.Column("best_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "investigation_conclusions",
        sa.Column(
            "against_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "investigation_conclusions",
        sa.Column("provisional", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "investigation_conclusions",
        sa.Column(
            "ranked_alternatives",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("investigation_conclusions", "ranked_alternatives")
    op.drop_column("investigation_conclusions", "provisional")
    op.drop_column("investigation_conclusions", "against_evidence")
    op.drop_column("investigation_conclusions", "best_reason")
    op.drop_column("investigation_conclusions", "verifier_version")
