# src/investigation_agent_platform/mcp/schemas/reference.py
"""MCP output schemas for reference-document search (Part 11.6)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.reference.documents import ReferenceSearchHit


class ReferenceDocHitOutput(BaseModel):
    """One reference chunk projection (no storage keys, no tenant data)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    document_path: str = Field(..., max_length=1024)
    content: str = Field(default="", max_length=8000)
    score: float = Field(default=0.0, ge=0.0)
    source_revision: str = Field(default="", max_length=256)


class ReferenceDocsSearchOutput(BaseModel):
    """Bounded reference search page (Part 11.6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[ReferenceDocHitOutput] = Field(default_factory=list, max_length=200)
    next_cursor: str | None = Field(default=None, max_length=4096)
    has_more: bool = Field(default=False)

    @classmethod
    def from_hits(
        cls, hits: list[ReferenceSearchHit], next_cursor: str | None
    ) -> ReferenceDocsSearchOutput:
        return cls(
            items=[
                ReferenceDocHitOutput(
                    document_path=hit.document_path,
                    content=hit.content,
                    score=hit.score,
                    source_revision=hit.source_revision,
                )
                for hit in hits
            ],
            next_cursor=next_cursor,
            has_more=next_cursor is not None,
        )
