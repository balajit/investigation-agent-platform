# src/investigation_agent_platform/ports/reasoning/reasoner.py
"""LLM reasoner port protocol and structured decision contracts."""

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from investigation_agent_platform.domain.code.models import JavaFactCategory
from investigation_agent_platform.domain.hypothesis.models import ClaimKind, ClaimTarget
from investigation_agent_platform.domain.investigation.models import (
    InvestigationAction,
    InvestigationState,
)


class CallerEdgeClaim(BaseModel):
    """One independently provable edge in a caller enumeration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: ClaimTarget
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    # Phase 2 (claim binding): per-edge retrieval hint (None = omitted;
    # derived fallback). Never proof, never identity.
    search_query: str | None = Field(default=None, max_length=1024)
    search_scope: str | None = Field(default=None, max_length=512)


class DecisionClaim(BaseModel):
    """One typed claim with its own evidence binding (Phase 2.1).

    Replaces the batch-wide citation list: two rival claims can cite
    different evidence. ``statement`` must match an entry in
    ``hypotheses_evaluated`` (or is persisted as a hypothesis itself).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    statement: str = Field(..., min_length=1, max_length=2048)
    claim_kind: ClaimKind = ClaimKind.CAUSAL
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)
    target: ClaimTarget | None = None
    # Phase 2 (claim binding): retrieval hint the model emitted for this
    # claim. Never proof, never identity — locality still requires
    # banked-ID overlap. None = legacy/model-omitted (derived fallback).
    search_query: str | None = Field(default=None, max_length=1024)
    search_scope: str | None = Field(default=None, max_length=512)
    fact_category: JavaFactCategory | None = None
    commit_id: str | None = Field(default=None, max_length=64)
    value_semantics: str | None = Field(default=None, max_length=64)
    claimed_value: str | None = Field(default=None, max_length=512)
    caller_edges: list[CallerEdgeClaim] = Field(default_factory=list, max_length=20)


class ContradictionReport(BaseModel):
    """H11a: a model-reported contradiction with the evidence rows it relies on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    statement: str = Field(min_length=1, max_length=2048)
    evidence_ids: list[str] = Field(default_factory=list, max_length=20)


class ReasoningDisposition(StrEnum):
    """Workflow disposition for a reasoner decision."""

    DEFER_GATHERING = "DEFER_GATHERING"


class DeferGathering(BaseModel):
    """Bounded evidence-gathering request that must not persist a hypothesis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str = Field(..., min_length=1, max_length=1024)
    missing_proof_kind: str = Field(..., min_length=1, max_length=64)
    next_action: str = Field(..., min_length=1, max_length=64)
    next_action_parameters: dict[str, Any] = Field(default_factory=dict)
    target: str = Field(..., min_length=1, max_length=512)
    scope: str = Field(..., min_length=1, max_length=512)
    remaining_repair_budget: int = Field(..., ge=0, le=2)


class InvestigationDecision(BaseModel):
    """Structured reasoning output containing step evaluation, action, and readiness."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: InvestigationAction | None = None
    observations: list[str] = Field(default_factory=list)
    information_gaps: list[str] = Field(default_factory=list)
    hypotheses_evaluated: list[str] = Field(default_factory=list)
    claims: list[DecisionClaim] = Field(default_factory=list, max_length=20)
    contradictions_found: list[ContradictionReport] = Field(default_factory=list, max_length=20)
    conclusion_readiness: float = Field(ge=0.0, le=1.0, default=0.0)
    reasoning_chain: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    disposition: ReasoningDisposition | None = None
    defer_gathering: DeferGathering | None = None

    @field_validator("contradictions_found", mode="before")
    @classmethod
    def _bare_strings_are_unlinked_reports(cls, value: Any) -> Any:
        # A bare string is an unlinked report (a note), by definition, not a legacy form.
        return [{"statement": v} if isinstance(v, str) else v for v in (value or [])]

    @model_validator(mode="after")
    def validate_disposition(self) -> "InvestigationDecision":
        if (
            self.defer_gathering is not None
            and self.disposition != ReasoningDisposition.DEFER_GATHERING
        ):
            raise ValueError("defer_gathering requires disposition=DEFER_GATHERING")
        if (
            self.disposition == ReasoningDisposition.DEFER_GATHERING
            and self.defer_gathering is None
        ):
            raise ValueError("DEFER_GATHERING requires defer_gathering")
        return self


@runtime_checkable
class InvestigationReasoner(Protocol):
    """Port isolating LLM reasoning model from platform execution."""

    async def reason(self, tenant_id: str, state: InvestigationState) -> InvestigationDecision: ...
