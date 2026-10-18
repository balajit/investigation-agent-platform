"""Alembic migration: profile v2 read-optimization columns.

Adds ``status`` / ``domain`` / ``planes`` columns to ``application_profiles``
mirroring the canonical profile JSON (JSON stays authoritative on conflict),
backfills them from ``profile_json``, and adds lookup indexes:

- ``idx_app_profile_json_gin`` — service/datasource lookups without full scans
- ``idx_app_profile_tenant_status`` — list filtering + ACTIVE gate
- ``idx_app_profile_tenant_domain`` — domain reuse scoping
"""

# revision identifiers, used by Alembic.
revision = "016_profile_v2_columns"
down_revision = "015_batch_intake"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # type: ignore[import-not-found]
from alembic import op  # type: ignore[import-not-found]
from sqlalchemy.dialects import postgresql  # type: ignore[import-not-found]


def upgrade() -> None:
    op.add_column(
        "application_profiles",
        sa.Column("status", sa.String(32), nullable=False, server_default="ACTIVE"),
    )
    op.add_column("application_profiles", sa.Column("domain", sa.String(128), nullable=True))
    op.add_column(
        "application_profiles",
        sa.Column("planes", postgresql.ARRAY(sa.String(32)), nullable=True),
    )
    # Backfill mirrored columns from the authoritative profile_json.
    op.execute(
        "UPDATE application_profiles "
        "SET status = COALESCE(profile_json->>'status', 'ACTIVE'), "
        "domain = profile_json->>'domain', "
        "planes = ARRAY(SELECT jsonb_array_elements_text("
        "COALESCE(profile_json->'planes', '[]'::jsonb)))"
    )
    op.alter_column("application_profiles", "status", server_default=None)
    op.create_index(
        "idx_app_profile_json_gin",
        "application_profiles",
        ["profile_json"],
        postgresql_using="gin",
        postgresql_ops={"profile_json": "jsonb_path_ops"},
    )
    op.create_index(
        "idx_app_profile_tenant_status", "application_profiles", ["tenant_id", "status"]
    )
    op.create_index(
        "idx_app_profile_tenant_domain", "application_profiles", ["tenant_id", "domain"]
    )


def downgrade() -> None:
    op.drop_index("idx_app_profile_tenant_domain", table_name="application_profiles")
    op.drop_index("idx_app_profile_tenant_status", table_name="application_profiles")
    op.drop_index("idx_app_profile_json_gin", table_name="application_profiles")
    op.drop_column("application_profiles", "planes")
    op.drop_column("application_profiles", "domain")
    op.drop_column("application_profiles", "status")
