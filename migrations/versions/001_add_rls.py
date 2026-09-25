"""Alembic migration: RLS hard requirement for production.

Hard requirement per spec: every table enforces tenant isolation via
Postgres Row-Level Security + SET LOCAL app.tenant_id per transaction.

Tables: investigations, evidence, timeline_events, hypotheses,
        findings, investigation_transitions, evidence_relationships
"""

# revision identifiers, used by Alembic.
revision = "001_add_rls"
down_revision = "000_baseline_schema"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # type: ignore[import-not-found]
from alembic import op  # type: ignore[import-not-found]

TABLES = [
    "investigations",
    "evidence",
    "timeline_events",
    "hypotheses",
    "findings",
    "investigation_transitions",
    "evidence_relationships",
]


def upgrade() -> None:
    for table in TABLES:
        # Ensure tenant_id column exists (idempotent for existing installs)
        # Real migration would use batch_alter_table; stub for plan phase.
        op.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64)"))
        op.execute(sa.text(f"CREATE INDEX IF NOT EXISTS idx_{table}_tenant ON {table}(tenant_id)"))
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(f"""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename='{table}' AND policyname='tenant_isolation') THEN
                    CREATE POLICY tenant_isolation ON {table}
                        USING (tenant_id = current_setting('app.tenant_id', true))
                        WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
                END IF;
            END $$;
        """)
        )
        # Force RLS for table owner
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))


def downgrade() -> None:
    for table in TABLES:
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
        op.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
