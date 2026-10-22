"""Add tenant-scoped coverage report filing audit and worker write path."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "025_onboarding_coverage_filing"
down_revision = "024_onboarding_phase0r"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "onboarding_coverage_filing_audits",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=False),
        sa.Column("database_role", sa.String(128), nullable=False),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("report_content_hash", sa.String(64), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["tenant_id", "request_id", "revision", "generation"],
            [
                "onboarding_coverage_reports.tenant_id",
                "onboarding_coverage_reports.request_id",
                "onboarding_coverage_reports.revision",
                "onboarding_coverage_reports.generation",
            ],
            ondelete="RESTRICT",
            name="fk_coverage_filing_audit_report",
        ),
        sa.CheckConstraint("action IN ('FILED')", name="ck_coverage_filing_audit_action"),
        sa.CheckConstraint("length(worker_id) > 0", name="ck_coverage_filing_audit_worker_nonempty"),
        sa.CheckConstraint("length(database_role) > 0", name="ck_coverage_filing_audit_role_nonempty"),
        sa.CheckConstraint("fencing_token >= 0", name="ck_coverage_filing_audit_token_nonnegative"),
        sa.CheckConstraint("revision >= 1", name="ck_coverage_filing_audit_revision_positive"),
        sa.CheckConstraint("generation >= 1", name="ck_coverage_filing_audit_generation_positive"),
    )
    op.create_index(
        "ix_coverage_filing_audit_report",
        "onboarding_coverage_filing_audits",
        ["tenant_id", "request_id", "revision", "generation", "occurred_at"],
    )

    op.execute(sa.text("ALTER TABLE onboarding_coverage_filing_audits ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE onboarding_coverage_filing_audits FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY onboarding_coverage_filing_audit_tenant ON onboarding_coverage_filing_audits "
            "FOR SELECT TO iap_app USING (tenant_id = current_setting('app.tenant_id', true))"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY onboarding_coverage_filing_audit_migrator_insert "
            "ON onboarding_coverage_filing_audits FOR INSERT TO iap_migrator WITH CHECK (true)"
        )
    )

    op.execute(sa.text("REVOKE ALL ON TABLE onboarding_coverage_reports FROM iap_worker"))
    op.execute(sa.text("GRANT SELECT, INSERT ON onboarding_coverage_reports TO iap_worker"))
    op.execute(
        sa.text(
            "CREATE POLICY onboarding_coverage_report_worker_insert ON onboarding_coverage_reports "
            "FOR INSERT TO iap_worker WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
        )
    )
    op.execute(
        sa.text(
            "GRANT SELECT ON onboarding_coverage_filing_audits TO iap_app, iap_migrator"
        )
    )
    op.execute(
        sa.text("GRANT INSERT ON onboarding_coverage_filing_audits TO iap_migrator")
    )
    op.execute(
        sa.text(
            "CREATE OR REPLACE FUNCTION record_onboarding_coverage_filing("
            "p_tenant_id text, p_request_id uuid, p_revision integer, p_generation integer, "
            "p_fencing_token integer, p_report_content_hash text) RETURNS void "
            "LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$ "
            "BEGIN "
            "IF p_tenant_id IS DISTINCT FROM current_setting('app.tenant_id', true) THEN "
            "RAISE EXCEPTION 'coverage filing audit tenant context mismatch'; END IF; "
            "IF current_setting('iap.worker_id', true) IS NULL OR "
            "length(current_setting('iap.worker_id', true)) = 0 THEN "
            "RAISE EXCEPTION 'coverage filing worker identity is required'; END IF; "
            "IF p_fencing_token < 0 THEN RAISE EXCEPTION 'invalid coverage filing fencing token'; END IF; "
            "IF NOT EXISTS (SELECT 1 FROM public.onboarding_coverage_reports r "
            "WHERE r.tenant_id = p_tenant_id AND r.request_id = p_request_id "
            "AND r.revision = p_revision AND r.generation = p_generation "
            "AND r.report_content_hash = p_report_content_hash) THEN "
            "RAISE EXCEPTION 'coverage filing audit report binding mismatch'; END IF; "
            "INSERT INTO public.onboarding_coverage_filing_audits "
            "(id, tenant_id, request_id, revision, generation, worker_id, database_role, "
            "fencing_token, report_content_hash, action) VALUES "
            "(gen_random_uuid(), p_tenant_id, p_request_id, p_revision, p_generation, "
            "current_setting('iap.worker_id', true), session_user::text, p_fencing_token, "
            "p_report_content_hash, 'FILED'); END; $$"
        )
    )
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION record_onboarding_coverage_filing(text, uuid, integer, integer, integer, text) FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            "GRANT EXECUTE ON FUNCTION record_onboarding_coverage_filing(text, uuid, integer, integer, integer, text) TO iap_worker"
        )
    )


def downgrade() -> None:
    # Preserve report and audit history; rollback by disabling the filing path.
    return None
