# src/investigation_agent_platform/domain/finding/models.py
"""Finding aggregates and terminal conclusion models."""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FindingType(StrEnum):
    ROOT_CAUSE = "ROOT_CAUSE"
    CONTRIBUTING_FACTOR = "CONTRIBUTING_FACTOR"
    ANOMALY = "ANOMALY"
    OBSERVATION = "OBSERVATION"


class SeverityLevel(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class ConclusionStatus(StrEnum):
    ROOT_CAUSE_ESTABLISHED = "ROOT_CAUSE_ESTABLISHED"
    ROOT_CAUSE_LIKELY = "ROOT_CAUSE_LIKELY"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NO_ROOT_CAUSE_FOUND = "NO_ROOT_CAUSE_FOUND"


class Finding(BaseModel):
    """Verified finding aggregate backed by causal evidence."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(..., max_length=128)
    investigation_id: UUID
    finding_type: FindingType
    title: str = Field(..., max_length=256)
    statement: str = Field(..., max_length=2048)
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=100)
    related_hypothesis_ids: list[UUID] = Field(default_factory=list, max_length=50)
    causal_chain: list[UUID] = Field(default_factory=list, max_length=50)
    severity: SeverityLevel = SeverityLevel.MEDIUM
    remediation_steps: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_root_cause_requirements(self) -> "Finding":
        if self.finding_type == FindingType.ROOT_CAUSE:
            if not self.evidence_ids:
                raise ValueError("ROOT_CAUSE finding must be backed by at least one evidence item.")
            if not self.causal_chain:
                raise ValueError("ROOT_CAUSE finding must specify a non-empty causal_chain.")
        return self


class InvestigationConclusion(BaseModel):
    """Terminal investigation report and root cause conclusion."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., max_length=128)
    investigation_id: UUID
    status: ConclusionStatus
    root_cause: str | None = Field(default=None, max_length=2048)
    supporting_evidence_ids: list[UUID] = Field(default_factory=list, max_length=200)
    supporting_hypothesis_ids: list[UUID] = Field(default_factory=list, max_length=50)
    contradicting_evidence_ids: list[UUID] = Field(default_factory=list, max_length=200)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    limitations: list[str] = Field(default_factory=list, max_length=20)
    recommended_next_steps: list[str] = Field(default_factory=list, max_length=20)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_conclusion_invariants(self) -> "InvestigationConclusion":
        if self.status == ConclusionStatus.ROOT_CAUSE_ESTABLISHED:
            if not self.root_cause:
                raise ValueError("ROOT_CAUSE_ESTABLISHED requires a detailed root cause statement.")
            if not self.supporting_evidence_ids:
                raise ValueError("ROOT_CAUSE_ESTABLISHED requires supporting evidence IDs.")
        return self
