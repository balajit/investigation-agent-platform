# src/investigation_agent_platform/ports/reasoning/reasoner.py
"""LLM reasoner port protocol and structured decision contracts."""

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.hypothesis.models import ClaimKind, ClaimTarget
from investigation_agent_platform.domain.investigation.models import (
    InvestigationAction,
    InvestigationState,
)


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
    value_semantics: str | None = Field(default=None, max_length=64)
    claimed_value: str | None = Field(default=None, max_length=512)


class InvestigationDecision(BaseModel):
    """Structured reasoning output containing step evaluation, action, and readiness."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: InvestigationAction | None = None
    observations: list[str] = Field(default_factory=list)
    information_gaps: list[str] = Field(default_factory=list)
    hypotheses_evaluated: list[str] = Field(default_factory=list)
    claims: list[DecisionClaim] = Field(default_factory=list, max_length=20)
    contradictions_found: list[str] = Field(default_factory=list)
    conclusion_readiness: float = Field(ge=0.0, le=1.0, default=0.0)
    reasoning_chain: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class InvestigationReasoner(Protocol):
    """Port isolating LLM reasoning model from platform execution."""

    async def reason(self, tenant_id: str, state: InvestigationState) -> InvestigationDecision: ...
