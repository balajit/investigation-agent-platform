# src/investigation_agent_platform/domain/hypothesis/models.py
"""Hypothesis, verification, and assessment models."""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HypothesisStatus(StrEnum):
    PROPOSED = "PROPOSED"
    UNDER_INVESTIGATION = "UNDER_INVESTIGATION"
    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class AssessmentType(StrEnum):
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    NEUTRAL = "NEUTRAL"


class AssessmentStrength(StrEnum):
    WEAK = "WEAK"
    MODERATE = "MODERATE"
    STRONG = "STRONG"
    DECISIVE = "DECISIVE"


class HypothesisEvidenceAssessment(BaseModel):
    """Assessment linking evidence evaluation to hypothesis status."""

    model_config = ConfigDict(frozen=True)

    hypothesis_id: UUID
    evidence_id: UUID
    assessment: AssessmentType
    strength: AssessmentStrength
    reason: str = Field(..., max_length=1024)


class Hypothesis(BaseModel):
    """Candidate explanation formulated during investigation."""

    model_config = ConfigDict(frozen=True)

    id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(..., max_length=128)
    investigation_id: UUID
    title: str = Field(..., max_length=256)
    statement: str = Field(..., max_length=2048)
    description: str = Field(..., max_length=4096)
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    confidence_score: float = Field(default=0.5, ge=0.0, le=1.0)
    support_score: float = Field(default=0.5, ge=0.0, le=1.0)
    supporting_evidence_ids: list[UUID] = Field(default_factory=list, max_length=200)
    refuting_evidence_ids: list[UUID] = Field(default_factory=list, max_length=200)
    assessments: list[HypothesisEvidenceAssessment] = Field(default_factory=list, max_length=100)
    required_verification: list[str] = Field(default_factory=list, max_length=20)
    parent_hypothesis_id: UUID | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_assessment_consistency(self) -> "Hypothesis":
        for asm in self.assessments:
            if (
                asm.assessment == AssessmentType.SUPPORTS
                and asm.evidence_id not in self.supporting_evidence_ids
            ):
                raise ValueError(
                    f"Supporting assessment evidence {asm.evidence_id} missing from supporting_evidence_ids."
                )
            if (
                asm.assessment == AssessmentType.CONTRADICTS
                and asm.evidence_id not in self.refuting_evidence_ids
            ):
                raise ValueError(
                    f"Refuting assessment evidence {asm.evidence_id} missing from refuting_evidence_ids."
                )
        return self

    def transition_status(self, new_status: HypothesisStatus) -> "Hypothesis":
        """Validate state transition graph for hypothesis lifecycle."""
        valid_transitions: dict[HypothesisStatus, set[HypothesisStatus]] = {
            HypothesisStatus.PROPOSED: {
                HypothesisStatus.UNDER_INVESTIGATION,
                HypothesisStatus.REJECTED,
            },
            HypothesisStatus.UNDER_INVESTIGATION: {
                HypothesisStatus.SUPPORTED,
                HypothesisStatus.CONTRADICTED,
                HypothesisStatus.REJECTED,
            },
            HypothesisStatus.SUPPORTED: {
                HypothesisStatus.VERIFIED,
                HypothesisStatus.CONTRADICTED,
                HypothesisStatus.REJECTED,
            },
            HypothesisStatus.CONTRADICTED: {HypothesisStatus.REJECTED, HypothesisStatus.SUPPORTED},
            HypothesisStatus.VERIFIED: set(),
            HypothesisStatus.REJECTED: set(),
        }
        allowed = valid_transitions.get(self.status, set())
        if new_status not in allowed:
            raise ValueError(
                f"Invalid hypothesis status transition from {self.status} to {new_status}"
            )
        return self.model_copy(update={"status": new_status, "updated_at": datetime.now(UTC)})
