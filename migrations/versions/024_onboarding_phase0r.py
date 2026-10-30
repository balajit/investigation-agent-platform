"""Phase 0R onboarding grants, role boundaries, dispatch fencing, and agent
contract tables (support registry + coverage reports).

The database roles are provisioned externally before this additive migration
runs. Revision 023 remains unchanged, and no platform grant is seeded here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "024_onboarding_phase0r"
down_revision = "023_onboarding_control_store"
branch_labels = None
depends_on = None


def _table_exists(table: str) -> bool:
    if context.is_offline_mode():
        return context.get_starting_revision_argument() in (None, "base")
    return sa.inspect(op.get_bind()).has_table(table)


def upgrade() -> None:
    # Existing upgraded databases have only revision-023 structures. Fresh
    # installs may already have these additive tables/columns from metadata.
    if not _table_exists("onboarding_grants"):
        op.create_table(
            "onboarding_grants",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("principal_id", sa.String(256), nullable=False),
            sa.Column("capability", sa.String(64), nullable=False),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("application_id", sa.String(128), nullable=False),
            sa.Column("environment", sa.String(64), nullable=False),
            sa.Column("operation", sa.String(128), nullable=False),
            sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("policy_version", sa.String(64), nullable=False),
            sa.Column("issuer_id", sa.String(256), nullable=False),
            sa.Column("approved_by", sa.String(256), nullable=False),
            sa.Column("approval_digest", sa.String(64), nullable=False),
            sa.Column("revoked_by", sa.String(256), nullable=True),
            sa.Column("revocation_approved_by", sa.String(256), nullable=True),
            sa.Column("revocation_reason", sa.String(512), nullable=True),
            sa.Column("reason", sa.String(512), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "tenant_id = '__platform__' OR "
                "(tenant_id = lower(btrim(tenant_id)) AND tenant_id ~ '^[a-z0-9][a-z0-9-]{0,63}$')",
                name="ck_onboarding_grant_tenant_scope",
            ),
            sa.CheckConstraint(
                "expires_at > valid_from", name="ck_onboarding_grant_validity_window"
            ),
        )
        op.create_index(
            "ix_onboarding_grant_principal_scope",
            "onboarding_grants",
            [
                "principal_id",
                "tenant_id",
                "application_id",
                "environment",
                "operation",
                "capability",
            ],
        )

    # The audit table below references (tenant_id, id) on dispatches, which
    # requires the tenant-scoped unique constraint to exist first. Revision
    # 023 never created it, so ensure it here before any dependent DDL.
    op.execute(
        sa.text(
            """DO $$ BEGIN
                  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_onboarding_dispatch_tenant_id_id') THEN
                    ALTER TABLE onboarding_dispatches ADD CONSTRAINT uq_onboarding_dispatch_tenant_id_id UNIQUE (tenant_id, id);
                  END IF;
                END $$"""
        )
    )

    if not _table_exists("onboarding_dispatch_audits"):
        op.create_table(
            "onboarding_dispatch_audits",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("dispatch_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("worker_id", sa.String(128), nullable=False),
            sa.Column("database_role", sa.String(128), nullable=False),
            sa.Column("action", sa.String(32), nullable=False),
            sa.Column("fencing_token", sa.Integer(), nullable=False),
            sa.Column(
                "occurred_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "length(worker_id) > 0", name="ck_onboarding_dispatch_audit_worker_nonempty"
            ),
            sa.CheckConstraint(
                "length(database_role) > 0", name="ck_onboarding_dispatch_audit_role_nonempty"
            ),
            sa.ForeignKeyConstraint(
                ["tenant_id", "dispatch_id"],
                ["onboarding_dispatches.tenant_id", "onboarding_dispatches.id"],
                ondelete="RESTRICT",
                name="fk_onboarding_dispatch_audit_dispatch",
            ),
            sa.CheckConstraint(
                "action IN ('CLAIM', 'RENEW', 'RELEASE', 'STALE_FENCE')",
                name="ck_onboarding_dispatch_audit_action",
            ),
            sa.CheckConstraint(
                "fencing_token >= 0", name="ck_onboarding_dispatch_audit_token_nonnegative"
            ),
        )
        op.create_index(
            "ix_onboarding_dispatch_audit_tenant_dispatch",
            "onboarding_dispatch_audits",
            ["tenant_id", "dispatch_id", "occurred_at"],
        )

    if not _table_exists("onboarding_grant_audits"):
        op.create_table(
            "onboarding_grant_audits",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("grant_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("action", sa.String(16), nullable=False),
            sa.Column("actor_id", sa.String(256), nullable=False),
            sa.Column("approved_by", sa.String(256), nullable=False),
            sa.Column("approval_digest", sa.String(64), nullable=False),
            sa.Column("reason", sa.String(512), nullable=False),
            sa.Column(
                "occurred_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "action IN ('MINT', 'REVOKE')", name="ck_onboarding_grant_audit_action"
            ),
            sa.CheckConstraint(
                "actor_id <> approved_by", name="ck_onboarding_grant_audit_two_person"
            ),
        )
        op.create_index(
            "ix_onboarding_grant_audit_grant",
            "onboarding_grant_audits",
            ["grant_id", "occurred_at"],
        )

    # Agent contract tables (DEP-02b, confirmed for the 0R wave by plan-owner
    # decision 2026-10-22). support_registry is deployment-scoped platform
    # configuration: no tenant column, readable by app roles, writable only
    # by the migration role. coverage_reports is tenant-scoped report linkage
    # for (request_id, revision, generation); the report body lives in
    # report_json, with linkage hashes extracted as columns.
    if not _table_exists("onboarding_support_registry"):
        op.create_table(
            "onboarding_support_registry",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("capability_key", sa.String(128), nullable=False),
            sa.Column("version", sa.String(32), nullable=False),
            sa.Column("strategy", sa.String(32), nullable=False),
            sa.Column("limits", sa.String(512), nullable=True),
            sa.Column(
                "trust_state",
                sa.String(32),
                nullable=False,
                server_default="UNVERIFIED",
            ),
            sa.Column("evidence_requirements", sa.String(1024), nullable=True),
            sa.Column("owner", sa.String(256), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint(
                "strategy IN ('NATIVE', 'GENERIC', 'BRIDGE', 'UNSUPPORTED')",
                name="ck_support_registry_strategy",
            ),
            sa.CheckConstraint(
                "trust_state IN ('UNVERIFIED', 'PROVISIONAL', 'TRUSTED')",
                name="ck_support_registry_trust_state",
            ),
            sa.UniqueConstraint(
                "capability_key",
                "version",
                name="uq_support_registry_capability_version",
            ),
        )
        op.create_index(
            "ix_support_registry_strategy",
            "onboarding_support_registry",
            ["strategy"],
        )

    if not _table_exists("onboarding_coverage_reports"):
        op.create_table(
            "onboarding_coverage_reports",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column(
                "generation",
                sa.Integer(),
                nullable=False,
                server_default="1",
            ),
            sa.Column("report_version", sa.String(32), nullable=False),
            sa.Column("registry_version", sa.String(32), nullable=False),
            sa.Column("catalog_version", sa.String(32), nullable=False),
            sa.Column("estate_sha", sa.String(64), nullable=False),
            sa.Column("input_manifest_hash", sa.String(64), nullable=False),
            sa.Column("report_content_hash", sa.String(64), nullable=False),
            sa.Column("analyzer_version", sa.String(64), nullable=False),
            sa.Column("model_version", sa.String(128), nullable=True),
            sa.Column("report_json", postgresql.JSONB(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.CheckConstraint("revision >= 1", name="ck_coverage_report_revision_positive"),
            sa.CheckConstraint("generation >= 1", name="ck_coverage_report_generation_positive"),
            sa.ForeignKeyConstraint(
                ["tenant_id", "request_id", "revision"],
                [
                    "onboarding_requests.tenant_id",
                    "onboarding_requests.request_id",
                    "onboarding_requests.revision",
                ],
                ondelete="RESTRICT",
                name="fk_coverage_report_request_revision",
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "request_id",
                "revision",
                "generation",
                name="uq_coverage_report_request_generation",
            ),
        )
        op.create_index(
            "ix_coverage_report_tenant_request",
            "onboarding_coverage_reports",
            ["tenant_id", "request_id", "revision"],
        )

    op.execute(
        sa.text(
            """DO $$ BEGIN
                 IF NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid='tenant_registry'::regclass AND attname='lifecycle_epoch' AND NOT attisdropped) THEN
                   ALTER TABLE tenant_registry ADD COLUMN lifecycle_epoch integer NOT NULL DEFAULT 1;
                 END IF;
                 IF NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid='onboarding_dispatches'::regclass AND attname='lease_fencing_token' AND NOT attisdropped) THEN
                   ALTER TABLE onboarding_dispatches ADD COLUMN lease_fencing_token integer NOT NULL DEFAULT 0;
                 END IF;
                 IF NOT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid='onboarding_dispatches'::regclass AND attname='execution_generation' AND NOT attisdropped) THEN
                   ALTER TABLE onboarding_dispatches ADD COLUMN execution_generation integer NOT NULL DEFAULT 1;
                 END IF;
                  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_background_jobs_tenant_id_id') THEN
                    ALTER TABLE background_jobs ADD CONSTRAINT uq_background_jobs_tenant_id_id UNIQUE (tenant_id, id);
                  END IF;
                  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_tenant_registry_lifecycle_epoch_positive') THEN
                   ALTER TABLE tenant_registry ADD CONSTRAINT ck_tenant_registry_lifecycle_epoch_positive CHECK (lifecycle_epoch >= 1);
                 END IF;
                 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_onboarding_dispatch_fencing_nonnegative') THEN
                   ALTER TABLE onboarding_dispatches ADD CONSTRAINT ck_onboarding_dispatch_fencing_nonnegative CHECK (lease_fencing_token >= 0);
                 END IF;
                 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_onboarding_dispatch_generation_positive') THEN
                   ALTER TABLE onboarding_dispatches ADD CONSTRAINT ck_onboarding_dispatch_generation_positive CHECK (execution_generation >= 1);
                 END IF;
                 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_onboarding_request_tenant_job') THEN
                   ALTER TABLE onboarding_requests ADD CONSTRAINT fk_onboarding_request_tenant_job
                     FOREIGN KEY (tenant_id, job_id) REFERENCES background_jobs (tenant_id, id) ON DELETE RESTRICT;
                 END IF;
                 IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_onboarding_dispatch_request_revision') THEN
                   ALTER TABLE onboarding_dispatches DROP CONSTRAINT uq_onboarding_dispatch_request_revision;
                 END IF;
                 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_onboarding_dispatch_request_generation') THEN
                   ALTER TABLE onboarding_dispatches ADD CONSTRAINT uq_onboarding_dispatch_request_generation
                     UNIQUE (tenant_id, request_id, revision, execution_generation);
                 END IF;
                 IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='onboarding_dispatches_job_id_fkey') THEN
                   ALTER TABLE onboarding_dispatches DROP CONSTRAINT onboarding_dispatches_job_id_fkey;
                 END IF;
                 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_onboarding_dispatch_tenant_job') THEN
                   ALTER TABLE onboarding_dispatches ADD CONSTRAINT fk_onboarding_dispatch_tenant_job
                     FOREIGN KEY (tenant_id, job_id) REFERENCES background_jobs (tenant_id, id) ON DELETE RESTRICT;
                 END IF;
               END $$"""
        )
    )

    op.execute(sa.text("ALTER TABLE onboarding_grants ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE onboarding_grants FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname='onboarding_grants_admin_manage') THEN "
            "CREATE POLICY onboarding_grants_admin_manage ON onboarding_grants "
            "FOR ALL TO iap_migrator USING (true) WITH CHECK (true); END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname='onboarding_grants_tenant_select') THEN "
            "CREATE POLICY onboarding_grants_tenant_select ON onboarding_grants "
            "FOR SELECT TO iap_app USING (tenant_id = current_setting('app.tenant_id', true)); END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname='onboarding_grants_admin_select') THEN CREATE POLICY onboarding_grants_admin_select ON onboarding_grants FOR SELECT TO iap_registry_admin USING (true); END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname='onboarding_grants_migrator_select') THEN CREATE POLICY onboarding_grants_migrator_select ON onboarding_grants FOR SELECT TO iap_migrator USING (true); END IF; END $$"
        )
    )

    op.execute(sa.text("ALTER TABLE onboarding_dispatch_audits ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE onboarding_dispatch_audits FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname='onboarding_dispatch_audit_tenant') THEN "
            "CREATE POLICY onboarding_dispatch_audit_tenant ON onboarding_dispatch_audits "
            "FOR ALL TO iap_app USING (tenant_id = current_setting('app.tenant_id', true)) "
            "WITH CHECK (tenant_id = current_setting('app.tenant_id', true)); END IF; END $$"
        )
    )
    op.execute(sa.text("ALTER TABLE onboarding_grant_audits ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE onboarding_grant_audits FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY onboarding_grant_audit_admin ON onboarding_grant_audits "
            "FOR ALL TO iap_migrator USING (true) WITH CHECK (true)"
        )
    )
    op.execute(
        sa.text(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname='onboarding_dispatch_audit_migrator_insert') THEN "
            "CREATE POLICY onboarding_dispatch_audit_migrator_insert ON onboarding_dispatch_audits "
            "FOR INSERT TO iap_migrator WITH CHECK (true); END IF; END $$"
        )
    )

    # Agent contract tables: support_registry is deployment-scoped (no tenant
    # column), readable by runtime roles, writable only by the migration role.
    # coverage_reports is tenant-scoped like the other request-linked tables.
    op.execute(sa.text("ALTER TABLE onboarding_support_registry ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE onboarding_support_registry FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname='onboarding_support_registry_read') THEN "
            "CREATE POLICY onboarding_support_registry_read ON onboarding_support_registry "
            "FOR SELECT TO iap_app, iap_worker, iap_registry_admin USING (true); END IF; END $$"
        )
    )
    op.execute(sa.text("ALTER TABLE onboarding_coverage_reports ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE onboarding_coverage_reports FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname='onboarding_coverage_report_tenant') THEN "
            "CREATE POLICY onboarding_coverage_report_tenant ON onboarding_coverage_reports "
            "FOR ALL USING (tenant_id = current_setting('app.tenant_id', true)) "
            "WITH CHECK (tenant_id = current_setting('app.tenant_id', true)); END IF; END $$"
        )
    )

    # Explicitly supersede revision 023's CURRENT_USER grant. Migration is run
    # as iap_migrator; the app/worker/admin roles receive only their reviewed
    # privileges. iap_worker gets global visibility on dispatches only.
    for table in (
        "tenant_registry",
        "onboarding_requests",
        "onboarding_dispatches",
        "onboarding_grants",
        "onboarding_dispatch_audits",
        "onboarding_grant_audits",
        "onboarding_support_registry",
        "onboarding_coverage_reports",
    ):
        op.execute(sa.text(f"REVOKE ALL ON TABLE {table} FROM PUBLIC"))
        op.execute(sa.text(f"REVOKE ALL ON TABLE {table} FROM iap_app"))
        op.execute(sa.text(f"REVOKE ALL ON TABLE {table} FROM iap_worker"))
        op.execute(sa.text(f"REVOKE ALL ON TABLE {table} FROM iap_registry_admin"))

    # Revision 023 granted to whichever CURRENT_USER ran that migration. That
    # historical principal is intentionally not assumed to be the new app
    # role, so clear direct ACL grants from every non-owner principal on these
    # dedicated onboarding tables before issuing the reviewed role matrix.
    op.execute(
        sa.text(
            """DO $$ DECLARE rel record; acl record; BEGIN
                 FOR rel IN SELECT c.relname, c.relowner, c.relacl
                            FROM pg_class c
                            JOIN pg_namespace n ON n.oid=c.relnamespace
                            WHERE n.nspname=current_schema()
                              AND c.relname IN ('tenant_registry', 'onboarding_requests',
                                                'onboarding_dispatches', 'onboarding_grants',
                                                'onboarding_dispatch_audits', 'onboarding_grant_audits',
                                                'onboarding_support_registry', 'onboarding_coverage_reports')
                              AND c.relkind='r'
                 LOOP
                   FOR acl IN SELECT DISTINCT x.grantee, r.rolname
                              FROM aclexplode(COALESCE(rel.relacl, acldefault('r', rel.relowner))) x
                              LEFT JOIN pg_roles r ON r.oid=x.grantee
                              WHERE x.grantee <> rel.relowner
                   LOOP
                     IF acl.grantee = 0 THEN
                       EXECUTE format('REVOKE ALL ON TABLE %I FROM PUBLIC', rel.relname);
                     ELSIF acl.rolname IS NOT NULL THEN
                       EXECUTE format('REVOKE ALL ON TABLE %I FROM %I CASCADE', rel.relname, acl.rolname);
                     END IF;
                   END LOOP;
                 END LOOP;
               END $$"""
        )
    )
    op.execute(sa.text("REVOKE ALL ON TABLE onboarding_grants FROM iap_app"))
    op.execute(sa.text("REVOKE ALL ON TABLE onboarding_dispatch_audits FROM iap_app"))
    op.execute(sa.text("REVOKE ALL ON TABLE onboarding_grants FROM PUBLIC"))
    op.execute(sa.text("REVOKE ALL ON TABLE onboarding_dispatch_audits FROM PUBLIC"))

    op.execute(
        sa.text(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_registry, onboarding_requests, onboarding_dispatches TO iap_app"
        )
    )
    op.execute(sa.text("GRANT SELECT ON onboarding_grants, onboarding_dispatch_audits TO iap_app"))
    op.execute(sa.text("GRANT SELECT, INSERT ON tenant_registry TO iap_registry_admin"))
    op.execute(
        sa.text(
            "GRANT UPDATE (display_slug, owner, status, updated_at) ON tenant_registry TO iap_registry_admin"
        )
    )
    op.execute(sa.text("GRANT SELECT ON onboarding_grants TO iap_registry_admin"))
    op.execute(sa.text("GRANT SELECT ON onboarding_grants TO iap_app"))
    op.execute(sa.text("GRANT SELECT ON onboarding_dispatch_audits TO iap_app"))
    op.execute(sa.text("GRANT INSERT ON onboarding_dispatch_audits TO iap_migrator"))
    op.execute(sa.text("GRANT SELECT, INSERT, UPDATE ON onboarding_grants TO iap_migrator"))
    op.execute(sa.text("GRANT SELECT, INSERT ON onboarding_grant_audits TO iap_migrator"))
    op.execute(
        sa.text(
            "GRANT SELECT ON onboarding_support_registry TO iap_app, iap_worker, iap_registry_admin"
        )
    )
    op.execute(
        sa.text(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON onboarding_support_registry TO iap_migrator"
        )
    )
    op.execute(sa.text("GRANT SELECT, INSERT ON onboarding_coverage_reports TO iap_app"))
    op.execute(sa.text("GRANT SELECT ON onboarding_coverage_reports TO iap_worker"))
    op.execute(sa.text("GRANT SELECT ON onboarding_coverage_reports TO iap_registry_admin"))
    op.execute(
        sa.text(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON onboarding_coverage_reports TO iap_migrator"
        )
    )
    op.execute(
        sa.text(
            "GRANT SELECT (id, tenant_id, event_id, request_id, revision, job_id, workflow_id, "
            "schema_version, execution_generation, state, attempts, next_attempt_at, lease_owner, "
            "lease_fencing_token, lease_expires_at, temporal_run_id, last_error_code, created_at, updated_at) "
            "ON onboarding_dispatches TO iap_worker"
        )
    )
    op.execute(sa.text("GRANT SELECT ON onboarding_requests TO iap_worker"))
    op.execute(
        sa.text(
            "GRANT UPDATE (state, lease_owner, lease_fencing_token, lease_expires_at, attempts, "
            "next_attempt_at, temporal_run_id, last_error_code, updated_at) "
            "ON onboarding_dispatches TO iap_worker"
        )
    )
    op.execute(
        sa.text(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname='onboarding_registry_admin') THEN CREATE POLICY onboarding_registry_admin ON tenant_registry FOR ALL TO iap_registry_admin USING (true) WITH CHECK (true); END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE policyname='onboarding_dispatch_worker_select') THEN CREATE POLICY onboarding_dispatch_worker_select ON onboarding_dispatches FOR SELECT TO iap_worker USING (true); END IF; END $$"
        )
    )
    op.execute(
        sa.text(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_policies "
            "WHERE policyname='onboarding_dispatch_worker_update') THEN "
            "CREATE POLICY onboarding_dispatch_worker_update ON onboarding_dispatches "
            "FOR UPDATE TO iap_worker USING (true) WITH CHECK "
            "(lease_owner IS NULL OR lease_owner = current_setting('iap.worker_id', true)); "
            "END IF; END $$"
        )
    )

    # Audits are inserted only by this constrained function. Identity is taken
    # from PostgreSQL session_user, not an application-supplied worker label.
    op.execute(
        sa.text(
            """CREATE OR REPLACE FUNCTION record_onboarding_dispatch_audit(
                   p_tenant_id text, p_dispatch_id uuid, p_action text, p_fencing_token integer
               ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
               SET search_path = pg_catalog, public AS $$
               BEGIN
                   IF p_action NOT IN ('CLAIM', 'RENEW', 'RELEASE', 'STALE_FENCE') THEN
                       RAISE EXCEPTION 'invalid onboarding dispatch audit action';
                   END IF;
                   IF p_tenant_id IS DISTINCT FROM current_setting('app.tenant_id', true) THEN
                       RAISE EXCEPTION 'onboarding audit tenant context mismatch';
                   END IF;
                   INSERT INTO public.onboarding_dispatch_audits
                       (id, tenant_id, dispatch_id, worker_id, database_role, action, fencing_token)
                   VALUES
                       (gen_random_uuid(), p_tenant_id, p_dispatch_id, current_setting('iap.worker_id', true), session_user::text,
                        p_action, p_fencing_token);
               END; $$"""
        )
    )
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION record_onboarding_dispatch_audit(text, uuid, text, integer) FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            "GRANT EXECUTE ON FUNCTION record_onboarding_dispatch_audit(text, uuid, text, integer) TO iap_worker"
        )
    )

    op.execute(
        sa.text(
            """CREATE OR REPLACE FUNCTION onboarding_has_platform_grant(
                   p_principal_id text, p_capability text, p_application_id text,
                   p_environment text, p_operation text, p_policy_version text, p_at timestamptz
               ) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
               SET search_path = pg_catalog, public AS $$
               SELECT EXISTS (
                   SELECT 1 FROM public.onboarding_grants g
                   WHERE g.principal_id = p_principal_id
                     AND g.capability = p_capability
                     AND g.tenant_id = '__platform__'
                     AND g.application_id = p_application_id
                     AND g.environment = p_environment
                     AND g.operation = p_operation
                     AND g.policy_version = p_policy_version
                     AND g.revoked_at IS NULL
                     AND g.valid_from <= p_at AND p_at < g.expires_at
               )
               $$"""
        )
    )
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION onboarding_has_platform_grant(text, text, text, text, text, text, timestamptz) FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            "GRANT EXECUTE ON FUNCTION onboarding_has_platform_grant(text, text, text, text, text, text, timestamptz) TO iap_app"
        )
    )

    # Raw worker UPDATE statements require DB-layer timestamp maintenance.
    op.execute(
        sa.text(
            """CREATE OR REPLACE FUNCTION onboarding_set_updated_at() RETURNS trigger AS $$
               BEGIN NEW.updated_at = clock_timestamp(); RETURN NEW; END; $$ LANGUAGE plpgsql"""
        )
    )
    for table in (
        "tenant_registry",
        "onboarding_requests",
        "onboarding_dispatches",
        "onboarding_support_registry",
        "onboarding_coverage_reports",
    ):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {table}_set_updated_at ON {table}"))
        op.execute(
            sa.text(
                f"CREATE TRIGGER {table}_set_updated_at BEFORE UPDATE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION onboarding_set_updated_at()"
            )
        )


def downgrade() -> None:
    # Preserve durable intent, grants, fences, and audit history. Disable
    # feature routes/workers and roll forward instead of destructive downgrade.
    return None
