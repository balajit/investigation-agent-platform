"""Add append-only audit linkage for tenant onboarding DRAFT requests."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "026_onboarding_request_audit"
down_revision = "025_onboarding_coverage_filing"
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
    # Fresh-install guard (chain-rot repair): the 000 baseline creates the
    # full current schema, so this audit table may already exist. The RLS,
    # policy, grant, and function statements below are NOT part of the
    # baseline and always run; only the table + index creation is skipped.
    # The CHECK created here is the CREATE-only form; migration 027 extends
    # it to ('CREATE', 'ACCEPT') on both paths.
    if not _table_exists("onboarding_request_audits"):
        op.create_table(
            "onboarding_request_audits",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=True),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("actor_sub", sa.String(256), nullable=False),
            sa.Column("database_role", sa.String(128), nullable=False),
            sa.Column("action", sa.String(16), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("idempotency_key", sa.String(256), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.ForeignKeyConstraint(
                ["tenant_id", "request_id", "revision"],
                [
                    "onboarding_requests.tenant_id",
                    "onboarding_requests.request_id",
                    "onboarding_requests.revision",
                ],
                ondelete="RESTRICT",
                name="fk_onboarding_request_audit_revision",
            ),
            sa.CheckConstraint("action IN ('CREATE')", name="ck_onboarding_request_audit_action"),
            sa.CheckConstraint(
                "revision >= 1", name="ck_onboarding_request_audit_revision_positive"
            ),
            sa.CheckConstraint(
                "length(actor_sub) > 0", name="ck_onboarding_request_audit_actor_nonempty"
            ),
            sa.CheckConstraint(
                "length(database_role) > 0", name="ck_onboarding_request_audit_role_nonempty"
            ),
        )
        op.create_index(
            "ix_onboarding_request_audit_revision",
            "onboarding_request_audits",
            ["tenant_id", "request_id", "revision"],
        )

    op.execute(sa.text("ALTER TABLE onboarding_request_audits ENABLE ROW LEVEL SECURITY"))
    op.execute(sa.text("ALTER TABLE onboarding_request_audits FORCE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            "CREATE POLICY onboarding_request_audit_tenant_read ON onboarding_request_audits "
            "FOR SELECT TO iap_app USING (tenant_id = current_setting('app.tenant_id', true))"
        )
    )
    op.execute(
        sa.text(
            "CREATE POLICY onboarding_request_audit_migrator_insert ON onboarding_request_audits "
            "FOR INSERT TO iap_migrator WITH CHECK (true)"
        )
    )
    op.execute(sa.text("GRANT SELECT ON onboarding_request_audits TO iap_app, iap_migrator"))
    op.execute(sa.text("GRANT INSERT ON onboarding_request_audits TO iap_migrator"))

    op.execute(
        sa.text(
            "CREATE OR REPLACE FUNCTION record_onboarding_request_audit("
            "p_tenant_id text, p_request_id uuid, p_revision integer, p_actor_sub text, "
            "p_action text, p_content_hash text, p_idempotency_key text) RETURNS void "
            "LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$ "
            "BEGIN "
            "IF p_tenant_id IS DISTINCT FROM current_setting('app.tenant_id', true) THEN "
            "RAISE EXCEPTION 'onboarding request audit tenant context mismatch'; END IF; "
            "IF p_action <> 'CREATE' THEN RAISE EXCEPTION 'invalid onboarding request audit action'; END IF; "
            "IF p_actor_sub IS NULL OR length(p_actor_sub) = 0 THEN "
            "RAISE EXCEPTION 'verified onboarding actor sub is required'; END IF; "
            "INSERT INTO public.onboarding_request_audits "
            "(tenant_id, request_id, revision, actor_sub, database_role, action, content_hash, idempotency_key) "
            "VALUES (p_tenant_id, p_request_id, p_revision, p_actor_sub, session_user::text, "
            "'CREATE', p_content_hash, p_idempotency_key); END; $$"
        )
    )
    op.execute(
        sa.text(
            "REVOKE ALL ON FUNCTION record_onboarding_request_audit(text, uuid, integer, text, text, text, text) FROM PUBLIC"
        )
    )
    op.execute(
        sa.text(
            "GRANT EXECUTE ON FUNCTION record_onboarding_request_audit(text, uuid, integer, text, text, text, text) TO iap_app"
        )
    )


def downgrade() -> None:
    # Preserve durable request and audit history; disable feature use to roll back.
    return None
