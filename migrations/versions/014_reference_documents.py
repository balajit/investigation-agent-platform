"""Alembic migration: Part 11.6 reference-document chunks + generations.

Adds ``reference_document_chunks`` (platform-owned retrieval chunks with
lexical TSVECTOR + fixed-dim pgvector, lifecycle for tombstones) and
``reference_index_generations`` (blue/green generation routing). Both tables
are new — no data migration. Downgrade drops policies, indexes, and tables.
"""

# revision identifiers, used by Alembic.
revision = "014_reference_documents"
down_revision = "013_chat_sessions"
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
    if not _table_exists("reference_document_chunks"):
        op.create_table(
            "reference_document_chunks",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("application_id", sa.String(128), nullable=True),
            sa.Column("source_id", sa.String(256), nullable=False),
            sa.Column("document_path", sa.String(1024), nullable=False),
            sa.Column("chunk_index", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("content_hash", sa.String(128), nullable=False, server_default=""),
            sa.Column("source_revision", sa.String(256), nullable=False, server_default=""),
            sa.Column("embedding_model", sa.String(128), nullable=False, server_default=""),
            sa.Column("embedding_version", sa.String(32), nullable=False, server_default="1.0"),
            sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("lifecycle", sa.String(32), nullable=False, server_default="ACTIVE"),
            sa.Column("provenance_json", sa.dialects.postgresql.JSONB, nullable=True),
            sa.Column("lexical_tsv", sa.dialects.postgresql.TSVECTOR(), nullable=True),
            # Fixed-dimension pgvector: HNSW requires declared dimensions
            # (see REFERENCE_EMBEDDING_DIMS); spaces stay isolated by
            # (model, version) at query time.
            sa.Column("embedding", Vector(1536), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "tenant_id",
                "source_id",
                "generation",
                "document_path",
                "chunk_index",
                name="uq_refdoc_chunk_space",
            ),
        )
        op.create_index(
            "idx_refdoc_tenant_source_gen",
            "reference_document_chunks",
            ["tenant_id", "source_id", "generation"],
        )
        op.create_index(
            "idx_refdoc_lexical",
            "reference_document_chunks",
            ["lexical_tsv"],
            postgresql_using="gin",
        )
        op.create_index(
            "idx_refdoc_vector",
            "reference_document_chunks",
            ["embedding"],
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        )
    _tenant_policy("reference_document_chunks", "refdoc_chunk_tenant")

    if not _table_exists("reference_index_generations"):
        op.create_table(
            "reference_index_generations",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("source_id", sa.String(256), nullable=False),
            sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("status", sa.String(32), nullable=False, server_default="ACTIVE"),
            sa.Column("source_revision", sa.String(256), nullable=False, server_default=""),
            sa.Column("embedding_model", sa.String(128), nullable=False, server_default=""),
            sa.Column("embedding_version", sa.String(32), nullable=False, server_default="1.0"),
            sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("provenance_json", sa.dialects.postgresql.JSONB, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "tenant_id",
                "source_id",
                "generation",
                name="uq_refdoc_generation",
            ),
        )
        op.create_index(
            "idx_refdoc_gen_tenant_source",
            "reference_index_generations",
            ["tenant_id", "source_id"],
        )
    _tenant_policy("reference_index_generations", "refdoc_generation_tenant")


def downgrade() -> None:
    op.execute(
        sa.text("DROP POLICY IF EXISTS refdoc_generation_tenant ON reference_index_generations")
    )
    op.execute(sa.text("DROP TABLE IF EXISTS reference_index_generations"))
    op.execute(sa.text("DROP POLICY IF EXISTS refdoc_chunk_tenant ON reference_document_chunks"))
    op.execute(sa.text("DROP TABLE IF EXISTS reference_document_chunks"))
