"""Alembic migration: widen application_profiles.id to match the domain.

`ApplicationProfile.id` allows 128 chars but the column was VARCHAR(64),
rejecting real-world slugs (e.g. 66-char org--repo ids) on save. Widen to
VARCHAR(128); no data migration (existing values fit).
"""

# revision identifiers, used by Alembic.
revision = "007_profile_id_128"
down_revision = "006_pgvector_extension"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # type: ignore[import-not-found]
from alembic import op  # type: ignore[import-not-found]


def upgrade() -> None:
    op.alter_column(
        "application_profiles", "id",
        existing_type=sa.String(64), type_=sa.String(128),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "application_profiles", "id",
        existing_type=sa.String(128), type_=sa.String(64),
        existing_nullable=False,
    )
