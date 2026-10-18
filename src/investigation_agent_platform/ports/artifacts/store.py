# src/investigation_agent_platform/ports/artifacts/store.py
"""Artifact storage port: report/render outputs and large payloads (Part 11.3A).

The adapter — never the caller — constructs tenant/application key prefixes
from the scope. Callers pass logical keys only; absolute keys, traversal
segments, and cross-tenant prefixes are rejected by every implementation.
"""

from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.common.extension import CapabilityScope


class ArtifactRef(BaseModel):
    """Opaque pointer to one stored artifact (Part 11.3A)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: UUID
    key: str = Field(..., min_length=1, max_length=1024)
    contract_version: str = Field(default="1.0", min_length=1, max_length=32)
    content_digest: str = Field(default="", max_length=128)
    content_type: str = Field(default="application/octet-stream", max_length=128)
    size_bytes: int = Field(default=0, ge=0)
    retention_class: str = Field(default="default", max_length=64)


class ArtifactObject(BaseModel):
    """Retrieved artifact bytes plus metadata (Part 11.3A)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: ArtifactRef
    content: bytes = Field(default=b"", max_length=500_000_000)
    metadata: dict[str, str] = Field(default_factory=dict, max_length=20)


class ArtifactPage(BaseModel):
    """Cursor-paginated artifact listing (Part 11.3A)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[ArtifactRef] = Field(default_factory=list, max_length=200)
    next_cursor: str | None = Field(default=None, max_length=4096)
    has_more: bool = Field(default=False)


@runtime_checkable
class ArtifactStorePort(Protocol):
    """Tenant-scoped object storage for rendered artifacts (Part 11.3A)."""

    async def put(
        self,
        scope: CapabilityScope,
        key: str,
        content: bytes,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> ArtifactRef: ...

    async def get(self, scope: CapabilityScope, ref: ArtifactRef) -> ArtifactObject: ...

    async def list(
        self,
        scope: CapabilityScope,
        prefix: str,
        cursor: str | None = None,
        limit: int = 50,
    ) -> ArtifactPage: ...

    async def delete(self, scope: CapabilityScope, ref: ArtifactRef) -> None: ...

    async def signed_url(
        self, scope: CapabilityScope, ref: ArtifactRef, ttl_seconds: int = 3600
    ) -> str: ...
