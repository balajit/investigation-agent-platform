"""Alembic migration: allow NULL evidence.observed_at (Part 9).

A missing/invalid provider timestamp is now represented as unknown (`None`)
instead of fabricated from retrieval time. No data migration: existing rows
carry real timestamps. Downgrade requires no NULL rows present.
"""

# revision identifiers, used by Alembic.
revision = "008_evidence_observed_at_nullable"
down_revision = "007_profile_id_128"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # type: ignore[import-not-found]
from alembic import op  # type: ignore[import-not-found]


def upgrade() -> None:
    op.alter_column(
        "evidence",
        "observed_at",
        existing_type=sa.DateTime(timezone=True),
        type_=sa.DateTime(timezone=True),
        existing_nullable=False,
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "evidence",
        "observed_at",
        existing_type=sa.DateTime(timezone=True),
        type_=sa.DateTime(timezone=True),
        existing_nullable=True,
        nullable=False,
    )
