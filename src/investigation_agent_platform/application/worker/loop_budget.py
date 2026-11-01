"""Priced loop bound (T5): pure budget arithmetic on the workflow clock."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from investigation_agent_platform.application.worker.reasoning_failure import (
    REASON_DURATION,
    REASON_TOKENS,
)

LOCAL_DURATION_GUARD_SECONDS = 1800  # equals the InvestigationLimits default
LOCAL_TOKEN_GUARD = 1_000_000  # equals the InvestigationLimits default
EXECUTION_TIMEOUT_SECONDS = 7200  # the one definition; the API start calls import it
PAUSE_MAX_SECONDS = 3600
CONCLUDE_RESERVE_SECONDS = 300  # conclude plus post-conclude activities after the budget ends
MIN_ACTIVITY_SECONDS = 10.0
#: The largest active-time budget that still leaves room for a maximal pause and the conclude steps.
MAX_DURATION_SECONDS = (
    EXECUTION_TIMEOUT_SECONDS - PAUSE_MAX_SECONDS - CONCLUDE_RESERVE_SECONDS
)  # 3300
#: Per-activity ceiling on total time including retries: attempts x start_to_close plus backoff slack.
ACTIVITY_S2C_CAP_SECONDS = {"reason": 400, "execute": 200, "verify": 200}


@dataclass(frozen=True)
class LoopBudget:
    started_at: datetime
    max_duration_seconds: int
    max_llm_tokens: int


def _bounded_int(value: Any, guard: int, low: int, high: int) -> int:
    try:
        number = int(value) if value is not None and not isinstance(value, bool) else guard
    except (TypeError, ValueError):
        number = guard
    return max(low, min(number, high))


def make_budget(
    started_at: datetime, max_duration_seconds: Any, max_llm_tokens: Any
) -> LoopBudget:
    """Resolve the budget (pure). None or malformed values fall back to the guards; the duration is
    clamped so budget + maximal pause + conclude fits inside the execution timeout."""
    return LoopBudget(
        started_at=started_at,
        max_duration_seconds=_bounded_int(
            max_duration_seconds, LOCAL_DURATION_GUARD_SECONDS, 10, MAX_DURATION_SECONDS
        ),
        max_llm_tokens=_bounded_int(max_llm_tokens, LOCAL_TOKEN_GUARD, 1000, 50_000_000),
    )


def active_seconds(budget: LoopBudget, now: datetime, paused_seconds: float) -> float:
    return max(0.0, (now - budget.started_at).total_seconds() - max(0.0, paused_seconds))


def remaining_seconds(budget: LoopBudget, now: datetime, paused_seconds: float) -> float:
    return budget.max_duration_seconds - active_seconds(budget, now, paused_seconds)


def budget_verdict(
    budget: LoopBudget, now: datetime, paused_seconds: float, llm_tokens_total: Any
) -> str | None:
    """The terminal reason when the budget is spent, else None (pure, never raises).

    Time is active time: a pause does not consume the duration budget (the pause has its own bound).
    """
    try:
        if remaining_seconds(budget, now, paused_seconds) <= 0:
            return REASON_DURATION
        tokens = int(llm_tokens_total or 0)
        if tokens >= budget.max_llm_tokens:
            return REASON_TOKENS
    except Exception:
        return None
    return None


def bounded_schedule_to_close(
    kind: str, budget: LoopBudget, now: datetime, paused_seconds: float
) -> timedelta:
    """schedule_to_close for a turn activity: its own retry ceiling, cut to the time that is left."""
    cap = float(ACTIVITY_S2C_CAP_SECONDS.get(kind, 100))
    left = remaining_seconds(budget, now, paused_seconds)
    return timedelta(seconds=max(MIN_ACTIVITY_SECONDS, min(cap, left)))


def is_schedule_to_close_timeout(exc: BaseException) -> bool:
    """Whether an ActivityError wraps a SCHEDULE_TO_CLOSE timeout (duck-typed walk of the cause chain)."""
    cause: BaseException | None = exc
    for _ in range(5):
        if cause is None:
            return False
        kind = getattr(getattr(cause, "type", None), "name", "")
        if kind == "SCHEDULE_TO_CLOSE":
            return True
        cause = getattr(cause, "__cause__", None) or getattr(cause, "__context__", None)
    return False


def reason_usage(data: Any) -> tuple[int, float]:
    """Token and cost usage carried by one reason result, from either shape (pure, never raises)."""

    def _i(v: Any) -> int:
        try:
            return max(0, int(v or 0))
        except (TypeError, ValueError):
            return 0

    def _f(v: Any) -> float:
        try:
            return max(0.0, float(v or 0.0))
        except (TypeError, ValueError):
            return 0.0

    try:
        d: dict[str, object] = dict(data) if isinstance(data, dict) else {}
        tokens, cost = _i(d.get("llm_tokens")), _f(d.get("llm_cost"))
        if not tokens:
            raw_usage = d.get("llm_usage")
            usage: dict[str, object] = dict(raw_usage) if isinstance(raw_usage, dict) else {}
            tokens = _i(usage.get("prompt_tokens")) + _i(usage.get("completion_tokens"))
            cost = cost or _f(usage.get("cost_usd"))
        return tokens, cost
    except Exception:
        return 0, 0.0
