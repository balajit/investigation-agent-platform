"""Alembic migration: R1 policy split for knowledge_artifacts (requires owner approval).

- One-time data step (explicitly approved): existing SHARED rows embed
  summaries, so demote them to TENANT.
- Replace the single FOR ALL `knowledge_visibility` policy with a
  tenant-only FOR ALL policy plus a cross-tenant FOR SELECT policy gated
  on the transaction-local proven fingerprint list.
- Shared rows carry no evidence or log references (mirrors the validator).
"""

# revision identifiers, used by Alembic.
revision = "039_knowledge_shared_split"
down_revision = "038_conclusion_s7_handoff"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # type: ignore[import-not-found]
from alembic import op  # type: ignore[import-not-found]


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE knowledge_artifacts SET visibility = 'TENANT' "
            "WHERE visibility = 'SHARED_CODE_ISSUE'"
        )
    )
    op.execute(sa.text("DROP POLICY IF EXISTS knowledge_visibility ON knowledge_artifacts"))
    op.execute(
        sa.text(
            """CREATE POLICY knowledge_tenant_all ON knowledge_artifacts
            FOR ALL
            USING (tenant_id = current_setting('app.tenant_id', true))
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"""
        )
    )
    op.execute(
        sa.text(
            """CREATE POLICY knowledge_shared_read ON knowledge_artifacts
            FOR SELECT
            USING (
                visibility = 'SHARED_CODE_ISSUE'
                AND status = 'ACTIVE'
                AND code_issue_fingerprint = ANY (
                    string_to_array(coalesce(current_setting('app.shared_fingerprints', true), ''), ',')
                )
            )"""
        )
    )
    op.execute(
        sa.text(
            """ALTER TABLE knowledge_artifacts ADD CONSTRAINT ck_knowledge_shared_projection
            CHECK (visibility <> 'SHARED_CODE_ISSUE'
                OR (source_evidence_ids = '[]'::jsonb AND source_log_refs = '[]'::jsonb
                    AND kind = 'error_signature'))"""
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "ALTER TABLE knowledge_artifacts DROP CONSTRAINT IF EXISTS "
            "ck_knowledge_shared_projection"
        )
    )
    op.execute(sa.text("DROP POLICY IF EXISTS knowledge_shared_read ON knowledge_artifacts"))
    op.execute(sa.text("DROP POLICY IF EXISTS knowledge_tenant_all ON knowledge_artifacts"))
    op.execute(
        sa.text(
            """CREATE POLICY knowledge_visibility ON knowledge_artifacts
            USING (tenant_id = current_setting('app.tenant_id', true)
                OR visibility = 'SHARED_CODE_ISSUE')
            WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"""
        )
    )
