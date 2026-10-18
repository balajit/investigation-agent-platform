"""Alembic migration: Part 11.7 finding clusters + assignments + embeddings.

Adds ``finding_clusters`` (tenant taxonomy, unique per tenant/key/revision),
``finding_cluster_assignments`` (normalized membership history; no FK to
clusters so the UNASSIGNED bucket needs no taxonomy row), and
``finding_embeddings`` (one row per finding/model/version/generation with a
pgvector column, lexical text + GIN full-text index, HNSW cosine index).
All tables are new — no data migration. Downgrade drops policies, indexes,
and tables.
"""

# revision identifiers, used by Alembic.
revision = "012_finding_clusters"
down_revision = "011_input_requirements"
branch_labels = None
depends_on = None

import sqlalchemy as sa  # type: ignore[import-not-found]
from alembic import op  # type: ignore[import-not-found]
from pgvector.sqlalchemy import Vector  # type: ignore[import-not-found]


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    try:
        return sa.inspect(bind).has_table(table)
    except Exception:
        # Offline (--sql) mode uses a MockConnection that cannot be
        # inspected; emit the DDL unconditionally so SQL scripts stay complete.
        return False


def _tenant_policy(table: str, policy: str) -> None:
    op.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    op.execute(
        sa.text(
            f"""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE tablename='{table}' AND policyname='{policy}'
                ) THEN
                    CREATE POLICY {policy} ON {table}
                        USING (tenant_id = current_setting('app.tenant_id', true))
                        WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
                END IF;
            END $$;
            """
        )
    )
    op.execute(sa.text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))


def upgrade() -> None:
    if not _table_exists("finding_clusters"):
        op.create_table(
            "finding_clusters",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("cluster_key", sa.String(16), nullable=False),
            sa.Column("label", sa.String(128), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("taxonomy_revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("lexical_tsv", sa.dialects.postgresql.TSVECTOR(), nullable=True),
            sa.UniqueConstraint(
                "tenant_id",
                "cluster_key",
                "taxonomy_revision",
                name="uq_cluster_tenant_key_revision",
            ),
        )
        op.create_index(
            "idx_cluster_tenant_revision",
            "finding_clusters",
            ["tenant_id", "taxonomy_revision"],
        )
        op.create_index(
            "idx_cluster_lexical",
            "finding_clusters",
            ["lexical_tsv"],
            postgresql_using="gin",
        )
    _tenant_policy("finding_clusters", "cluster_tenant")

    if not _table_exists("finding_cluster_assignments"):
        op.create_table(
            "finding_cluster_assignments",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("finding_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("cluster_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("method", sa.String(64), nullable=False, server_default="llm-assign"),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
            sa.Column("taxonomy_revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
            sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
            sa.Column("provenance_json", sa.dialects.postgresql.JSONB, nullable=True),
        )
        op.create_index(
            "idx_assignment_tenant_finding",
            "finding_cluster_assignments",
            ["tenant_id", "finding_id"],
        )
        op.create_index(
            "idx_assignment_tenant_cluster",
            "finding_cluster_assignments",
            ["tenant_id", "cluster_id"],
        )
    _tenant_policy("finding_cluster_assignments", "assignment_tenant")

    if not _table_exists("finding_embeddings"):
        op.create_table(
            "finding_embeddings",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("finding_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("embedding_model", sa.String(128), nullable=False),
            sa.Column("embedding_version", sa.String(32), nullable=False, server_default="1.0"),
            sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
            # Fixed-dimension pgvector: HNSW requires declared dimensions.
            # 1536 pins the inaugural text-embedding-3-small space (see
            # FINDING_EMBEDDING_DIMS); spaces stay isolated by
            # (model, version) at query time, never compared across spaces.
            sa.Column("embedding", Vector(1536), nullable=True),
            sa.Column("lexical", sa.Text(), nullable=True),
            sa.Column("lexical_tsv", sa.dialects.postgresql.TSVECTOR(), nullable=True),
            sa.Column("lifecycle", sa.String(32), nullable=False, server_default="ACTIVE"),
            sa.Column("provenance_json", sa.dialects.postgresql.JSONB, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "tenant_id",
                "finding_id",
                "embedding_model",
                "embedding_version",
                "generation",
                name="uq_finding_embedding_space",
            ),
        )
        op.create_index(
            "idx_finding_emb_tenant_model",
            "finding_embeddings",
            ["tenant_id", "embedding_model"],
        )
        op.create_index(
            "idx_finding_emb_lexical",
            "finding_embeddings",
            ["lexical_tsv"],
            postgresql_using="gin",
        )
        op.create_index(
            "idx_finding_emb_vector",
            "finding_embeddings",
            ["embedding"],
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        )
    _tenant_policy("finding_embeddings", "finding_embedding_tenant")


def downgrade() -> None:
    op.execute(sa.text("DROP POLICY IF EXISTS finding_embedding_tenant ON finding_embeddings"))
    op.execute(sa.text("DROP TABLE IF EXISTS finding_embeddings"))
    op.execute(sa.text("DROP POLICY IF EXISTS assignment_tenant ON finding_cluster_assignments"))
    op.execute(sa.text("DROP TABLE IF EXISTS finding_cluster_assignments"))
    op.execute(sa.text("DROP POLICY IF EXISTS cluster_tenant ON finding_clusters"))
    op.execute(sa.text("DROP TABLE IF EXISTS finding_clusters"))
