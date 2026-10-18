"""Alembic baseline: create the full schema from ORM metadata (F-069).

This is the ``000`` baseline — fresh installs get the complete schema from
``Base.metadata.create_all``; subsequent migrations (001+) evolve it. The
baseline records the schema as-of the Phase 2–4 model set. Production
databases must never be created implicitly at runtime; this migration runs
only via explicit ``alembic upgrade head``.
"""

# revision identifiers, used by Alembic.
revision = "000_baseline_schema"
down_revision = None
branch_labels = None
depends_on = None

from alembic import op  # type: ignore[import-not-found]


def upgrade() -> None:
    from investigation_agent_platform.infrastructure.persistence.models import Base

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    from investigation_agent_platform.infrastructure.persistence.models import Base

    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
