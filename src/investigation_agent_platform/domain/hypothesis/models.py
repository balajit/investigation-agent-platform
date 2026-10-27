# src/investigation_agent_platform/domain/hypothesis/models.py
"""Hypothesis, verification, and assessment models."""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from investigation_agent_platform.domain.code.models import JavaFactCategory


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


class ClaimKind(StrEnum):
    """What a hypothesis asserts — selects the verification rule (Phase 2).

    DEFINITION: "symbol S is defined/implemented in file F" — needs a range
        read whose window contains S.
    CALL_EDGE: "caller C calls callee S" / "callers of S" — needs a call site.
    BEHAVIOR: "the code does X at runtime" — needs code AND runtime evidence.
    ABSENCE: "X does not exist / is never called" — needs a scoped, empty
        search proof; an empty result alone is never proof.
    HISTORY: "file F changed in commit C" — needs a matching COMMIT_HISTORY row.
    CAUSAL: root-cause mechanism — the existing causal-chain path.
    NOT_CHECKABLE: model-declared unfalsifiable (taste/preference/value
        judgment, or a claim no observable could ever settle). Predictions
        that name an observable outcome stay checkable even without a
        current code anchor. Never retrieves, never grounds, never
        obligates; excluded from checkable denominators (Phase 4).
    """

    DEFINITION = "DEFINITION"
    CALL_EDGE = "CALL_EDGE"
    BEHAVIOR = "BEHAVIOR"
    ABSENCE = "ABSENCE"
    HISTORY = "HISTORY"
    CAUSAL = "CAUSAL"
    # Phase 4: model-declared unfalsifiable. Never retrieves, never
    # grounds, never obligates; excluded from checkable denominators.
    NOT_CHECKABLE = "NOT_CHECKABLE"


class ClaimTarget(BaseModel):
    """Typed subject of a claim so verifiers need not re-parse prose.

    ``symbol`` is the subject (defined symbol, or the callee of a call edge);
    ``counterpart`` is the other side of a relation (the caller); ``scope``
    names what an absence claim searched (path prefix / repository@revision).
    """

    model_config = ConfigDict(frozen=True)

    symbol: str | None = Field(default=None, max_length=256)
    counterpart: str | None = Field(default=None, max_length=256)
    file_path: str | None = Field(default=None, max_length=512)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    scope: str | None = Field(default=None, max_length=512)
    declaring_type: str | None = Field(default=None, max_length=512)
    commit_id: str | None = Field(default=None, max_length=64)
    # Phase 1 (claim binding): structured commit subject the HISTORY claim
    # asserts. Compared deterministically against the cited COMMIT_HISTORY
    # row summary; never matched by substring against freeform model prose.
    # None = legacy/unstructured subject (pre-Phase-1 rows and untyped
    # claims); readers must treat absence as "unknown", never guess.
    commit_subject: str | None = Field(default=None, max_length=512)
    value_semantics: str | None = Field(default=None, max_length=64)
    claimed_value: str | None = Field(default=None, max_length=512)
    fact_category: JavaFactCategory | None = Field(default=None)


