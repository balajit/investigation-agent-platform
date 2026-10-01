# src/investigation_agent_platform/domain/reference/documents.py
"""Reference-document domain (Part 11.6).

Platform-owned retrieval over external documentation (git, confined local
paths, S3): versioned discriminated fetch specs, content-hashed chunks with
lifecycle (ACTIVE/TOMBSTONED), and index generations with blue/green
routing. Tombstones are excluded from every retrieval branch; failed runs
never displace the prior active generation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.common.provenance import ProvenanceRecord

#: Contract version for reference-document payloads and fetch specs.
REFERENCE_DOC_CONTRACT_VERSION = "1.0"

#: Fixed pgvector dimension for reference chunks (HNSW requires declared
#: dimensions). A different-dimension model needs a new migration; same
#: dimensions flow through generations. Never compare across model/version.
REFERENCE_EMBEDDING_DIMS = 1536

#: Fetch/indexing bounds.
MAX_REFERENCE_SOURCES = 10
MAX_REFERENCE_FILES = 50_000
MAX_REFERENCE_FILE_BYTES = 1_000_000
MAX_REFERENCE_CHUNKS_PER_FILE = 200
MAX_REFERENCE_CHUNK_CHARS = 8000
MAX_REFERENCE_CHUNKS_PER_RUN = 100_000
REINDEX_BATCH_SIZE = 100
MAX_INDEX_BATCHES_PER_RUN = 50


class ReferenceSourceKind(StrEnum):
    """Trusted fetcher kinds (Part 11.6)."""

    GIT = "git"
    LOCAL_PATH = "local_path"
    S3 = "s3"


class GitSourceSpec(BaseModel):
    """Git source: shallow-cloned at a pinned revision (Part 11.6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[ReferenceSourceKind.GIT] = ReferenceSourceKind.GIT
    source_version: str = Field(default="1.0", min_length=1, max_length=32)
    repo_url: str = Field(..., min_length=1, max_length=1024)
    revision: str = Field(
        default="HEAD",
        min_length=1,
        max_length=128,
        description="Immutable commit SHA preferred; branch names re-resolve per run.",
    )
    paths: list[str] = Field(default_factory=list, max_length=100)


class LocalPathSourceSpec(BaseModel):
    """Confined local-path source for development (Part 11.6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[ReferenceSourceKind.LOCAL_PATH] = ReferenceSourceKind.LOCAL_PATH
    source_version: str = Field(default="1.0", min_length=1, max_length=32)
    root: str = Field(..., min_length=1, max_length=1024)
    paths: list[str] = Field(default_factory=list, max_length=100)


class S3SourceSpec(BaseModel):
    """S3-compatible object source (Part 11.6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[ReferenceSourceKind.S3] = ReferenceSourceKind.S3
    source_version: str = Field(default="1.0", min_length=1, max_length=32)
    endpoint: str = Field(..., min_length=1, max_length=1024)
    bucket: str = Field(..., min_length=1, max_length=256)
    keys: list[str] = Field(..., min_length=1, max_length=1000)
    access_key_ref: Any | None = Field(
        default=None,
        description="SecretReference (env provider) for the access key; None uses ambient credentials.",
    )
    secret_key_ref: Any | None = Field(
        default=None,
        description="SecretReference (env provider) for the secret key; None uses ambient credentials.",
    )


ReferenceDocumentSource = Annotated[
    GitSourceSpec | LocalPathSourceSpec | S3SourceSpec,
    Field(discriminator="kind"),
]


class FetchedFile(BaseModel):
    """One fetched source file with immutable revision provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(..., min_length=1, max_length=1024)
    content: str = Field(default="", max_length=MAX_REFERENCE_FILE_BYTES)
    content_hash: str = Field(..., min_length=1, max_length=128)
    source_revision: str = Field(..., min_length=1, max_length=256)


class ChunkLifecycle(StrEnum):
    """Chunk lifecycle (Part 11.6)."""

    ACTIVE = "ACTIVE"
    TOMBSTONED = "TOMBSTONED"


class GenerationStatus(StrEnum):
    """Index generation lifecycle (Part 11.6)."""

    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    FAILED = "FAILED"


class ReferenceDocumentChunk(BaseModel):
    """One retrieval chunk of a sourced document (Part 11.6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(..., min_length=1, max_length=128)
    application_id: str | None = Field(default=None, max_length=128)
    source_id: str = Field(..., min_length=1, max_length=256)
    document_path: str = Field(..., min_length=1, max_length=1024)
    chunk_index: int = Field(default=0, ge=0)
    content: str = Field(default="", max_length=MAX_REFERENCE_CHUNK_CHARS)
    content_hash: str = Field(default="", max_length=128)
    source_revision: str = Field(default="", max_length=256)
    embedding_model: str = Field(default="", max_length=128)
    embedding_version: str = Field(default="1.0", max_length=32)
    generation: int = Field(default=1, ge=1)
    lifecycle: ChunkLifecycle = Field(default=ChunkLifecycle.ACTIVE)
    provenance: ProvenanceRecord | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class IndexGeneration(BaseModel):
    """One index generation for a source (Part 11.6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(..., min_length=1, max_length=128)
    source_id: str = Field(..., min_length=1, max_length=256)
    generation: int = Field(default=1, ge=1)
    status: GenerationStatus = Field(default=GenerationStatus.ACTIVE)
    source_revision: str = Field(default="", max_length=256)
    embedding_model: str = Field(default="", max_length=128)
    embedding_version: str = Field(default="1.0", max_length=32)
    chunk_count: int = Field(default=0, ge=0)
    provenance: ProvenanceRecord | None = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class IndexRunSummary(BaseModel):
    """Outcome of one index run (Part 11.6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(..., min_length=1, max_length=128)
    source_id: str = Field(..., min_length=1, max_length=256)
    generation: int = Field(default=1, ge=1)
    files_seen: int = Field(default=0, ge=0)
    chunks_written: int = Field(default=0, ge=0)
    tombstoned: int = Field(default=0, ge=0)
    failed: bool = Field(default=False)


class IndexStatus(BaseModel):
    """Current index status for a source (Part 11.6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(..., min_length=1, max_length=128)
    source_id: str = Field(..., min_length=1, max_length=256)
    active_generation: int = Field(default=0, ge=0)
    source_revision: str = Field(default="", max_length=256)
    chunk_count: int = Field(default=0, ge=0)
    status: GenerationStatus = Field(default=GenerationStatus.ACTIVE)


class ReferenceSearchHit(BaseModel):
    """One hybrid retrieval hit (Part 11.6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chunk_id: UUID
    source_id: str = Field(..., min_length=1, max_length=256)
    document_path: str = Field(..., min_length=1, max_length=1024)
    content: str = Field(default="", max_length=MAX_REFERENCE_CHUNK_CHARS)
    score: float = Field(default=0.0, ge=0.0)
    source_revision: str = Field(default="", max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)
