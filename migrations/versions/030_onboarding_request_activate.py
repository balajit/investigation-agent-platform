# migrations/versions/030_onboarding_request_activate.py
"""Extend request audit with the Phase 4 activation event.

Mirrors 027 (acceptance): widen the action CHECK to
('CREATE', 'ACCEPT', 'ACTIVATE') and replace the definer function with the
matching whitelist. No new tables, columns, or grants; approval of the exact
request revision stays the caller's responsibility, recorded here as an
append-only audit row.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "030_onboarding_request_activate"
down_revision = "029_onboarding_probe_readiness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_onboarding_request_audit_action",
        "onboarding_request_audits",
        type_="check",
    )
    op.create_check_constraint(
        "ck_onboarding_request_audit_action",
        "onboarding_request_audits",
        "action IN ('CREATE', 'ACCEPT', 'ACTIVATE')",
    )
    op.execute(
        sa.text(
            "CREATE OR REPLACE FUNCTION record_onboarding_request_audit("
            "p_tenant_id text, p_request_id uuid, p_revision integer, p_actor_sub text, "
            "p_action text, p_content_hash text, p_idempotency_key text) RETURNS void "
            "LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$ "
            "BEGIN "
            "IF p_tenant_id IS DISTINCT FROM current_setting('app.tenant_id', true) THEN "
            "RAISE EXCEPTION 'onboarding request audit tenant context mismatch'; END IF; "
            "IF p_action NOT IN ('CREATE', 'ACCEPT', 'ACTIVATE') THEN "
            "RAISE EXCEPTION 'invalid onboarding request audit action'; END IF; "
            "IF p_actor_sub IS NULL OR length(p_actor_sub) = 0 THEN "
            "RAISE EXCEPTION 'verified onboarding actor sub is required'; END IF; "
            "INSERT INTO public.onboarding_request_audits "
            "(tenant_id, request_id, revision, actor_sub, database_role, action, content_hash, idempotency_key) "
            "VALUES (p_tenant_id, p_request_id, p_revision, p_actor_sub, session_user::text, "
            "p_action, p_content_hash, p_idempotency_key); END; $$"
        )
    )


def downgrade() -> None:
    # Preserve audit history. Roll back activation use by application policy;
    # never remove or rewrite request audit rows.
    return None
