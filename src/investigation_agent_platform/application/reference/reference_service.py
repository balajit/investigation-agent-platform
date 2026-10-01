# src/investigation_agent_platform/application/reference/reference_service.py
"""Reference-document application service (Part 11.6).

Dedicated sibling to the runtime evidence gateway (never folded into it):
scope-aware search/index/status over platform-owned chunks, generation
reconciliation (only successful runs activate; failures keep the prior
generation), and hybrid bounded lexical/vector retrieval with
reciprocal-rank fusion. Tombstoned chunks are excluded from both branches.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Any
from uuid import UUID

from investigation_agent_platform.domain.common.provenance import ProvenanceRecord
from investigation_agent_platform.domain.reference.documents import (
    MAX_REFERENCE_CHUNK_CHARS,
    MAX_REFERENCE_CHUNKS_PER_RUN,
    MAX_REFERENCE_FILES,
    REFERENCE_DOC_CONTRACT_VERSION,
    ChunkLifecycle,
    FetchedFile,
    GenerationStatus,
    IndexRunSummary,
    IndexStatus,
    ReferenceDocumentChunk,
    ReferenceDocumentSource,
    ReferenceSearchHit,
)

logger = logging.getLogger(__name__)

REFDOC_BUILDER_PLUGIN_ID = "reference-document-service"
REFDOC_BUILDER_VERSION = "1.0"

_CHUNK_OVERLAP_CHARS = 200
_LEXICAL_CANDIDATES = 30
_VECTOR_CANDIDATES = 30
_RRF_K = 60


def source_id_for(application_id: str | None, source: ReferenceDocumentSource) -> str:
    """Stable opaque source id: sha256 over (application, canonical spec)."""
    canonical = json.dumps(
        {"application_id": application_id or "", "spec": source.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def _chunk_text(content: str) -> list[str]:
    """Fixed windows with small overlap; empty content yields no chunks."""
    size = MAX_REFERENCE_CHUNK_CHARS
    chunks: list[str] = []
    start = 0
    while start < len(content):
        chunks.append(content[start : start + size])
        if start + size >= len(content):
            break
        start += size - _CHUNK_OVERLAP_CHARS
    return chunks


def _cursor_encode(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode()


def _cursor_decode(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())["o"]))
    except Exception:
        return 0


class ReferenceDocumentService:
    """Scope-aware reference indexing + hybrid search (Part 11.6)."""

    def __init__(
        self,
        chunk_repo: Any,
        profile_repo: Any | None = None,
        embedder: Any | None = None,
        embedding_model: str = "text-embedding-3-small",
        embedding_version: str = "1.0",
    ) -> None:
        self._chunk_repo = chunk_repo
        self._profile_repo = profile_repo
        self._embedder = embedder
        self._embedding_model = embedding_model
        self._embedding_version = embedding_version

    # -- sources ---------------------------------------------------------

    async def _resolve_spec(
        self, tenant_id: str, application_id: str | None, source_id: str
    ) -> tuple[Any, ReferenceDocumentSource]:
        """Resolve (profile, spec) for a source id; fail closed when absent."""
        if self._profile_repo is None:
            raise ValueError("Reference retrieval is unconfigured for this tenant")
        profiles = await self._profile_repo.list(tenant_id)
        for profile in profiles:
            refdocs = getattr(profile, "reference_docs_configuration", None)
            if refdocs is None:
                continue
            if application_id is not None and profile.id != application_id:
                continue
            for raw in refdocs.sources:
                try:
                    spec = _validate_spec(raw)
                except Exception:
                    continue
                if source_id_for(profile.id, spec) == source_id:
                    return refdocs, spec
        raise ValueError(f"Unknown reference source {source_id!r}")

    async def list_sources(
        self, tenant_id: str, application_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Configured sources with stable ids (for status/reindex clients)."""
        if self._profile_repo is None:
            return []
        profiles = await self._profile_repo.list(tenant_id)
        sources: list[dict[str, Any]] = []
        for profile in profiles:
            refdocs = getattr(profile, "reference_docs_configuration", None)
            if refdocs is None:
                continue
            if application_id is not None and profile.id != application_id:
                continue
            for raw in refdocs.sources:
                try:
                    spec = _validate_spec(raw)
                except Exception:
                    continue
                sources.append(
                    {
                        "source_id": source_id_for(profile.id, spec),
                        "kind": spec.kind.value,
                        "application_id": profile.id,
                    }
                )
        return sources

    # -- indexing ----------------------------------------------------------

    async def index_source(
        self, tenant_id: str, source_id: str, application_id: str | None = None
    ) -> IndexRunSummary:
        """Index one source; failed runs keep the prior generation active."""
        from investigation_agent_platform.infrastructure.reference.fetchers import fetcher_for

        refdocs, spec = await self._resolve_spec(tenant_id, application_id, source_id)
        fetcher = fetcher_for(spec)
        if spec.kind.value == "git":
            fetcher = _policy_fetcher(fetcher, refdocs, "git")
        elif spec.kind.value == "s3":
            fetcher = _policy_fetcher(fetcher, refdocs, "s3")
        elif spec.kind.value == "local_path":
            from investigation_agent_platform.infrastructure.reference.fetchers import (
                LocalPathReferenceFetcher,
            )

            fetcher = LocalPathReferenceFetcher(allowed_roots=list(refdocs.local_allowed_roots))
        try:
            files = await fetcher.fetch(tenant_id, spec)
        except Exception as exc:
            logger.warning("Reference fetch failed", extra={"source_id": source_id})
            raise RuntimeError(f"Reference fetch failed for {source_id!r}: {exc}") from exc
        files = files[:MAX_REFERENCE_FILES]
        if not files:
            raise RuntimeError(f"Reference fetch returned no usable files for {source_id!r}")
        previous = await self._chunk_repo.active_generation(tenant_id, source_id)
        previous_map = (
            await self._chunk_repo.chunks_for_paths(tenant_id, source_id, previous)
            if previous
            else {}
        )
        manifest = {item.path: item.content_hash for item in files}
        if previous and manifest == previous_map:
            # Unchanged-file reindex is a complete no-op: no new generation,
            # no writes, prior generation stays active.
            return IndexRunSummary(
                tenant_id=tenant_id,
                source_id=source_id,
                generation=previous,
                files_seen=len(files),
                chunks_written=0,
                tombstoned=0,
            )
        revision = files[0].source_revision if files else ""
        generation = previous + 1
        generation_id = await self._chunk_repo.create_generation(
            tenant_id,
            source_id,
            generation,
            revision,
            self._embedding_model,
            self._embedding_version,
        )
        try:
            written, seen_hashes = await self._write_chunks(
                tenant_id, source_id, generation, files, application_id
            )
            tombstoned = await self._tombstone_unseen(
                tenant_id, source_id, generation, previous_map, set(manifest.values())
            )
            await self._chunk_repo.set_generation_status(
                tenant_id, generation_id, GenerationStatus.ACTIVE.value, written
            )
            if previous:
                await self._supersede(tenant_id, source_id, previous)
        except Exception as exc:
            try:
                await self._chunk_repo.set_generation_status(
                    tenant_id, generation_id, GenerationStatus.FAILED.value, 0
                )
            except Exception:
                pass
            logger.warning("Reference index run failed", extra={"source_id": source_id})
            raise RuntimeError(f"Reference index failed for {source_id!r}: {exc}") from exc
        return IndexRunSummary(
            tenant_id=tenant_id,
            source_id=source_id,
            generation=generation,
            files_seen=len(files),
            chunks_written=written,
            tombstoned=tombstoned,
        )

    async def _write_chunks(
        self,
        tenant_id: str,
        source_id: str,
        generation: int,
        files: list[FetchedFile],
        application_id: str | None,
    ) -> tuple[int, set[str]]:
        """Chunk + embed + write; returns (rows_written, seen_hashes)."""
        vectors: dict[int, list[float] | None] = {}
        flat: list[tuple[FetchedFile, str]] = []
        for item in files:
            for piece in _chunk_text(item.content):
                flat.append((item, piece))
                if len(flat) >= MAX_REFERENCE_CHUNKS_PER_RUN:
                    break
            if len(flat) >= MAX_REFERENCE_CHUNKS_PER_RUN:
                break
        if self._embedder is not None and flat:
            try:
                results = await self._embedder.embed([piece for _, piece in flat])
                for index, result in enumerate(results):
                    if result is not None:
                        vectors[index] = list(result.vector)
            except Exception as exc:
                raise RuntimeError(f"Reference embedding failed: {exc}") from exc
        written = 0
        seen: set[str] = set()
        for index, (item, piece) in enumerate(flat):
            seen.add(item.content_hash)
            await self._chunk_repo.upsert_chunk(
                tenant_id,
                ReferenceDocumentChunk(
                    tenant_id=tenant_id,
                    application_id=application_id,
                    source_id=source_id,
                    document_path=item.path,
                    chunk_index=index,
                    content=piece,
                    content_hash=item.content_hash,
                    source_revision=item.source_revision,
                    embedding_model=self._embedding_model,
                    embedding_version=self._embedding_version,
                    generation=generation,
                    provenance=ProvenanceRecord(
                        plugin_id=REFDOC_BUILDER_PLUGIN_ID,
                        plugin_version=REFDOC_BUILDER_VERSION,
                        schema_version=REFERENCE_DOC_CONTRACT_VERSION,
                        input_digests={"content": item.content_hash},
                    ),
                ),
                vectors.get(index),
            )
            written += 1
        return written, seen

    async def _tombstone_unseen(
        self,
        tenant_id: str,
        source_id: str,
        generation: int,
        previous_map: dict[str, str],
        seen_hashes: set[str],
    ) -> int:
        """Explicit tombstone rows for files missing from the new generation."""
        tombstoned = 0
        for path, content_hash in previous_map.items():
            if content_hash in seen_hashes:
                continue
            await self._chunk_repo.upsert_chunk(
                tenant_id,
                ReferenceDocumentChunk(
                    tenant_id=tenant_id,
                    source_id=source_id,
                    document_path=path,
                    chunk_index=0,
                    content="",
                    content_hash=content_hash,
                    lifecycle=ChunkLifecycle.TOMBSTONED,
                    generation=generation,
                    provenance=ProvenanceRecord(
                        plugin_id=REFDOC_BUILDER_PLUGIN_ID,
                        plugin_version=REFDOC_BUILDER_VERSION,
                        schema_version=REFERENCE_DOC_CONTRACT_VERSION,
                        input_digests={"tombstoned_path": path},
                    ),
                ),
                None,
            )
            tombstoned += 1
        return tombstoned

    async def _supersede(self, tenant_id: str, tenant_source: str, previous: int) -> None:
        for row in await self._chunk_repo.list_generations(tenant_id, tenant_source):
            if row["generation"] == previous and row["status"] == GenerationStatus.ACTIVE.value:
                generations = await self._generation_id(tenant_id, tenant_source, previous)
                if generations is not None:
                    await self._chunk_repo.set_generation_status(
                        tenant_id,
                        generations,
                        GenerationStatus.SUPERSEDED.value,
                        row.get("chunk_count", 0),
                    )

    async def _generation_id(self, tenant_id: str, source_id: str, generation: int) -> UUID | None:
        for row in await self._chunk_repo.list_generations(tenant_id, source_id):
            if row["generation"] == generation:
                raw = row.get("id")
                try:
                    return raw if isinstance(raw, UUID) else UUID(str(raw))
                except (ValueError, TypeError, AttributeError):
                    return None
        return None

    # -- status / rollback / purge -------------------------------------------

    async def _index_status(self, tenant_id: str, source_id: str) -> IndexStatus:
        active = await self._chunk_repo.active_generation(tenant_id, source_id)
        if not active:
            return IndexStatus(tenant_id=tenant_id, source_id=source_id)
        revision = ""
        count = 0
        for row in await self._chunk_repo.list_generations(tenant_id, source_id):
            if row["generation"] == active:
                revision = str(row.get("source_revision", ""))
                count = int(row.get("chunk_count", 0))
        return IndexStatus(
            tenant_id=tenant_id,
            source_id=source_id,
            active_generation=active,
            source_revision=revision,
            chunk_count=count,
        )

    async def index_status(self, tenant_id: str, source_id: str) -> IndexStatus:
        return await self._index_status(tenant_id, source_id)

    async def rollback_generation(
        self, tenant_id: str, source_id: str, generation: int
    ) -> IndexStatus:
        """Reactivate a prior generation (blue/green rollback window)."""
        target_id = await self._generation_id(tenant_id, source_id, generation)
        if target_id is None:
            raise ValueError(f"Unknown generation {generation} for {source_id!r}")
        current = await self._chunk_repo.active_generation(tenant_id, source_id)
        current_id = await self._generation_id(tenant_id, source_id, current) if current else None
        await self._chunk_repo.set_generation_status(
            tenant_id, target_id, GenerationStatus.ACTIVE.value
        )
        if current_id is not None and current != generation:
            await self._chunk_repo.set_generation_status(
                tenant_id, current_id, GenerationStatus.SUPERSEDED.value
            )
        return await self._index_status(tenant_id, source_id)

    async def purge_source(self, tenant_id: str, source_id: str) -> int:
        removed: int = await self._chunk_repo.delete_source(tenant_id, source_id)
        return removed

    # -- search -----------------------------------------------------------------

    async def search(
        self,
        tenant_id: str,
        query: str,
        application_id: str | None = None,
        kinds: list[str] | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[ReferenceSearchHit], str | None]:
        """Hybrid lexical/vector retrieval with reciprocal-rank fusion."""
        if not query or not query.strip():
            raise ValueError("query must not be empty")
        limit = max(1, min(limit, 200))
        offset = _cursor_decode(cursor)
        lexical = await self._chunk_repo.lexical_candidates(
            tenant_id, query.strip(), _LEXICAL_CANDIDATES
        )
        vector_rows: list[Any] = []
        if self._embedder is not None:
            try:
                results = await self._embedder.embed([query.strip()])
                if results and results[0] is not None:
                    vector_rows = await self._chunk_repo.vector_candidates(
                        tenant_id,
                        list(results[0].vector),
                        self._embedding_model,
                        self._embedding_version,
                        _VECTOR_CANDIDATES,
                    )
            except Exception as exc:
                logger.warning(
                    "Reference vector branch failed; lexical only", extra={"error": str(exc)}
                )
        fused = _reciprocal_rank_fusion(lexical, vector_rows)
        if kinds:
            wanted = {kind.lower() for kind in kinds}
            sources = await self.list_sources(tenant_id, application_id)
            allowed = {
                item["source_id"] for item in sources if str(item.get("kind", "")).lower() in wanted
            }
            fused = [(row, score) for row, score in fused if _row_source(row) in allowed]
        if application_id is not None:
            fused = [
                (row, score)
                for row, score in fused
                if getattr(row, "application_id", None) == application_id
            ]
        page = fused[offset : offset + limit]
        hits = [
            ReferenceSearchHit(
                chunk_id=_row_id(row),
                source_id=_row_source(row),
                document_path=str(getattr(row, "document_path", "")),
                content=str(getattr(row, "content", ""))[:8000],
                score=score,
                source_revision=str(getattr(row, "source_revision", "")),
            )
            for row, score in page
        ]
        next_cursor = _cursor_encode(offset + limit) if offset + limit < len(fused) else None
        return hits, next_cursor


