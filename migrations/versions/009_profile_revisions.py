"""Alembic migration: profile immutable revisions + investigation metadata (Part 11.2).

application_profiles: replaces the single-row-per-id model with immutable,
tenant-aware revisions. Adds a surrogate ``row_id`` primary key, a ``version``
column (existing rows backfilled to 1 — they become the first revision), and
a unique ``(tenant_id, id, version)`` constraint. The prior PK (``id`` alone)
is dropped: two tenants may now share an application id, and one tenant may
hold many historical revisions of the same id.

investigations: adds ``metadata_json`` (nullable JSONB) so
``Investigation.metadata`` round-trips through persistence instead of being
silently dropped on every save/load — used starting now to pin the resolved
profile revision an investigation was created against.
"""

# revision identifiers, used by Alembic.
revision = "009_profile_revisions"
down_revision = "008_evidence_observed_nullable"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # type: ignore[import-not-found]
from alembic import op  # type: ignore[import-not-found]
from sqlalchemy.dialects import postgresql  # type: ignore[import-not-found]


def upgrade() -> None:
    # -- investigations: metadata round-trip -----------------------------
    op.add_column(
        "investigations",
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
    )

    # -- application_profiles: immutable revisions -----------------------
    op.add_column(
        "application_profiles",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "application_profiles",
        sa.Column(
            "row_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
    )
    # Drop the old single-column PK (id alone) before promoting row_id.
    op.drop_constraint("application_profiles_pkey", "application_profiles", type_="primary")
    op.create_primary_key("application_profiles_pkey", "application_profiles", ["row_id"])
    op.create_index("idx_app_profile_tenant_app", "application_profiles", ["tenant_id", "id"])
    op.create_unique_constraint(
        "uq_app_profile_tenant_id_version",
        "application_profiles",
        ["tenant_id", "id", "version"],
    )
    # Drop the server defaults now that existing rows are backfilled — new
    # rows must supply both explicitly (ORM always does).
    op.alter_column("application_profiles", "version", server_default=None)
    op.alter_column("application_profiles", "row_id", server_default=None)


def downgrade() -> None:
    op.drop_constraint("uq_app_profile_tenant_id_version", "application_profiles", type_="unique")
    op.drop_index("idx_app_profile_tenant_app", table_name="application_profiles")
    op.drop_constraint("application_profiles_pkey", "application_profiles", type_="primary")
    # Downgrade only supports the single-latest-revision-per-id shape;
    # multiple revisions of the same id cannot coexist under the old PK.
    # Callers downgrading a database with multiple revisions must first
    # delete all but the latest revision per (tenant_id, id).
    op.create_primary_key("application_profiles_pkey", "application_profiles", ["id"])
    op.drop_column("application_profiles", "row_id")
    op.drop_column("application_profiles", "version")

    op.drop_column("investigations", "metadata_json")
