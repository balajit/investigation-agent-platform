"""Core retention classes + RLS coverage (TO_DO-023, TO_DO-014).

TO_DO-023: adds ``retention_class`` to the four core tables
(investigations, evidence, timeline_events, investigation_transitions),
backfilled by the server default. The ORM sets the same client-side
default; the server default is intentionally kept so metadata and DB agree
and future autogenerates stay quiet.

TO_DO-014: enables RLS + tenant_isolation policy + FORCE ROW LEVEL
SECURITY on the four tenant-scoped tables that predate the per-migration
RLS pattern (investigation_facts, investigation_entities,
investigation_conclusions, application_profiles). Same shape as 001/002.

Autogenerate noise (server_default rendering drift, ix_/idx_ naming drift,
GIN/HNSW index churn from other in-flight work) was stripped — this
revision contains only the Wave 3 scope above.

Revision ID: 3354e515779a
Revises: 016_profile_v2_columns
Create Date: 2026-10-15
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "3354e515779a"
down_revision = "016_profile_v2_columns"
branch_labels = None
depends_on = None

_RLS_TABLES = (
    "investigation_facts",
    "investigation_entities",
    "investigation_conclusions",
    "application_profiles",
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


_RETENTION_DEFAULTS = (
    ("investigations", "investigation"),
    ("evidence", "evidence"),
    ("timeline_events", "timeline"),
    ("investigation_transitions", "transition"),
)


def upgrade() -> None:
    # Fresh-install guard (chain-rot repair): the 000 baseline creates the
    # full current schema, so retention_class may already exist. Databases
    # upgrading from pre-3354 history take the create path via the same
    # guards. The RLS block below is already idempotent (IF NOT EXISTS
    # policy guard; ENABLE/FORCE are re-runnable).
    for table, default in _RETENTION_DEFAULTS:
        if _column_exists(table, "retention_class"):
            continue
        op.add_column(
            table,
            sa.Column(
                "retention_class",
                sa.String(length=64),
                server_default=default,
                nullable=False,
            ),
        )
    for table in _RLS_TABLES:
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_policies
                        WHERE tablename='{table}' AND policyname='tenant_isolation'
                    ) THEN
                        CREATE POLICY tenant_isolation ON {table}
                            USING (tenant_id = current_setting('app.tenant_id', true))
                            WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
                    END IF;
                END $$;
                """
            )
        )
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))


def downgrade() -> None:
    for table in _RLS_TABLES:
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
        op.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
    op.drop_column("investigation_transitions", "retention_class")
    op.drop_column("timeline_events", "retention_class")
    op.drop_column("evidence", "retention_class")
    op.drop_column("investigations", "retention_class")