def _validate_spec(raw: Any) -> ReferenceDocumentSource:
    """Validate one raw source dict into the discriminated union."""
    from pydantic import TypeAdapter

    return TypeAdapter(ReferenceDocumentSource).validate_python(raw)


def _policy_fetcher(fetcher: Any, refdocs: Any, purpose: str) -> Any:
    """Rebuild git/S3 fetchers with the profile's host allowlist."""
    from investigation_agent_platform.infrastructure.reference.fetchers import (
        GitReferenceFetcher,
        S3ReferenceFetcher,
    )
    from investigation_agent_platform.infrastructure.security.endpoint_guard import (
        EndpointPolicy,
    )

    _ = fetcher
    policy = EndpointPolicy(
        allowed_hosts=frozenset(refdocs.allowed_hosts),
        require_https=True,
        allow_loopback=False,
        timeout_seconds=30.0,
        max_response_bytes=1_000_000,
    )
    if purpose == "git":
        return GitReferenceFetcher(endpoint_policy=policy)
    return S3ReferenceFetcher(endpoint_policy=policy)


def _row_id(row: Any) -> UUID:
    raw = getattr(row, "id", None)
    try:
        return raw if isinstance(raw, UUID) else UUID(str(raw))
    except (ValueError, TypeError, AttributeError):
        from uuid import uuid4 as _uuid4

        return _uuid4()


def _row_source(row: Any) -> str:
    return str(getattr(row, "source_id", ""))


def _reciprocal_rank_fusion(
    lexical: list[Any], vector: list[Any], bound: int = 200
) -> list[tuple[Any, float]]:
    """Fuse bounded branches; tombstones never reach here (repo-filtered)."""
    scores: dict[int, float] = {}
    rows: dict[int, Any] = {}
    for rank, row in enumerate(lexical):
        key = id(row)
        rows[key] = row
        scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank + 1)
    for rank, row in enumerate(vector):
        key = id(row)
        rows[key] = row
        scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank + 1)
    ranked = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)[:bound]
    return [(rows[key], score) for key, score in ranked]
