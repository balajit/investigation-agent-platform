# src/investigation_agent_platform/ports/reference/documents.py
"""Reference-document ports (Part 11.6).

A dedicated application service implements `ReferenceDocumentPort`; it is
never folded into the runtime evidence gateway. Fetchers are trusted
platform-installed implementations resolved by source kind.
"""

from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from investigation_agent_platform.domain.reference.documents import (
    FetchedFile,
    IndexRunSummary,
    IndexStatus,
    ReferenceDocumentSource,
    ReferenceSearchHit,
)


@runtime_checkable
class ReferenceFetcherPort(Protocol):
    """Fetch raw files for one source spec (Part 11.6)."""

    async def fetch(self, tenant_id: str, source: ReferenceDocumentSource) -> list[FetchedFile]:
        """Fetch bounded files; raises on any fetch failure (no partial)."""
        ...


@runtime_checkable
class ReferenceDocumentPort(Protocol):
    """Scope-aware reference search/index/status (Part 11.6)."""

    async def search(
        self,
        tenant_id: str,
        query: str,
        application_id: str | None = None,
        kinds: list[str] | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[ReferenceSearchHit], str | None]:
        """Hybrid retrieval returning (hits, next_cursor)."""
        ...

    async def index_source(
        self, tenant_id: str, source_id: str, idempotency_key: str
    ) -> IndexRunSummary:
        """Index one configured source; failed runs keep prior generation."""
        ...

    async def index_status(self, tenant_id: str, source_id: str) -> IndexStatus:
        """Current generation status for one source."""
        ...

    async def rollback_generation(
        self, tenant_id: str, source_id: str, generation: int
    ) -> IndexStatus:
        """Reactivate a prior generation (blue/green rollback window)."""
        ...

    async def purge_source(self, tenant_id: str, source_id: str) -> int:
        """Hard-delete one source's chunks + generations; returns rows removed."""
        ...


@runtime_checkable
class ReferenceChunkRepository(Protocol):
    """Platform-owned chunk + generation persistence (Part 11.6)."""

    async def active_generation(self, tenant_id: str, source_id: str) -> int:
        """Newest ACTIVE generation number, 0 when none."""
        ...

    async def create_generation(
        self,
        tenant_id: str,
        source_id: str,
        generation: int,
        source_revision: str,
        embedding_model: str,
        embedding_version: str,
    ) -> UUID:
        """Open a new generation row (status ACTIVE on success path)."""
        ...

    async def set_generation_status(
        self, tenant_id: str, generation_id: UUID, status: str, chunk_count: int = 0
    ) -> None:
        """Transition one generation row (ACTIVE/SUPERSEDED/FAILED)."""
        ...

    async def list_generations(self, tenant_id: str, source_id: str) -> list[dict[str, Any]]:
        """All generations for a source, newest first."""
        ...

    async def upsert_chunk(self, tenant_id: str, chunk: Any, vector: list[float] | None) -> None:
        """Idempotent chunk write for one (source, generation, path, index)."""
        ...

    async def chunks_for_paths(
        self, tenant_id: str, source_id: str, generation: int
    ) -> dict[str, str]:
        """Active path → content_hash map for reconciliation."""
        ...

    async def tombstone_missing(
        self, tenant_id: str, source_id: str, generation: int, seen_hashes: set[str]
    ) -> int:
        """Tombstone active chunks whose hash was not observed; returns count."""
        ...

    async def lexical_candidates(self, tenant_id: str, query: str, limit: int) -> list[Any]:
        """Bounded full-text candidates over ACTIVE chunks (tombstones out)."""
        ...

    async def vector_candidates(
        self,
        tenant_id: str,
        vector: list[float],
        embedding_model: str,
        embedding_version: str,
        limit: int,
    ) -> list[Any]:
        """Bounded cosine candidates over ACTIVE chunks (tombstones out)."""
        ...

    async def delete_source(self, tenant_id: str, source_id: str) -> int:
        """Hard-delete chunks + generations; returns rows removed."""
        ...