class HypothesisEvidenceAssessment(BaseModel):
    """Assessment linking evidence evaluation to hypothesis status.

    Phase 2.1: ``claim_kind``/``claim_target`` bind a cited row to the
    specific claim it supports. They default to None so rows persisted in
    ``assessments_json`` before this change still validate unchanged.
    """

    model_config = ConfigDict(frozen=True)

    hypothesis_id: UUID
    # Unresolved obligations have no evidence row yet. ``None`` is explicit
    # absence, never a fabricated sentinel evidence ID.
    evidence_id: UUID | None = None
    assessment: AssessmentType
    strength: AssessmentStrength
    reason: str = Field(..., max_length=1024)
    claim_kind: ClaimKind | None = None
    claim_target: ClaimTarget | None = None
    commit_id: str | None = Field(default=None, max_length=64)
    # Phase 1 (claim binding): denormalized mirror of
    # ``claim_target.commit_subject`` so verifiers need not chase the target
    # object. None = unstructured legacy subject.
    commit_subject: str | None = Field(default=None, max_length=512)
    # Phase 2 (claim binding): stored retrieval hint carried from
    # DecisionClaim at FORMULATE. None = legacy/omitted (derived fallback).
    # Never proof, never identity.
    search_query: str | None = Field(default=None, max_length=1024)
    search_scope: str | None = Field(default=None, max_length=512)
    value_semantics: str | None = Field(default=None, max_length=64)
    claimed_value: str | None = Field(default=None, max_length=512)
    fact_category: JavaFactCategory | None = Field(default=None)
    # WP-A2: diagnostics schema version that wrote this assessment
    # (None = legacy/pre-diagnostics row). Readers must treat None as
    # "unknown" — never fabricate a version for old rows.
    schema_version: str | None = Field(default=None, max_length=16)
    # WP-A2: stable DiagnosticReasonCode value explaining this
    # assessment's role (None = legacy/untyped). Free-form str, not the
    # enum, so persisted rows never fail on unknown future codes.
    reason_code: str | None = Field(default=None, max_length=64)


class HypothesisCausalStep(BaseModel):
    """One validated causal link proposed for a hypothesis (P0-3).

    Cause/mechanism/effect text comes from the model; evidence_ids must
    resolve to this investigation's evidence rows (validated at write).
    """

    model_config = ConfigDict(frozen=True)

    cause: str = Field(..., max_length=1024)
    mechanism: str = Field(..., max_length=2048)
    effect: str = Field(..., max_length=1024)
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=20)


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
    causal_steps: list[HypothesisCausalStep] = Field(default_factory=list, max_length=20)
    parent_hypothesis_id: UUID | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    version: int = Field(
        default=1,
        ge=1,
        description="Optimistic-concurrency version (WP-C2). Saves compare against "
        "expected_version and bump by exactly one; new rows start at 1.",
    )

    @property
    def claim_kind(self) -> ClaimKind | None:
        """Typed claim kind recorded at formulation (None for legacy rows)."""
        for asm in self.assessments:
            if asm.claim_kind is not None:
                return asm.claim_kind
        return None

    @property
    def claim_target(self) -> ClaimTarget | None:
        for asm in self.assessments:
            if asm.claim_target is not None:
                return asm.claim_target
        return None

    @property
    def commit_id(self) -> str | None:
        for asm in self.assessments:
            if asm.commit_id:
                return asm.commit_id
        return None

    @property
    def claim_value_semantics(self) -> str | None:
        for asm in self.assessments:
            if asm.value_semantics:
                return asm.value_semantics
        return None

    @property
    def claimed_value(self) -> str | None:
        for asm in self.assessments:
            if asm.claimed_value is not None:
                return asm.claimed_value
        return None

    @property
    def fact_category(self) -> JavaFactCategory | None:
        """Typed Java fact category (None for legacy/untyped rows).

        Legacy rows without a category keep legacy proof paths only; they
        never gain a new Java proof rule by default.
        """
        for asm in self.assessments:
            if asm.fact_category is not None:
                return asm.fact_category
            target = asm.claim_target
            if target is not None and target.fact_category is not None:
                return target.fact_category
        return None

    @model_validator(mode="after")
    def validate_assessment_consistency(self) -> "Hypothesis":
        for asm in self.assessments:
            if (
                asm.assessment == AssessmentType.SUPPORTS
                # Unresolved obligations carry no evidence by construction:
                # per-caller (CALL_EDGE) and per-claim (CLAIM_UNRESOLVED,
                # Phase 2) placeholders persist the query for the directive
                # instead of citing a row.
                and asm.reason_code not in ("CALL_EDGE_UNRESOLVED", "CLAIM_UNRESOLVED")
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
