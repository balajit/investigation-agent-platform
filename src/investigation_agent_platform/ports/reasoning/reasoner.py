# src/investigation_agent_platform/ports/reasoning/reasoner.py
"""LLM reasoner port protocol and structured decision contracts."""

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.investigation.models import (
    InvestigationAction,
    InvestigationState,
)


class InvestigationDecision(BaseModel):
    """Structured reasoning output containing step evaluation, action, and readiness."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: InvestigationAction | None = None
    observations: list[str] = Field(default_factory=list)
    information_gaps: list[str] = Field(default_factory=list)
    hypotheses_evaluated: list[str] = Field(default_factory=list)
    contradictions_found: list[str] = Field(default_factory=list)
    conclusion_readiness: float = Field(ge=0.0, le=1.0, default=0.0)
    reasoning_chain: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class InvestigationReasoner(Protocol):
    """Port isolating LLM reasoning model from platform execution."""

    async def reason(self, tenant_id: str, state: InvestigationState) -> InvestigationDecision:
        ...