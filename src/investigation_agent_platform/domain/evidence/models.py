# src/investigation_agent_platform/domain/evidence/models.py
"""Evidence domain specifications and relationship graph models."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from investigation_agent_platform.domain.evidence.completeness import EvidenceDataQuality
from investigation_agent_platform.domain.provenance.models import (
    EvidenceFreshness,
    EvidenceProvenance,
)


class ClassificationLevel(StrEnum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"
    SECRET = "SECRET"


class LogSeverity(StrEnum):
    """Closed severity vocabulary for runtime evidence filtering.

    Unknown values are rejected at the request boundary (never silently
    dropped): an invalid severity must not broaden a query into an unfiltered
    one. Values mirror the MCP `severities` enum contract.
    """

    TRACE = "TRACE"
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    WARNING = "WARNING"
    ERROR = "ERROR"
    FATAL = "FATAL"


class EvidenceType(StrEnum):
    RUNTIME_LOG = "RUNTIME_LOG"
    RUNTIME_TRACE = "RUNTIME_TRACE"
    LOG = "LOG"
    TRACE = "TRACE"
    DATABASE_STATE = "DATABASE_STATE"
    SOURCE_CODE = "SOURCE_CODE"
    COMMIT_HISTORY = "COMMIT_HISTORY"
    CALL_GRAPH = "CALL_GRAPH"
    EXCEPTION_PATH = "EXCEPTION_PATH"
    METRIC = "METRIC"
    DOCUMENTATION = "DOCUMENTATION"
    SYSTEM_EVENT = "SYSTEM_EVENT"
    API_PROBE = "API_PROBE"
    # D4: machine-checked divergence (expected-vs-actual diff, assertion
    # FAIL). Citations for silent-failure claims; absence verified in code.
    DIVERGENCE = "DIVERGENCE"
    # G-1: journey spine / step evidence (ordered session→tracking-ID
    # service timeline). Navigation + orientation; steps cite their own
    # rows for verification.
    JOURNEY = "JOURNEY"


class EvidenceRelationshipType(StrEnum):
    DERIVED_FROM = "DERIVED_FROM"
    CORROBORATES = "CORROBORATES"
    CONTRADICTS = "CONTRADICTS"
    RELATES_TO = "RELATES_TO"
    CAUSED_BY = "CAUSED_BY"
    OCCURRED_IN = "OCCURRED_IN"
    BELONGS_TO = "BELONGS_TO"
    REFERENCES = "REFERENCES"
    IMPLEMENTS = "IMPLEMENTS"
    CHANGED_BY = "CHANGED_BY"
    PRECEDES = "PRECEDES"
    FOLLOWS = "FOLLOWS"
    # §2 similar-past-investigation protocol (TO_DO_LLM_List): investigation-
    # level links written by the deterministic disposition step, never by the
    # LLM. Node IDs use the `investigation:<uuid>` shape.
    DUPLICATE_OF = "DUPLICATE_OF"
    FOLLOW_UP_OF = "FOLLOW_UP_OF"
    RETRY_OF = "RETRY_OF"
    POSSIBLY_RELATED = "POSSIBLY_RELATED"


class RedactionEntry(BaseModel):
    """Audit entry for sensitive information scrubbed at ingress."""

    model_config = ConfigDict(frozen=True)

    redaction_type: str = Field(..., max_length=64)
    original_length: int = Field(..., ge=0)
    replacement_token: str = Field(..., max_length=64)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EntityReference(BaseModel):
    """Reference to an entity associated with evidence."""

    model_config = ConfigDict(frozen=True)

    entity_type: str = Field(..., max_length=128)
    entity_id: str = Field(..., max_length=256)


class LargeEvidencePointer(BaseModel):
    """Pointer to a large payload offloaded to tier-2 object storage."""

    model_config = ConfigDict(frozen=True)

    content_pointer: str = Field(..., max_length=1024)
    payload_size_bytes: int = Field(..., ge=0)
    summary_snippet: str = Field(..., max_length=2048)


class Evidence(BaseModel):
    """Normalized evidence aggregate retrieved from systems under test."""

    model_config = ConfigDict(frozen=True)

    evidence_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(..., max_length=128)
    investigation_id: UUID
    evidence_type: EvidenceType
    provider: str = Field(..., max_length=128)
    source: str = Field(..., max_length=512)
    title: str = Field(..., max_length=256)
    summary: str = Field(..., max_length=2048)
    content_snippet: str = Field(default="", max_length=4096)
    attributes: dict[str, Any] = Field(default_factory=dict)
    # Nullable since Part 9: a missing/invalid provider timestamp must never be
    # fabricated from retrieval time. `None` means observation time unknown;
    # consumers must consult provenance/freshness data-quality state.
    observed_at: datetime | None
    retrieved_at: datetime
    provenance: EvidenceProvenance
    freshness: EvidenceFreshness
    classification: ClassificationLevel = ClassificationLevel.INTERNAL
    # Part 9: interpretation-relevant quality flags (why observed_at is None,
    # whether attributes were reduced). Empty in the common clean case.
    data_quality: list[EvidenceDataQuality] = Field(default_factory=list, max_length=10)
    is_redacted: bool = False
    fingerprint: str = Field(..., max_length=128)
    content_uri: str | None = Field(default=None, max_length=1024)
    relevance: float = Field(default=1.0, ge=0.0, le=1.0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    redaction_manifest: list[RedactionEntry] = Field(default_factory=list, max_length=50)
    large_payload_pointer: LargeEvidencePointer | None = None

    @model_validator(mode="after")
    def validate_redaction_and_payload_invariants(self) -> "Evidence":
        attrs = self.attributes if isinstance(self.attributes, dict) else {}
        fact = attrs.get("code_fact_resolution")
        if fact is not None:
            from investigation_agent_platform.domain.code.models import CodeFactResolution

            try:
                CodeFactResolution.model_validate(fact)
            except Exception as exc:
                raise ValueError("code_fact_resolution has an invalid shape") from exc
        fact_v2 = attrs.get("code_fact_resolution_v2")
        if fact_v2 is not None:
            from investigation_agent_platform.domain.code.models import CodeFactResolutionV2

            try:
                CodeFactResolutionV2.model_validate(fact_v2)
            except Exception as exc:
                raise ValueError("code_fact_resolution_v2 has an invalid shape") from exc
        if fact is not None and fact_v2 is not None:
            from investigation_agent_platform.domain.code.models import (
                detect_dual_resolution_conflict,
            )

            conflict = detect_dual_resolution_conflict(fact, fact_v2)
            if conflict is not None:
                raise ValueError(f"conflicting dual code-fact payloads: {conflict}")
        if self.is_redacted and not self.redaction_manifest:
            raise ValueError("is_redacted is True but redaction_manifest is empty.")
        if len(self.content_snippet) >= 4096 and not (
            self.content_uri or self.large_payload_pointer
        ):
            raise ValueError(
                "Large content snippet must have externalized content_uri or large_payload_pointer."
            )
        return self


class EvidencePage(BaseModel):
    """Paged result set returned by evidence discovery gateway operations."""

    model_config = ConfigDict(frozen=True)

    items: list[Evidence] = Field(default_factory=list, max_length=100)
    # Signed pagination cursors are base64url tokens that exceed 256 chars.
    next_cursor: str | None = Field(default=None, max_length=4096)
    has_more: bool = False
    total_count: int | None = Field(default=None, ge=0)


class EvidenceReference(BaseModel):
    """Lightweight handle referencing raw evidence payload location."""

    model_config = ConfigDict(frozen=True)

    evidence_id: UUID
    provider: str = Field(..., max_length=128)
    location: str = Field(..., max_length=1024)
    snippet: str = Field(..., max_length=2048)


class EvidenceRelationship(BaseModel):
    """Directed edge between evidence nodes forming evidence correlation graph."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(..., max_length=128)
    investigation_id: UUID
    source_evidence_id: UUID
    target_evidence_id: UUID
    relationship_type: EvidenceRelationshipType
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    created_by: str = Field(default="SYSTEM", max_length=128)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LogExemplar(BaseModel):
    """Persisted log group: presentation twin of banked raw rows.

    `member_evidence_ids` are EXACTLY the raw rows' evidence IDs (verbatim
    UUIDs) so humans match groups to rows by direct equality. Exemplars are
    never citations: verification binds claims to raw member IDs only.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(..., max_length=128)
    investigation_id: UUID
    signature: str = Field(..., max_length=4096)
    member_evidence_ids: list[UUID] = Field(default_factory=list, max_length=500)
    count: int = Field(default=1, ge=1)
    span_first: datetime | None = None
    span_last: datetime | None = None
    representative_snippet: str = Field(default="", max_length=4096)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# G6: evidence types that are repository-scoped reference data (shared across
# investigations by content identity) rather than investigation-scoped
# observations. Everything else is observational (owned + cascade-purged).
REFERENCE_LIFECYCLE_TYPES = frozenset({"SOURCE_CODE", "COMMIT_HISTORY"})
