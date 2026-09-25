"""Alembic migration: checkpoint schema-version columns (F-025).

Adds schema_version / app_version / state_hash to investigation_checkpoints so
readers can refuse incompatible snapshots instead of blindly hydrating them.
"""

# revision identifiers, used by Alembic.
revision = "003_checkpoint_schema"
down_revision = "002_add_phase2_tables"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # type: ignore[import-not-found]
from alembic import op  # type: ignore[import-not-found]


def upgrade() -> None:
    op.execute(
        sa.text(
            "ALTER TABLE investigation_checkpoints ADD COLUMN IF NOT EXISTS schema_version VARCHAR(32) DEFAULT 'v1'"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE investigation_checkpoints ADD COLUMN IF NOT EXISTS app_version VARCHAR(64) DEFAULT '0.1.0'"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE investigation_checkpoints ADD COLUMN IF NOT EXISTS state_hash VARCHAR(128) DEFAULT ''"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE investigation_checkpoints DROP COLUMN IF EXISTS state_hash"))
    op.execute(sa.text("ALTER TABLE investigation_checkpoints DROP COLUMN IF EXISTS app_version"))
    op.execute(
        sa.text("ALTER TABLE investigation_checkpoints DROP COLUMN IF EXISTS schema_version")
    )
