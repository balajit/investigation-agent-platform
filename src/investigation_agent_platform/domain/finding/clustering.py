# src/investigation_agent_platform/domain/finding/clustering.py
"""Cross-investigation finding clustering domain (Part 11.7).

Clusters group findings from many investigations into named root-cause
patterns. Membership is normalized assignment history (never embedded ID
arrays): each assignment records method, confidence, validity interval, and
provenance. Findings the assigner cannot place go to the explicit
``UNASSIGNED`` bucket — never silently dropped.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.common.provenance import ProvenanceRecord

#: Well-known bucket for findings no taxonomy member claims. Never a persisted
#: taxonomy row; assignments may reference it, taxonomy listings exclude it.
UNASSIGNED_CLUSTER_ID = "UNASSIGNED"

#: Cap: mirrors the proven batch size from the reference implementation.
DEFAULT_CLUSTER_BATCH_SIZE = 25

#: Cap: maximum taxonomy members before the assigner must merge.
MAX_TAXONOMY_CLUSTERS = 25


class FindingCluster(BaseModel):
    """One named root-cause pattern within a tenant's taxonomy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(..., min_length=1, max_length=128)
    cluster_key: str = Field(..., min_length=1, max_length=16)
    label: str = Field(..., min_length=1, max_length=128)
    description: str = Field(default="", max_length=512)
    taxonomy_revision: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class FindingClusterAssignment(BaseModel):
    """One finding's membership in a cluster, versioned as history."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(..., min_length=1, max_length=128)
    finding_id: UUID
    cluster_id: UUID | None = Field(
        default=None,
        description="None means the UNASSIGNED bucket (no taxonomy row).",
    )
    method: str = Field(default="llm-assign", min_length=1, max_length=64)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    taxonomy_revision: int = Field(default=1, ge=1)
    valid_from: datetime = Field(default_factory=lambda: datetime.now(UTC))
    valid_to: datetime | None = Field(default=None)
    provenance: ProvenanceRecord | None = Field(default=None)


class FindingEmbedding(BaseModel):
    """One finding's retrieval vectors, one row per (model, generation)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: UUID
    tenant_id: str = Field(..., min_length=1, max_length=128)
    embedding_model: str = Field(..., min_length=1, max_length=128)
    embedding_version: str = Field(default="1.0", min_length=1, max_length=32)
    generation: int = Field(default=1, ge=1)
    vector: list[float] = Field(default_factory=list, max_length=4096)
    lexical_text: str = Field(default="", max_length=4096)
    lifecycle: str = Field(default="ACTIVE", min_length=1, max_length=32)
    provenance: ProvenanceRecord | None = Field(default=None)


class ClusterAssignmentResult(BaseModel):
    """Validated LLM assignment for one finding (Part 11.7)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: UUID
    cluster_key: str = Field(..., min_length=1, max_length=16)
    new_cluster: dict[str, Any] | None = Field(default=None)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
