"""Alembic migration: pgvector extension for the Mem0 vector substrate (Part 6 Slice 1).

Enables `pgvector` on the existing Postgres so Mem0 OSS can use it as its
vector store — no new database container. Requires a superuser (or a
pre-provisioned image) at migrate time; operators without superuser must
enable the extension out-of-band before running this migration.

Mem0-managed collections are a disposable index: embeddings + metadata only,
rebuildable from knowledge envelopes at any time.
"""

# revision identifiers, used by Alembic.
revision = "006_pgvector_extension"
down_revision = "005_knowledge_layer"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # type: ignore[import-not-found]
from alembic import op  # type: ignore[import-not-found]


def upgrade() -> None:
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))


def downgrade() -> None:
    # Do NOT drop the extension on downgrade: other objects may depend on it.
    # Removal is an explicit operator action.
    pass
