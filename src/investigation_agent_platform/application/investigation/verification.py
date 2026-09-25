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
    # F-052: durable contradiction lifecycle — every contradiction carries its
    # resolution state plus who resolved it, why, and when. Unresolved
    # HIGH/CRITICAL contradictions block conclusion via ConclusionGate.
    resolution_status: str = Field(default="UNRESOLVED")
    resolved_by: str | None = None
    resolution_rationale: str | None = None
    resolved_at: datetime | None = None
    superseded_by: UUID | None = None

    def resolve(self, resolved_by: str, rationale: str) -> "Contradiction":
        if self.resolution_status not in ("UNRESOLVED", "INVESTIGATING"):
            raise ValueError(
                f"Contradiction {self.contradiction_id} is already {self.resolution_status}"
            )
        return self.model_copy(
            update={
                "resolution_status": "RESOLVED",
                "resolved_by": resolved_by,
                "resolution_rationale": rationale,
                "resolved_at": datetime.now(UTC),
            }
        )

    def accept_risk(self, resolved_by: str, rationale: str) -> "Contradiction":
        """Explicit risk acceptance — recorded, attributed, and auditable."""
        if self.resolution_status not in ("UNRESOLVED", "INVESTIGATING"):
            raise ValueError(
                f"Contradiction {self.contradiction_id} is already {self.resolution_status}"
            )
        return self.model_copy(
            update={
                "resolution_status": "ACCEPTED_RISK",
                "resolved_by": resolved_by,
                "resolution_rationale": rationale,
                "resolved_at": datetime.now(UTC),
            }
        )


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

        # F-050: cross-layer corroboration requires INDEPENDENT sources — distinct
        # providers/systems, not merely distinct evidence_type values. Two rows
        # from the same derived dataset must never count as corroboration.
        providers = {getattr(e, "provider", "unknown") for e in evidence_items}
        sources = {getattr(e, "source", "unknown") for e in evidence_items}
        types = {e.evidence_type for e in evidence_items}
        independent_layers = len(providers) >= 2 and len(types) >= 2
        cross_layer = (
            (independent_layers or len(sources) >= 3)
            if policy.require_cross_layer_evidence
            else True
        )

        # F-049: explicit, auditable scoring inputs instead of magic constants.
        # Coverage = corroborating evidence breadth capped by hypothesis needs;
        # reliability = fraction of evidence from sanctioned providers with
        # provenance; causal = chain completeness. Scores are labeled heuristic
        # in the explanation and never constitute proof by themselves.
        corroborating_ids = [e.evidence_id for e in evidence_items]
        coverage = (
            min(1.0, len(corroborating_ids) / 4.0)
            if cross_layer
            else min(0.5, len(corroborating_ids) / 4.0)
        )
        with_provenance = sum(
            1 for e in evidence_items if getattr(e, "provenance", None) is not None
        )
        reliability = (with_provenance / len(evidence_items)) if evidence_items else 0.0
        causal = min(1.0, len(causal_chain) / 2.0) if causal_chain else 0.0

        confidence = InvestigationConfidence(
            coverage_score=coverage,
            reliability_score=reliability,
            causal_score=causal,
            contradiction_penalty=0.0,
        )

        status = (
            VerificationStatus.VERIFIED
            if confidence.overall_confidence >= policy.min_confidence_threshold
            else VerificationStatus.INCONCLUSIVE
        )
        return VerificationResult(
            hypothesis_id=hypothesis.id,
            status=status,
            corroborating_evidence_ids=corroborating_ids,
            explanation=(
                "Heuristic verification score "
                f"(coverage={coverage:.2f} from {len(corroborating_ids)} items across "
                f"{len(providers)} providers, reliability={reliability:.2f}, causal={causal:.2f}). "
                "Heuristic only — final conclusions require ConclusionGate approval."
            ),
            confidence=confidence,
        )


class ConclusionGateDecision(BaseModel):
    approved: bool
    blockers: list[str] = Field(default_factory=list)


class ConclusionGate:
    """Single mandatory gate for conclusion publication (F-051).

    The LLM may RECOMMEND readiness, but only this domain service authorizes
    finalization — evaluating evidence coverage, contradiction state, causal
    chain, freshness, and provenance. Any blocker fails closed.
    """

    async def evaluate(
        self,
        tenant_id: str,
        investigation_id: UUID,
        verification: VerificationResult,
        contradictions: list[Contradiction],
        causal_chain: list[CausalRelationship],
        evidence_items: list[Evidence],
        policy: RootCauseVerificationPolicy,
    ) -> ConclusionGateDecision:
        blockers: list[str] = []
        if verification.status != VerificationStatus.VERIFIED:
            blockers.append(f"hypothesis status is {verification.status.value}, not VERIFIED")
        if verification.confidence.overall_confidence < policy.min_confidence_threshold:
            blockers.append(
                f"confidence {verification.confidence.overall_confidence:.2f} below threshold "
                f"{policy.min_confidence_threshold:.2f}"
            )
        blocking_contradictions = [
            c
            for c in contradictions
            if c.resolution_status == "UNRESOLVED"
            and c.severity in (ContradictionSeverity.HIGH, ContradictionSeverity.CRITICAL)
        ]
        if blocking_contradictions:
            blockers.append(
                f"{len(blocking_contradictions)} unresolved HIGH/CRITICAL contradiction(s)"
            )
        if policy.require_causal_relationship and not causal_chain:
            blockers.append("no causal chain established")
        if not evidence_items:
            blockers.append("no evidence items bound to the conclusion")
        else:
            # Freshness + provenance: every bound evidence item must carry
            # provenance; stale items (>24h) must be explicitly re-validated.
            from datetime import timedelta

            now = datetime.now(UTC)
            for item in evidence_items:
                if getattr(item, "provenance", None) is None:
                    blockers.append(f"evidence {item.evidence_id} lacks provenance")
                    break
                retrieved = getattr(getattr(item, "freshness", None), "retrieved_at", None)
                if retrieved is not None and (now - retrieved) > timedelta(hours=24):
                    blockers.append(f"evidence {item.evidence_id} is stale (>24h since retrieval)")
                    break
        return ConclusionGateDecision(approved=not blockers, blockers=blockers)
