# src/investigation_agent_platform/infrastructure/persistence/reference_doc_repository.py
"""SQLAlchemy reference-document repository (Part 11.6)."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert

from investigation_agent_platform.domain.common.exceptions import ConcurrencyError
from investigation_agent_platform.domain.reference.documents import (
    REFERENCE_EMBEDDING_DIMS,
    ChunkLifecycle,
    GenerationStatus,
    ReferenceDocumentChunk,
)
from investigation_agent_platform.infrastructure.persistence.models import (
    ReferenceDocumentChunkORM,
    ReferenceIndexGenerationORM,
)
from investigation_agent_platform.infrastructure.persistence.rls import rls_session


def _chunk_to_orm(
    chunk: ReferenceDocumentChunk, vector: list[float] | None
) -> ReferenceDocumentChunkORM:
    if vector is not None and len(vector) != REFERENCE_EMBEDDING_DIMS:
        raise ConcurrencyError(
            f"Reference vector has {len(vector)} dims, expected {REFERENCE_EMBEDDING_DIMS}"
        )
    return ReferenceDocumentChunkORM(
        id=chunk.id,
        tenant_id=chunk.tenant_id,
        application_id=chunk.application_id,
        source_id=chunk.source_id,
        document_path=chunk.document_path,
        chunk_index=chunk.chunk_index,
        content=chunk.content,
        content_hash=chunk.content_hash,
        source_revision=chunk.source_revision,
        embedding_model=chunk.embedding_model,
        embedding_version=chunk.embedding_version,
        generation=chunk.generation,
        lifecycle=chunk.lifecycle.value,
        provenance_json=(
            chunk.provenance.model_dump(mode="json") if chunk.provenance is not None else None
        ),
        lexical_tsv=func.to_tsvector("english", chunk.content[:8000] or ""),
        embedding=vector,
        created_at=chunk.created_at,
    )


class SqlAlchemyReferenceDocRepository:
    """PostgreSQL-backed reference chunks + generations, tenant-scoped."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    async def active_generation(self, tenant_id: str, source_id: str) -> int:
        async with rls_session(self._session_factory, tenant_id) as session:
            generation = await session.scalar(
                select(func.max(ReferenceIndexGenerationORM.generation)).where(
                    ReferenceIndexGenerationORM.tenant_id == tenant_id,
                    ReferenceIndexGenerationORM.source_id == source_id,
                    ReferenceIndexGenerationORM.status == GenerationStatus.ACTIVE.value,
                )
            )
            return int(generation or 0)

    async def create_generation(
        self,
        tenant_id: str,
        source_id: str,
        generation: int,
        source_revision: str,
        embedding_model: str,
        embedding_version: str,
    ) -> UUID:
        generation_id = uuid4()
        async with rls_session(self._session_factory, tenant_id) as session:
            session.add(
                ReferenceIndexGenerationORM(
                    id=generation_id,
                    tenant_id=tenant_id,
                    source_id=source_id,
                    generation=generation,
                    status=GenerationStatus.ACTIVE.value,
                    source_revision=source_revision,
                    embedding_model=embedding_model,
                    embedding_version=embedding_version,
                    created_at=datetime.now(UTC),
                )
            )
        return generation_id

    async def set_generation_status(
        self, tenant_id: str, generation_id: UUID, status: str, chunk_count: int = 0
    ) -> None:
        if status not in {
            GenerationStatus.ACTIVE.value,
            GenerationStatus.SUPERSEDED.value,
            GenerationStatus.FAILED.value,
        }:
            raise ConcurrencyError(f"Unknown generation status {status!r}")
        async with rls_session(self._session_factory, tenant_id) as session:
            row = await session.get(ReferenceIndexGenerationORM, generation_id)
            if row is None or row.tenant_id != tenant_id:
                raise ConcurrencyError("Generation not found")
            row.status = status
            row.chunk_count = chunk_count

    async def list_generations(self, tenant_id: str, source_id: str) -> list[dict[str, Any]]:
        async with rls_session(self._session_factory, tenant_id) as session:
            rows = (
                await session.scalars(
                    select(ReferenceIndexGenerationORM)
                    .where(
                        ReferenceIndexGenerationORM.tenant_id == tenant_id,
                        ReferenceIndexGenerationORM.source_id == source_id,
                    )
                    .order_by(ReferenceIndexGenerationORM.generation.desc())
                )
            ).all()
            return [
                {
                    "generation": row.generation,
                    "status": row.status,
                    "source_revision": row.source_revision,
                    "chunk_count": row.chunk_count,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]

    async def upsert_chunk(
        self, tenant_id: str, chunk: ReferenceDocumentChunk, vector: list[float] | None
    ) -> None:
        if chunk.tenant_id != tenant_id:
            raise ConcurrencyError("Reference chunk tenant mismatch")
        row = _chunk_to_orm(chunk, vector)
        async with rls_session(self._session_factory, tenant_id) as session:
            stmt = (
                pg_insert(ReferenceDocumentChunkORM)
                .values(
                    id=row.id,
                    tenant_id=row.tenant_id,
                    application_id=row.application_id,
                    source_id=row.source_id,
                    document_path=row.document_path,
                    chunk_index=row.chunk_index,
                    content=row.content,
                    content_hash=row.content_hash,
                    source_revision=row.source_revision,
                    embedding_model=row.embedding_model,
                    embedding_version=row.embedding_version,
                    generation=row.generation,
                    lifecycle=row.lifecycle,
                    provenance_json=row.provenance_json,
                    lexical_tsv=row.lexical_tsv,
                    embedding=row.embedding,
                    created_at=row.created_at,
                )
                .on_conflict_do_update(
                    constraint="uq_refdoc_chunk_space",
                    set_={
                        "content": row.content,
                        "content_hash": row.content_hash,
                        "lifecycle": row.lifecycle,
                        "lexical_tsv": row.lexical_tsv,
                        "embedding": row.embedding,
                    },
                )
            )
            await session.execute(stmt)

    async def chunks_for_paths(
        self, tenant_id: str, source_id: str, generation: int
    ) -> dict[str, str]:
        async with rls_session(self._session_factory, tenant_id) as session:
            rows = (
                await session.scalars(
                    select(ReferenceDocumentChunkORM).where(
                        ReferenceDocumentChunkORM.tenant_id == tenant_id,
                        ReferenceDocumentChunkORM.source_id == source_id,
                        ReferenceDocumentChunkORM.generation == generation,
                        ReferenceDocumentChunkORM.lifecycle == ChunkLifecycle.ACTIVE.value,
                    )
                )
            ).all()
            merged: dict[str, str] = {}
            for row in rows:
                merged.setdefault(f"{row.document_path}", row.content_hash)
            return merged

    async def tombstone_missing(
        self, tenant_id: str, source_id: str, generation: int, seen_hashes: set[str]
    ) -> int:
        async with rls_session(self._session_factory, tenant_id) as session:
            rows = (
                await session.scalars(
                    select(ReferenceDocumentChunkORM).where(
                        ReferenceDocumentChunkORM.tenant_id == tenant_id,
                        ReferenceDocumentChunkORM.source_id == source_id,
                        ReferenceDocumentChunkORM.generation == generation,
                        ReferenceDocumentChunkORM.lifecycle == ChunkLifecycle.ACTIVE.value,
                    )
                )
            ).all()
            count = 0
            for row in rows:
                if row.content_hash not in seen_hashes:
                    row.lifecycle = ChunkLifecycle.TOMBSTONED.value
                    count += 1
            return count

    async def lexical_candidates(
        self, tenant_id: str, query: str, limit: int
    ) -> list[ReferenceDocumentChunkORM]:
        async with rls_session(self._session_factory, tenant_id) as session:
            tsquery = func.plainto_tsquery("english", query[:1000])
            active_gens = select(
                ReferenceIndexGenerationORM.source_id,
                ReferenceIndexGenerationORM.generation,
            ).where(
                ReferenceIndexGenerationORM.tenant_id == tenant_id,
                ReferenceIndexGenerationORM.status == GenerationStatus.ACTIVE.value,
            )
            rows = (
                await session.scalars(
                    select(ReferenceDocumentChunkORM)
                    .where(
                        ReferenceDocumentChunkORM.tenant_id == tenant_id,
                        ReferenceDocumentChunkORM.lifecycle == ChunkLifecycle.ACTIVE.value,
                        tuple_(
                            ReferenceDocumentChunkORM.source_id,
                            ReferenceDocumentChunkORM.generation,
                        ).in_(active_gens),
                        ReferenceDocumentChunkORM.lexical_tsv.op("@@")(tsquery),
                    )
                    .order_by(func.ts_rank(ReferenceDocumentChunkORM.lexical_tsv, tsquery).desc())
                    .limit(max(1, min(limit, 60)))
                )
            ).all()
            return list(rows)

    async def vector_candidates(
        self,
        tenant_id: str,
        vector: list[float],
        embedding_model: str,
        embedding_version: str,
        limit: int,
    ) -> list[ReferenceDocumentChunkORM]:
        if len(vector) != REFERENCE_EMBEDDING_DIMS:
            raise ConcurrencyError(
                f"Query vector has {len(vector)} dims, expected {REFERENCE_EMBEDDING_DIMS}"
            )
        async with rls_session(self._session_factory, tenant_id) as session:
            active_gens = select(
                ReferenceIndexGenerationORM.source_id,
                ReferenceIndexGenerationORM.generation,
            ).where(
                ReferenceIndexGenerationORM.tenant_id == tenant_id,
                ReferenceIndexGenerationORM.status == GenerationStatus.ACTIVE.value,
            )
            rows = (
                await session.scalars(
                    select(ReferenceDocumentChunkORM)
                    .where(
                        ReferenceDocumentChunkORM.tenant_id == tenant_id,
                        ReferenceDocumentChunkORM.lifecycle == ChunkLifecycle.ACTIVE.value,
                        ReferenceDocumentChunkORM.embedding_model == embedding_model,
                        ReferenceDocumentChunkORM.embedding_version == embedding_version,
                        ReferenceDocumentChunkORM.embedding.is_not(None),
                        tuple_(
                            ReferenceDocumentChunkORM.source_id,
                            ReferenceDocumentChunkORM.generation,
                        ).in_(active_gens),
                    )
                    .order_by(ReferenceDocumentChunkORM.embedding.cosine_distance(vector))
                    .limit(max(1, min(limit, 60)))
                )
            ).all()
            return list(rows)

    async def delete_source(self, tenant_id: str, source_id: str) -> int:
        async with rls_session(self._session_factory, tenant_id) as session:
            chunks = (
                await session.scalars(
                    select(ReferenceDocumentChunkORM).where(
                        ReferenceDocumentChunkORM.tenant_id == tenant_id,
                        ReferenceDocumentChunkORM.source_id == source_id,
                    )
                )
            ).all()
            generations = (
                await session.scalars(
                    select(ReferenceIndexGenerationORM).where(
                        ReferenceIndexGenerationORM.tenant_id == tenant_id,
                        ReferenceIndexGenerationORM.source_id == source_id,
                    )
                )
            ).all()
            removed = len(chunks) + len(generations)
            for row in [*chunks, *generations]:
                await session.delete(row)
            return removed
