# src/investigation_agent_platform/mcp/schemas/evidence.py
"""Evidence search/retrieval MCP output models (Part 9 Phase 6)."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.application.investigation.runtime_evidence import (
    RuntimeEvidenceSearchResult,
)
from investigation_agent_platform.domain.evidence.models import Evidence
from investigation_agent_platform.mcp.schemas.common import (
    McpCompleteness,
    McpEvidenceItem,
    McpProvenance,
)


class RuntimeEvidenceSearchOutput(BaseModel):
    """Bounded search page with pagination and completeness state."""

    model_config = ConfigDict(frozen=True)

    items: list[McpEvidenceItem] = Field(..., max_length=100)
    next_cursor: str | None = Field(..., max_length=4096)
    has_more: bool
    total_count: int | None = Field(..., ge=0)
    completeness: McpCompleteness

    @classmethod
    def from_domain(cls, result: RuntimeEvidenceSearchResult) -> "RuntimeEvidenceSearchOutput":
        return cls(
            items=[McpEvidenceItem.from_domain(item) for item in result.items],
            next_cursor=result.next_cursor,
            has_more=result.has_more,
            total_count=result.total_count,
            completeness=McpCompleteness.from_domain(result.completeness),
        )


class EvidenceDetailOutput(BaseModel):
    """One evidence record with provenance for agent inspection."""

    model_config = ConfigDict(frozen=True)

    evidence_id: UUID
    evidence_type: str = Field(..., min_length=1, max_length=100)
    summary: str = Field(..., max_length=4000)
    observed_at: datetime | None
    attributes: dict[str, Any]
    provenance: McpProvenance

    @classmethod
    def from_domain(cls, evidence: Evidence) -> "EvidenceDetailOutput":
        return cls(
            evidence_id=evidence.evidence_id,
            evidence_type=evidence.evidence_type.value,
            summary=evidence.summary,
            observed_at=evidence.observed_at,
            attributes=dict(evidence.attributes),
            provenance=McpProvenance.from_domain(evidence.provenance, evidence),
        )
