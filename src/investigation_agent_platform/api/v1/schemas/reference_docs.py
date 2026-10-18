# src/investigation_agent_platform/api/v1/schemas/reference_docs.py
"""Response models for the reference-docs router (Part 11.6).

NOTE: rollback returns the refreshed IndexStatus (not the generation row) —
there is deliberately no IndexGeneration view: schemas mirror wire shapes.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ReferenceSourceView(BaseModel):
    """One configured source. Not a secret (id, kind, owning app only)."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    kind: str
    application_id: str


class IndexStatusView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    source_id: str
    active_generation: int
    source_revision: str
    chunk_count: int
    status: str


class ReferenceSearchHitView(BaseModel):
    """One hybrid retrieval hit. `content` is already-served data, not a secret."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: UUID
    source_id: str
    document_path: str
    content: str
    score: float
    source_revision: str
    metadata: dict[str, Any]


class ReindexAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    workflow_id: str
    status: str = Field(description="BackgroundJobStatus value, e.g. QUEUED.")


class SourceDeleteAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    status: str = Field(description="Always 'DELETED'.")
    rows_removed: int


class ReindexBody(BaseModel):
    """POST /reference-docs/sources/{source_id}/reindex request body."""

    application_id: str | None = Field(default=None, max_length=128, description="Owning application profile identifier")
    source_id: str = Field(..., min_length=1, max_length=256, description="Reference source to reindex")


class RollbackBody(BaseModel):
    """POST /reference-docs/sources/{source_id}/rollback request body."""

    application_id: str | None = Field(default=None, max_length=128, description="Owning application profile identifier")
    source_id: str = Field(..., min_length=1, max_length=256, description="Reference source to roll back")
    generation: int = Field(..., ge=1, description="Generation number to restore as active")
