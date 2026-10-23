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


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    try:
        inspector = sa.inspect(bind)
        columns = inspector.get_columns(table)
    except Exception:
        # Missing table, or offline (--sql) mode: report missing so DDL is emitted.
        return False
    return column in {col["name"] for col in columns}


def _index_exists(table: str, index: str) -> bool:
    bind = op.get_bind()
    try:
        inspector = sa.inspect(bind)
        indexes = inspector.get_indexes(table)
    except Exception:
        # Missing table, or offline (--sql) mode: report missing so DDL is emitted.
        return False
    return index in {idx["name"] for idx in indexes}


def upgrade() -> None:
    # Fresh-install guard (chain-rot repair): the 000 baseline creates the
    # full current schema, so these columns/indexes may already exist.
    # Databases upgrading from pre-016 history take the create path via the
    # same guards.
    added = False
    if not _column_exists("application_profiles", "status"):
        op.add_column(
            "application_profiles",
            sa.Column("status", sa.String(32), nullable=False, server_default="ACTIVE"),
        )
        added = True
    if not _column_exists("application_profiles", "domain"):
        op.add_column("application_profiles", sa.Column("domain", sa.String(128), nullable=True))
        added = True
    if not _column_exists("application_profiles", "planes"):
        op.add_column(
            "application_profiles",
            sa.Column("planes", postgresql.ARRAY(sa.String(32)), nullable=True),
        )
        added = True
    if added:
        # Backfill mirrored columns from the authoritative profile_json.
        op.execute(
            "UPDATE application_profiles "
            "SET status = COALESCE(profile_json->>'status', 'ACTIVE'), "
            "domain = profile_json->>'domain', "
            "planes = ARRAY(SELECT jsonb_array_elements_text("
            "COALESCE(profile_json->'planes', '[]'::jsonb)))"
        )
        op.alter_column("application_profiles", "status", server_default=None)
    if not _index_exists("application_profiles", "idx_app_profile_json_gin"):
        op.create_index(
            "idx_app_profile_json_gin",
            "application_profiles",
            ["profile_json"],
            postgresql_using="gin",
            postgresql_ops={"profile_json": "jsonb_path_ops"},
        )
    if not _index_exists("application_profiles", "idx_app_profile_tenant_status"):
        op.create_index(
            "idx_app_profile_tenant_status", "application_profiles", ["tenant_id", "status"]
        )
    if not _index_exists("application_profiles", "idx_app_profile_tenant_domain"):
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
