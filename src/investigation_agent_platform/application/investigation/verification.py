# src/investigation_agent_platform/application/investigation/verification.py
from datetime import UTC, datetime
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from investigation_agent_platform.domain.evidence.models import Evidence
from investigation_agent_platform.domain.hypothesis.models import Hypothesis


class ContradictionSeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Contradiction(BaseModel):
    contradiction_id: UUID = Field(default_factory=uuid4)
    statement: str
    evidence_ids: list[UUID] = Field(default_factory=list)
    hypothesis_ids: list[UUID] = Field(default_factory=list)
    severity: ContradictionSeverity
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    resolution_status: str = Field(default="UNRESOLVED")


class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    SUPPORTED = "SUPPORTED"
    CONTRADICTED = "CONTRADICTED"
    INCONCLUSIVE = "INCONCLUSIVE"


class CausalRelationship(BaseModel):
    cause: str = Field(..., description="Root trigger event or condition")
    effect: str = Field(..., description="Observed symptom or failure")
    evidence_ids: list[UUID] = Field(default_factory=list)
    mechanism: str = Field(..., description="Explicit step-by-step mechanism")
    confidence: float = Field(..., ge=0.0, le=1.0)


class InvestigationConfidence(BaseModel):
    coverage_score: float = Field(..., ge=0.0, le=1.0)
    reliability_score: float = Field(..., ge=0.0, le=1.0)
    causal_score: float = Field(..., ge=0.0, le=1.0)
    contradiction_penalty: float = Field(..., ge=0.0, le=1.0)

    @property
    def overall_confidence(self) -> float:
        score = (
            0.35 * self.coverage_score
            + 0.25 * self.reliability_score
            + 0.20 * self.causal_score
            + 0.20 * (1.0 - self.contradiction_penalty)
        )
        return max(0.0, min(1.0, score))


class RootCauseVerificationPolicy(BaseModel):
    min_confidence_threshold: float = Field(default=0.80, ge=0.0, le=1.0)
    require_cross_layer_evidence: bool = Field(default=True)
    max_allowed_unresolved_contradictions: int = Field(default=0, ge=0)
    require_causal_relationship: bool = Field(default=True)


class VerificationResult(BaseModel):
    hypothesis_id: UUID
    status: VerificationStatus
    corroborating_evidence_ids: list[UUID] = Field(default_factory=list)
    explanation: str
    confidence: InvestigationConfidence


class VerificationEngine:
    """Evaluates hypothesis validity against root cause policy requirements."""

    def evaluate_hypothesis(
        self,
        hypothesis: Hypothesis,
        evidence_items: list[Evidence],
        contradictions: list[Contradiction],
        causal_chain: list[CausalRelationship],
        policy: RootCauseVerificationPolicy,
    ) -> VerificationResult:
        unresolved = [c for c in contradictions if c.resolution_status == "UNRESOLVED"]
        if len(unresolved) > policy.max_allowed_unresolved_contradictions:
            confidence = InvestigationConfidence(
                coverage_score=0.4,
                reliability_score=0.5,
                causal_score=0.2,
                contradiction_penalty=1.0,
            )
            return VerificationResult(
                hypothesis_id=hypothesis.id,
                status=VerificationStatus.CONTRADICTED,
                corroborating_evidence_ids=[],
                explanation=f"Unresolved contradictions ({len(unresolved)}) exceed limit.",
                confidence=confidence,
            )

        sources = {e.evidence_type for e in evidence_items}
        cross_layer = len(sources) >= 2 if policy.require_cross_layer_evidence else True
        coverage = 0.9 if cross_layer else 0.5

        confidence = InvestigationConfidence(
            coverage_score=coverage,
            reliability_score=0.85,
            causal_score=0.8 if causal_chain else 0.0,
            contradiction_penalty=0.0,
        )

        status = VerificationStatus.VERIFIED if confidence.overall_confidence >= policy.min_confidence_threshold else VerificationStatus.INCONCLUSIVE
        return VerificationResult(
            hypothesis_id=hypothesis.id,
            status=status,
            corroborating_evidence_ids=[e.evidence_id for e in evidence_items],
            explanation="Evidence supports hypothesis across operational layers.",
            confidence=confidence,
        )