# migrations/versions/029_onboarding_probe_readiness.py
"""Add probe evidence and readiness report tables (Phase 3).

Probe evidences pin one adapter outcome to an exact request revision, scope,
and policy/catalog version; readiness reports record the tier decision over
those pins. Both follow the 025/026 access shape: tenant RLS with app SELECT,
migrator INSERT, and worker INSERT (filing runs under workflow identity);
advisory-only enforcement is application logic, not a grant.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "029_onboarding_probe_readiness"
down_revision = "028_onboarding_request_cancel"
branch_labels = None
depends_on = None


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    try:
        return sa.inspect(bind).has_table(table)
    except Exception:
        # Offline (--sql) mode: report missing so DDL is emitted.
        return False


def upgrade() -> None:
    # Fresh-install guard: the 000 baseline creates the full current schema,
    # so these tables may already exist. RLS, policies, and grants below are
    # NOT part of the baseline and always run.
    if not _table_exists("onboarding_probe_evidences"):
        op.create_table(
            "onboarding_probe_evidences",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("application_id", sa.String(128), nullable=False),
            sa.Column("environment", sa.String(64), nullable=False),
            sa.Column("policy_version", sa.String(32), nullable=False),
            sa.Column("catalog_version", sa.String(32), nullable=False),
            sa.Column("capability_key", sa.String(128), nullable=False),
            sa.Column("adapter", sa.String(128), nullable=False),
            sa.Column("adapter_version", sa.String(32), nullable=False),
            sa.Column("outcome", sa.String(32), nullable=False),
            sa.Column("diagnostic_code", sa.String(64), nullable=False),
            sa.Column("detail", sa.String(1024), nullable=False, server_default=""),
            sa.Column("auth_mode", sa.String(64), nullable=False),
            sa.Column("probed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["tenant_id", "request_id", "revision"],
                [
                    "onboarding_requests.tenant_id",
                    "onboarding_requests.request_id",
                    "onboarding_requests.revision",
                ],
                ondelete="RESTRICT",
                name="fk_probe_evidence_request_revision",
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "request_id",
                "revision",
                "capability_key",
                name="uq_probe_evidence_request_capability",
            ),
            sa.CheckConstraint("revision >= 1", name="ck_probe_evidence_revision_positive"),
            sa.CheckConstraint(
                "outcome IN ('VERIFIED', 'DEGRADED', 'FAILED', 'UNSUPPORTED', 'SKIPPED', 'NOT_CONFIGURED')",
                name="ck_probe_evidence_outcome",
            ),
            sa.CheckConstraint(
                "expires_at > probed_at", name="ck_probe_evidence_expiry_after_probe"
            ),
        )
        op.create_index(
            "ix_probe_evidence_request",
            "onboarding_probe_evidences",
            ["tenant_id", "request_id", "revision"],
        )
    if not _table_exists("onboarding_readiness_reports"):
        op.create_table(
            "onboarding_readiness_reports",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("tier", sa.String(32), nullable=False),
            sa.Column("policy_version", sa.String(32), nullable=False),
            sa.Column("catalog_version", sa.String(32), nullable=False),
            sa.Column("decision", sa.String(16), nullable=False),
            sa.Column("blocking_codes", postgresql.JSONB(), nullable=False, server_default="[]"),
            sa.Column("report_json", postgresql.JSONB(), nullable=False),
            sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["tenant_id", "request_id", "revision"],
                [
                    "onboarding_requests.tenant_id",
                    "onboarding_requests.request_id",
                    "onboarding_requests.revision",
                ],
                ondelete="RESTRICT",
                name="fk_readiness_report_request_revision",
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "request_id",
                "revision",
                "tier",
                name="uq_readiness_report_request_tier",
            ),
            sa.CheckConstraint("revision >= 1", name="ck_readiness_report_revision_positive"),
            sa.CheckConstraint(
                "decision IN ('READY', 'BLOCKED')",
                name="ck_readiness_report_decision",
            ),
        )
        op.create_index(
            "ix_readiness_report_request",
            "onboarding_readiness_reports",
            ["tenant_id", "request_id", "revision"],
        )

    for table in ("onboarding_probe_evidences", "onboarding_readiness_reports"):
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY onboarding_probe_evidence_tenant_read ON onboarding_probe_evidences "
            "FOR SELECT TO iap_app USING (tenant_id = current_setting('app.tenant_id', true))"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY onboarding_probe_evidence_migrator_insert ON onboarding_probe_evidences "
            "FOR INSERT TO iap_migrator WITH CHECK (true)"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY onboarding_probe_evidence_worker_insert ON onboarding_probe_evidences "
            "FOR INSERT TO iap_worker WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY onboarding_probe_evidence_worker_select ON onboarding_probe_evidences "
            "FOR SELECT TO iap_worker USING (tenant_id = current_setting('app.tenant_id', true))"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY onboarding_readiness_report_tenant_read ON onboarding_readiness_reports "
            "FOR SELECT TO iap_app USING (tenant_id = current_setting('app.tenant_id', true))"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY onboarding_readiness_report_migrator_insert ON onboarding_readiness_reports "
            "FOR INSERT TO iap_migrator WITH CHECK (true)"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY onboarding_readiness_report_worker_insert ON onboarding_readiness_reports "
            "FOR INSERT TO iap_worker WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY onboarding_readiness_report_worker_select ON onboarding_readiness_reports "
            "FOR SELECT TO iap_worker USING (tenant_id = current_setting('app.tenant_id', true))"
        )
    )
    op.execute(sa.text("GRANT SELECT ON onboarding_probe_evidences TO iap_app, iap_migrator"))
    op.execute(sa.text("GRANT INSERT ON onboarding_probe_evidences TO iap_migrator"))
    op.execute(sa.text("GRANT SELECT, INSERT ON onboarding_probe_evidences TO iap_worker"))
    op.execute(sa.text("GRANT SELECT ON onboarding_readiness_reports TO iap_app, iap_migrator"))
    op.execute(sa.text("GRANT INSERT ON onboarding_readiness_reports TO iap_migrator"))
    op.execute(sa.text("GRANT SELECT, INSERT ON onboarding_readiness_reports TO iap_worker"))


def downgrade() -> None:
    # Preserve probe and readiness history; rollback disables filing use.
    return None
