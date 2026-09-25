# src/investigation_agent_platform/domain/provenance/models.py
"""Provenance, lineage, and source tracking models."""

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class SourceLocation(BaseModel):
    """Identifies the precise origin location of retrieved evidence."""

    model_config = ConfigDict(frozen=True)

    system: str = Field(..., max_length=256)
    identifier: str = Field(..., max_length=512)
    file_path: str | None = Field(default=None, max_length=1024)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    revision: str | None = Field(default=None, max_length=128)


class QueryFingerprint(BaseModel):
    """Cryptographic signature and normalized summary of evidence queries."""

    model_config = ConfigDict(frozen=True)

    provider_type: str = Field(..., max_length=128)
    operation: str = Field(..., max_length=128)
    normalized_query_hash: str = Field(..., max_length=128)
    schema_version: str = Field(default="v1", max_length=32)


class EvidenceProvenance(BaseModel):
    """Complete lineage and audit trail for retrieved evidence artifacts."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., max_length=128)
    investigation_id: UUID
    action_id: UUID = Field(default_factory=uuid4)
    provider_type: str = Field(..., max_length=128)
    requested_provider_id: str = Field(..., max_length=128)
    actual_provider_id: str = Field(..., max_length=128)
    source_system: str = Field(..., max_length=256)
    retrieval_timestamp: datetime
    query_fingerprint: QueryFingerprint
    source_location: SourceLocation
    adapter_version: str = Field(default="1.0.0", max_length=32)
    fallback_occurred: bool = False


class EvidenceFreshness(BaseModel):
    """Timeliness metrics for evidence evaluation and cache staleness checks."""

    model_config = ConfigDict(frozen=True)

    observed_at: datetime
    retrieved_at: datetime
    source_last_updated_at: datetime | None = None