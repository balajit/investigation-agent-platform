-- scripts/g5_apply_kernel_rls.sql — surgical RLS posture for the G5-formal gate.
--
-- Applies migration 001's posture (tenant isolation + FORCE RLS) to the
-- seven proof-kernel tables, idempotently, WITHOUT running the full
-- alembic chain. Use when the gate target database was created outside
-- migrations (e.g. ORM metadata or test scaffolding) and a full
-- `alembic upgrade head` is riskier than the targeted fix.
--
-- Deviation (record in the verdict): `alembic current` does NOT advance;
-- schema truth for these tables is this file + the driver's B1/B2
-- checks, not the migration stamp. Reconcile with `upgrade head` before
-- any authoritative rollout.
--
-- Run as a superuser or table owner (enables FORCE RLS):
--   psql $G5_ADMIN_DSN -v ON_ERROR_STOP=1 -f scripts/g5_apply_kernel_rls.sql
-- Then verify:
--   SELECT relname, relforcerowsecurity FROM pg_class
--    WHERE relname IN ('investigations','evidence','timeline_events',
--                      'hypotheses','findings','investigation_transitions',
--                      'evidence_relationships');
--   SELECT tablename, policyname FROM pg_policies
--    WHERE tablename IN ('investigations','evidence','timeline_events',
--                        'hypotheses','findings','investigation_transitions',
--                        'evidence_relationships');
--   -- expect 7 rows each, force=true throughout.

DO $$
DECLARE
    t text;
    tables text[] := ARRAY[
        'investigations', 'evidence', 'timeline_events', 'hypotheses',
        'findings', 'investigation_transitions', 'evidence_relationships'
    ];
BEGIN
    FOREACH t IN ARRAY tables
    LOOP
        EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64)', t);
        EXECUTE format('CREATE INDEX IF NOT EXISTS idx_%I_tenant ON %I(tenant_id)', t, t);
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = t AND policyname = 'tenant_isolation') THEN
            EXECUTE format(
                'CREATE POLICY tenant_isolation ON %I '
                'USING (tenant_id = current_setting(%L, true)) '
                'WITH CHECK (tenant_id = current_setting(%L, true))',
                t, 'app.tenant_id', 'app.tenant_id'
            );
        END IF;
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    END LOOP;
END $$;
