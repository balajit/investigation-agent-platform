# src/investigation_agent_platform/mcp/schemas/common.py
"""Shared MCP output models (Part 9 Phase 6).

Projections of domain evidence for agent consumption: identity and
provenance are preserved, infrastructure internals (tenant IDs, index
names, credentials, raw request bodies) are never exposed. The ``source``
field is a safe provider label, not the internal source URI.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.evidence.completeness import (
    CompletenessReason,
    EvidenceCompleteness,
)
from investigation_agent_platform.domain.evidence.models import Evidence
from investigation_agent_platform.domain.provenance.models import EvidenceProvenance

_SAFE_SOURCE_LABELS = {"ELASTIC": "runtime", "MCP": "mcp"}


def safe_source_label(provider: str) -> str:
    """Map a provider name to its agent-safe source label."""
    return _SAFE_SOURCE_LABELS.get(provider, provider.lower() or "unknown")


class McpProvenance(BaseModel):
    """Provenance subset safe for agent exposure."""

    model_config = ConfigDict(frozen=True)

    evidence_id: UUID
    provider: str = Field(..., min_length=1, max_length=100)
    provider_record_id: str | None = Field(..., max_length=500)
    source: str | None = Field(..., max_length=500)
    observed_at: datetime | None
    retrieved_at: datetime
    query_fingerprint: str | None = Field(..., max_length=128)

    @classmethod
    def from_domain(cls, provenance: EvidenceProvenance, evidence: Evidence) -> "McpProvenance":
        return cls(
            evidence_id=evidence.evidence_id,
            provider=provenance.provider_type,
            provider_record_id=evidence.fingerprint,
            source=safe_source_label(evidence.provider),
            observed_at=evidence.observed_at,
            retrieved_at=provenance.retrieval_timestamp,
            query_fingerprint=provenance.query_fingerprint.normalized_query_hash,
        )


class McpCompleteness(BaseModel):
    """Completeness subset safe for agent exposure."""

    model_config = ConfigDict(frozen=True)

    complete: bool
    truncated: bool
    reason: CompletenessReason | None
    returned_count: int = Field(..., ge=0)
    examined_count: int = Field(..., ge=0)

    @classmethod
    def from_domain(cls, completeness: EvidenceCompleteness) -> "McpCompleteness":
        return cls(
            complete=completeness.complete,
            truncated=completeness.truncated,
            reason=completeness.reason,
            returned_count=completeness.returned_count,
            examined_count=completeness.examined_count,
        )


class McpEvidenceItem(BaseModel):
    """One evidence record projected for agent consumption."""

    model_config = ConfigDict(frozen=True)

    evidence_id: UUID
    evidence_type: str = Field(..., min_length=1, max_length=100)
    summary: str = Field(..., max_length=2000)
    observed_at: datetime | None
    attributes: dict[str, Any]
    provenance: McpProvenance

    @classmethod
    def from_domain(cls, evidence: Evidence) -> "McpEvidenceItem":
        return cls(
            evidence_id=evidence.evidence_id,
            evidence_type=evidence.evidence_type.value,
            summary=evidence.summary,
            observed_at=evidence.observed_at,
            attributes=dict(evidence.attributes),
            provenance=McpProvenance.from_domain(evidence.provenance, evidence),
        )
