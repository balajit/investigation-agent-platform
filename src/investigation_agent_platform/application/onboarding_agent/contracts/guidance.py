# src/investigation_agent_platform/application/onboarding_agent/contracts/guidance.py
"""Report Q&A contract shapes (agent design §5.2).

Guidance inherits the report's evidence-pointer discipline: every answer
cites report entries, unknowns are answered with the probe that would
resolve them, and customer-facing renderings omit internals (analyzer and
model versions — the report shape carries no adapter names or thresholds
to leak).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.application.onboarding_agent.contracts.coverage import (
    CapabilityDisposition,
    EvidencePointer,
)


class QuestionKind(StrEnum):
    WHY = "why"
    EVIDENCE = "evidence"
    WHAT_WOULD_CHANGE = "what-would-change"
    LIMITATIONS = "limitations"


class Audience(StrEnum):
    OPERATOR = "operator"
    CUSTOMER = "customer"


class CoverageQuestion(BaseModel):
    """A structured question against one validated coverage report."""

    model_config = ConfigDict(extra="forbid")

    kind: QuestionKind
    subject: str = Field(min_length=1, max_length=256)
    audience: Audience = Audience.OPERATOR


class CitedAnswer(BaseModel):
    """A cited answer assembled only from report fields and fixed templates.

    ``status`` is ``unknown`` when the subject matches nothing in the report;
    an unknown answer always names the resolving probe and never guesses.
    """

    model_config = ConfigDict(extra="forbid")

    question: CoverageQuestion
    status: str = Field(pattern=r"^(answered|unknown)$")
    summary: str = Field(min_length=1)
    matched_service: str | None = Field(default=None, max_length=256)
    matched_disposition: CapabilityDisposition | None = None
    citations: list[EvidencePointer] = Field(default_factory=list)
    resolving_probe: str | None = Field(default=None, max_length=1024)
