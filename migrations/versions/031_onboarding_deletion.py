# migrations/versions/031_onboarding_deletion.py
"""Add deletion store registry, manifests, approvals, and outcomes (Phase 5).

The deployment store registry declares required cleanup adapters per
registry version; manifests are immutable approval plans; approvals persist
only token/payload digests with atomic one-time consumption; outcomes are
append-only per-resource records. Tenant RLS with app SELECT, migrator
INSERT, and worker INSERT mirrors the 025/029 access shape; purge execution
writes outcomes under workflow authority.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "031_onboarding_deletion"
down_revision = "030_onboarding_request_activate"
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
    if not _table_exists("onboarding_store_registry"):
        op.create_table(
            "onboarding_store_registry",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("deployment_id", sa.String(128), nullable=False),
            sa.Column("store_id", sa.String(128), nullable=False),
            sa.Column("store_type", sa.String(64), nullable=False),
            sa.Column("adapter_version", sa.String(32), nullable=False),
            sa.Column("registry_version", sa.String(32), nullable=False),
            sa.Column("required_for_deletion", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
            sa.Column("capabilities_json", postgresql.JSONB(), nullable=False, server_default="{}"),
            sa.UniqueConstraint(
                "deployment_id",
                "store_id",
                "registry_version",
                name="uq_store_registry_version",
            ),
        )
        op.create_index(
            "ix_store_registry_deployment",
            "onboarding_store_registry",
            ["deployment_id", "enabled"],
        )
    if not _table_exists("onboarding_deletion_manifests"):
        op.create_table(
            "onboarding_deletion_manifests",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("manifest_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("application_id", sa.String(128), nullable=True),
            sa.Column("environment", sa.String(64), nullable=False),
            sa.Column("plan_version", sa.Integer(), nullable=False),
            sa.Column("store_registry_version", sa.String(32), nullable=False),
            sa.Column("policy_version", sa.String(32), nullable=False),
            sa.Column("manifest_json", postgresql.JSONB(), nullable=False),
            sa.Column("plan_hash", sa.String(64), nullable=False),
            sa.Column("state", sa.String(32), nullable=False),
            sa.Column("requester_id", sa.String(256), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint("tenant_id", "manifest_id", name="uq_onboarding_manifest"),
            sa.CheckConstraint(
                "state IN ('PREVIEW', 'APPROVAL_PENDING', 'QUEUED', 'IN_PROGRESS', 'COMPLETE', "
                "'COMPLETE_WITH_RETENTION', 'COMPLETE_WITH_APPROVED_RESIDUALS', 'INCOMPLETE', "
                "'BLOCKED', 'CANCELED')",
                name="ck_deletion_manifest_state",
            ),
            sa.CheckConstraint("plan_version >= 1", name="ck_deletion_manifest_plan_positive"),
        )
        op.create_index(
            "ix_onboarding_manifest_state",
            "onboarding_deletion_manifests",
            ["tenant_id", "state"],
        )
    if not _table_exists("onboarding_deletion_approvals"):
        op.create_table(
            "onboarding_deletion_approvals",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("manifest_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("requester_id", sa.String(256), nullable=False),
            sa.Column("approver_id", sa.String(256), nullable=False),
            sa.Column("manifest_hash", sa.String(64), nullable=False),
            sa.Column("token_digest", sa.String(64), nullable=False),
            sa.Column("payload_digest", sa.String(64), nullable=False),
            sa.Column("policy_version", sa.String(32), nullable=False),
            sa.Column("store_registry_version", sa.String(32), nullable=False),
            sa.Column("nonce", sa.String(128), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("purge_job_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("dispatch_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "tenant_id", "manifest_id", "nonce", name="uq_onboarding_approval_nonce"
            ),
        )
        op.create_index(
            "ix_onboarding_approval_manifest",
            "onboarding_deletion_approvals",
            ["tenant_id", "manifest_id", "consumed_at"],
        )
    if not _table_exists("onboarding_deletion_outcomes"):
        op.create_table(
            "onboarding_deletion_outcomes",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("manifest_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("entry_key", sa.String(1024), nullable=False),
            sa.Column("store_id", sa.String(128), nullable=False),
            sa.Column("action", sa.String(24), nullable=False),
            sa.Column("state", sa.String(32), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column("completion_evidence_ref", sa.String(512), nullable=True),
            sa.Column("residual_code", sa.String(64), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "manifest_id",
                "entry_key",
                "attempt",
                name="uq_deletion_outcome_attempt",
            ),
            sa.CheckConstraint("attempt >= 1", name="ck_deletion_outcome_attempt_positive"),
            sa.CheckConstraint(
                "state IN ('DONE', 'RETAINED_BY_POLICY', 'APPROVED_RESIDUAL', 'INCOMPLETE', "
                "'BLOCKED', 'FAILED')",
                name="ck_deletion_outcome_state",
            ),
        )
        op.create_index(
            "ix_deletion_outcome_manifest",
            "onboarding_deletion_outcomes",
            ["tenant_id", "manifest_id", "state"],
        )

    for table in (
        "onboarding_store_registry",
        "onboarding_deletion_manifests",
        "onboarding_deletion_approvals",
        "onboarding_deletion_outcomes",
    ):
        op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    # Deployment registry is operator-managed: migrator writes, app reads.
    op.execute(
        sa.text(
            "CREATE POLICY onboarding_store_registry_read ON onboarding_store_registry "
            "FOR SELECT TO iap_app USING (true)"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY onboarding_store_registry_migrator_write ON onboarding_store_registry "
            "FOR INSERT TO iap_migrator WITH CHECK (true)"
        )
    )
    # Manifests/approvals/outcomes are tenant-scoped; purge execution writes
    # outcomes under workflow (worker) identity.
    for table in (
        "onboarding_deletion_manifests",
        "onboarding_deletion_approvals",
        "onboarding_deletion_outcomes",
    ):
        op.execute(
            sa.text(
                f"CREATE POLICY {table}_tenant_read ON {table} "
                f"FOR SELECT TO iap_app USING (tenant_id = current_setting('app.tenant_id', true))"
            )
        )
        op.execute(
            sa.text(
                f"CREATE POLICY {table}_migrator_insert ON {table} "
                f"FOR INSERT TO iap_migrator WITH CHECK (true)"
            )
        )
        op.execute(
            sa.text(
                f"CREATE POLICY {table}_worker_insert ON {table} "
                f"FOR INSERT TO iap_worker WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
            )
        )
        op.execute(sa.text(f"GRANT SELECT ON {table} TO iap_app, iap_migrator"))
        op.execute(sa.text(f"GRANT INSERT ON {table} TO iap_migrator, iap_worker"))
    op.execute(sa.text("GRANT SELECT ON onboarding_store_registry TO iap_app, iap_migrator"))
    op.execute(sa.text("GRANT INSERT ON onboarding_store_registry TO iap_migrator"))
    op.execute(
        sa.text(
            "CREATE POLICY onboarding_deletion_outcomes_worker_select ON onboarding_deletion_outcomes "
            "FOR SELECT TO iap_worker USING (tenant_id = current_setting('app.tenant_id', true))"
        )
    )
    op.execute(sa.text("GRANT SELECT ON onboarding_deletion_outcomes TO iap_worker"))
    op.execute(
        sa.text(
            "CREATE POLICY onboarding_deletion_manifests_worker_select ON onboarding_deletion_manifests "
            "FOR SELECT TO iap_worker USING (tenant_id = current_setting('app.tenant_id', true))"
        )
    )
    op.execute(sa.text("GRANT SELECT ON onboarding_deletion_manifests TO iap_worker"))


def downgrade() -> None:
    # Preserve registry, manifests, approvals, and outcomes; rollback disables
    # cleanup use by application policy, never by deleting the audit trail.
    return None
