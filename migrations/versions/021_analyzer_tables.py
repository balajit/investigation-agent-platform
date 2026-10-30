"""Alembic migration: analyzer Stage-1 store (Acquired Judgement Phase 1).

analyzer_files/symbols/endpoints/edges keyed by (tenant_id, repo, sha).
Each table creation is guarded by an existence check so fresh installs (whose
000 baseline already contains these tables) and upgrades share one path.
"""

# revision identifiers, used by Alembic.
revision = "021_analyzer_tables"
down_revision = "020_conclusion_verifier"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # type: ignore[import-not-found]
from alembic import op  # type: ignore[import-not-found]
from sqlalchemy.dialects import postgresql  # type: ignore[import-not-found]


def _base_columns():
    return [
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("repo", sa.String(256), nullable=False),
        sa.Column("sha", sa.String(128), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(80), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    try:
        return sa.inspect(bind).has_table(table)
    except Exception:
        # Offline (--sql) mode: report missing so DDL is emitted.
        return False


def upgrade() -> None:
    # Fresh-install guard (chain-rot repair): the 000 baseline creates the
    # full current schema, so these tables may already exist. Unlike the
    # original "not rerunnable" note, each table is now guarded individually.
    if not _table_exists("analyzer_files"):
        op.create_table(
            "analyzer_files",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            *_base_columns(),
            sa.Column("language", sa.String(64), nullable=False, server_default="unknown"),
            sa.Column("is_generated", sa.Boolean(), nullable=False, server_default="false"),
            sa.Column("generated_reason", sa.Text(), nullable=False, server_default=""),
            sa.UniqueConstraint("tenant_id", "repo", "sha", "path", name="uq_analyzer_files"),
        )
    if not _table_exists("analyzer_symbols"):
        op.create_table(
            "analyzer_symbols",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("repo", sa.String(256), nullable=False),
            sa.Column("sha", sa.String(128), nullable=False),
            sa.Column("path", sa.Text(), nullable=False),
            sa.Column("name", sa.String(512), nullable=False),
            sa.Column("kind", sa.String(64), nullable=False, server_default="unknown"),
            sa.Column("line", sa.Integer(), nullable=False, server_default="1"),
            sa.Column(
                "annotations",
                postgresql.JSONB(astext_type=sa.Text()),
                nullable=False,
                server_default="[]",
            ),
            sa.UniqueConstraint(
                "tenant_id", "repo", "sha", "path", "name", "line", name="uq_analyzer_symbols"
            ),
        )
    if not _table_exists("analyzer_endpoints"):
        op.create_table(
            "analyzer_endpoints",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("repo", sa.String(256), nullable=False),
            sa.Column("sha", sa.String(128), nullable=False),
            sa.Column("path", sa.Text(), nullable=False),
            sa.Column("method", sa.String(16), nullable=False),
            sa.Column("route", sa.Text(), nullable=False),
            sa.Column("handler", sa.Text(), nullable=False, server_default=""),
            sa.UniqueConstraint(
                "tenant_id", "repo", "sha", "method", "route", name="uq_analyzer_endpoints"
            ),
        )
    if not _table_exists("analyzer_edges"):
        op.create_table(
            "analyzer_edges",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("tenant_id", sa.String(128), nullable=False),
            sa.Column("repo", sa.String(256), nullable=False),
            sa.Column("sha", sa.String(128), nullable=False),
            sa.Column("src", sa.Text(), nullable=False),
            sa.Column("dst", sa.Text(), nullable=False),
            sa.Column("kind", sa.String(64), nullable=False, server_default="depends_on"),
            sa.Column("confidence", sa.String(16), nullable=False, server_default="Exact"),
            sa.UniqueConstraint(
                "tenant_id", "repo", "sha", "src", "dst", "kind", name="uq_analyzer_edges"
            ),
        )


def downgrade() -> None:
    for table in ("analyzer_edges", "analyzer_endpoints", "analyzer_symbols", "analyzer_files"):
        op.drop_table(table)
