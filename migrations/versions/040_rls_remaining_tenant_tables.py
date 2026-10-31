"""Alembic migration: R11 RLS for the seven unprotected tenant tables (requires owner approval).

Applies ENABLE + FORCE RLS plus tenant_isolation to each table carrying
tenant_id without coverage, and creates the narrow iap_tenant_directory
lookup (role + function) for the R2 index-scope guard.
"""

# revision identifiers, used by Alembic.
revision = "040_rls_remaining_tenant_tables"
down_revision = "039_knowledge_shared_split"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # type: ignore[import-not-found]
from alembic import op  # type: ignore[import-not-found]

_RLS_TABLES = [
    "investigation_contradictions",
    "log_exemplars",
    "evidence_bank_interest",
    "analyzer_files",
    "analyzer_symbols",
    "analyzer_edges",
    "analyzer_endpoints",
]


def upgrade() -> None:
    for table in _RLS_TABLES:
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(
            sa.text(
                f"""DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = '{table}'
                    AND policyname = 'tenant_isolation') THEN
                    CREATE POLICY tenant_isolation ON {table}
                        USING (tenant_id = current_setting('app.tenant_id', true))
                        WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
                END IF;
            END $$"""
            )
        )
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            """DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'iap_tenant_directory') THEN
                CREATE ROLE iap_tenant_directory NOLOGIN NOSUPERUSER NOCREATEDB
                    NOCREATEROLE NOBYPASSRLS;
            END IF;
        END $$"""
        )
    )
    op.execute(
        sa.text(
            "GRANT SELECT (tenant_id) ON investigations, application_profiles "
            "TO iap_tenant_directory"
        )
    )
    op.execute(
        sa.text(
            "GRANT SELECT (canonical_id) ON tenant_registry TO iap_tenant_directory"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY tenant_directory_read ON investigations "
            "FOR SELECT TO iap_tenant_directory USING (true)"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY tenant_directory_read ON application_profiles "
            "FOR SELECT TO iap_tenant_directory USING (true)"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY tenant_directory_read ON tenant_registry "
            "FOR SELECT TO iap_tenant_directory USING (true)"
        )
    )
    op.execute(
        sa.text(
            """CREATE OR REPLACE FUNCTION iap_known_tenant_ids() RETURNS SETOF text
            LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
                SELECT canonical_id::text FROM tenant_registry
                UNION SELECT tenant_id::text FROM investigations
                UNION SELECT tenant_id::text FROM application_profiles
            $$"""
        )
    )
    op.execute(
        sa.text(
            "ALTER FUNCTION iap_known_tenant_ids() OWNER TO iap_tenant_directory"
        )
    )
    op.execute(sa.text("REVOKE ALL ON FUNCTION iap_known_tenant_ids() FROM PUBLIC"))
    op.execute(
        sa.text("GRANT EXECUTE ON FUNCTION iap_known_tenant_ids() TO iap_app, iap_worker")
    )


def downgrade() -> None:
    op.execute(sa.text("REVOKE EXECUTE ON FUNCTION iap_known_tenant_ids() FROM iap_app, iap_worker"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS iap_known_tenant_ids()"))
    op.execute(
        sa.text("DROP POLICY IF EXISTS tenant_directory_read ON tenant_registry")
    )
    op.execute(
        sa.text(
            "DROP POLICY IF EXISTS tenant_directory_read ON application_profiles"
        )
    )
    op.execute(
        sa.text("DROP POLICY IF EXISTS tenant_directory_read ON investigations")
    )
    for table in _RLS_TABLES:
        op.execute(sa.text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
        # DISABLE clears relrowsecurity but leaves the FORCE flag set;
        # NO FORCE restores the exact pre-upgrade state (both flags false).
        op.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
