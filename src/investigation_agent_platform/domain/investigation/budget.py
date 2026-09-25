# src/investigation_agent_platform/domain/investigation/budget.py
"""Domain models for investigation execution boundaries and budget tracking."""

import logging
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class InvestigationBudget(BaseModel):
    """Immutable boundary specifications for execution cost and limit controls."""

    model_config = ConfigDict(frozen=True)

    time_budget_seconds: int = Field(
        default=1800, ge=10, description="Max allowed execution duration"
    )
    max_action_steps: int = Field(default=25, ge=1, description="Max tool execution turns allowed")
    max_prompt_tokens_per_turn: int = Field(
        default=30000, ge=1000, description="Max allowed working context tokens per turn"
    )
    max_cumulative_tokens: int = Field(
        default=150000, ge=5000, description="Max cumulative token usage ceiling"
    )
    max_cost_usd: float = Field(default=10.0, ge=0.1, description="Max estimated LLM cost budget")
    current_tokens_used: int = Field(default=0, ge=0)
    current_turn_tokens_used: int = Field(
        default=0, ge=0, description="Tokens consumed by the most recent execution turn"
    )
    current_cost_usd: float = Field(default=0.0, ge=0.0)
    current_action_count: int = Field(default=0, ge=0)
    start_time: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def record_usage(self, tokens: int, cost: float) -> "InvestigationBudget":
        """Returns a new immutable instance updated with additional resource consumption."""
        return self.model_copy(
            update={
                "current_tokens_used": self.current_tokens_used + tokens,
                "current_turn_tokens_used": tokens,
                "current_cost_usd": round(self.current_cost_usd + cost, 4),
                "current_action_count": self.current_action_count + 1,
            }
        )

    def is_exceeded(self, current_time: datetime | None = None) -> bool:
        """Evaluates whether any execution limit or wall-clock budget has been breached."""
        now = current_time or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)

        elapsed_seconds = (now - self.start_time).total_seconds()

        exceeded = (
            elapsed_seconds >= self.time_budget_seconds
            or self.current_action_count >= self.max_action_steps
            or self.current_tokens_used >= self.max_cumulative_tokens
            or self.current_cost_usd >= self.max_cost_usd
        )

        if exceeded:
            logger.warning(
                "Investigation budget breach detected",
                extra={
                    "elapsed_seconds": elapsed_seconds,
                    "time_budget_seconds": self.time_budget_seconds,
                    "current_action_count": self.current_action_count,
                    "max_action_steps": self.max_action_steps,
                    "current_tokens_used": self.current_tokens_used,
                    "max_cumulative_tokens": self.max_cumulative_tokens,
                    "current_cost_usd": self.current_cost_usd,
                    "max_cost_usd": self.max_cost_usd,
                },
            )

        return exceeded
