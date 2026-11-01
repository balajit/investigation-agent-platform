"""T5: priced iteration bound — pure budget arithmetic, dispatch plumbing, terminal mapping."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from investigation_agent_platform.application.worker.loop_budget import (
    EXECUTION_TIMEOUT_SECONDS,
    LOCAL_DURATION_GUARD_SECONDS,
    LOCAL_TOKEN_GUARD,
    MAX_DURATION_SECONDS,
    PAUSE_MAX_SECONDS,
    LoopBudget,
    bounded_schedule_to_close,
    budget_verdict,
    is_schedule_to_close_timeout,
    make_budget,
    reason_usage,
)


def _budget(**over):  # type: ignore[no-untyped-def]
    base = dict(
        started_at=datetime(2026, 1, 1, 0, 0, 0),
        max_duration_seconds=1800,
        max_llm_tokens=1_000_000,
    )
    base.update(over)
    return LoopBudget(**base)  # type: ignore[arg-type]


def test_t501_budget_verdict_table() -> None:
    b = _budget()
    t0 = datetime(2026, 1, 1, 0, 0, 0)
    assert budget_verdict(b, t0 + timedelta(seconds=1799), 0.0, 0) is None
    assert (
        budget_verdict(b, t0 + timedelta(seconds=1800), 0.0, 0)
        == "duration_budget_exceeded"
    )
    assert (
        budget_verdict(b, t0 + timedelta(seconds=2399), 600.0, 0) is None
    )  # pause excluded
    assert (
        budget_verdict(b, t0 + timedelta(seconds=2400), 600.0, 0)
        == "duration_budget_exceeded"
    )
    assert budget_verdict(b, t0, 0.0, 999_999) is None
    assert budget_verdict(b, t0, 0.0, 1_000_000) == "token_budget_exceeded"
    assert budget_verdict(b, t0, 0.0, "x") is None
    assert budget_verdict(b, t0, 0.0, None) is None
    assert budget_verdict(None, t0, 0.0, 0) is None  # type: ignore[arg-type]


def test_t502_make_budget_clamps_and_defaults() -> None:
    t0 = datetime(2026, 1, 1, 0, 0, 0)
    assert make_budget(t0, None, None).max_duration_seconds == LOCAL_DURATION_GUARD_SECONDS
    assert make_budget(t0, None, None).max_llm_tokens == LOCAL_TOKEN_GUARD
    assert make_budget(t0, 0, -5).max_duration_seconds == 10
    assert make_budget(t0, "x", "y").max_llm_tokens == LOCAL_TOKEN_GUARD
    assert make_budget(t0, True, True).max_duration_seconds == LOCAL_DURATION_GUARD_SECONDS
    assert make_budget(t0, 99999, 10**9).max_duration_seconds == MAX_DURATION_SECONDS
    assert make_budget(t0, 99999, 10**9).max_llm_tokens == 50_000_000


def test_t503_bounded_schedule_to_close() -> None:
    b = _budget()
    t0 = datetime(2026, 1, 1, 0, 0, 0)
    assert bounded_schedule_to_close("reason", b, t0 + timedelta(seconds=900), 0.0) == timedelta(
        seconds=400
    )
    assert bounded_schedule_to_close("reason", b, t0 + timedelta(seconds=1750), 0.0) == timedelta(
        seconds=50
    )
    assert bounded_schedule_to_close("reason", b, t0 + timedelta(seconds=9999), 0.0) == timedelta(
        seconds=10
    )
    assert bounded_schedule_to_close("mystery", b, t0, 0.0) == timedelta(seconds=100)


def _activity_wrap(cause):  # type: ignore[no-untyped-def]
    from temporalio.exceptions import ActivityError, RetryState

    wrapped = ActivityError(
        "wrap",
        scheduled_event_id=1,
        started_event_id=2,
        identity="test",
        activity_type="reason_activity",
        activity_id="1",
        retry_state=RetryState.TIMEOUT,
    )
    wrapped.__cause__ = cause
    return wrapped


def test_t504_is_schedule_to_close_timeout() -> None:
    from temporalio.exceptions import TimeoutError, TimeoutType

    s2c = TimeoutError(
        "deadline", type=TimeoutType.SCHEDULE_TO_CLOSE, last_heartbeat_details=[]
    )
    assert is_schedule_to_close_timeout(_activity_wrap(s2c)) is True
    start = TimeoutError(
        "slow", type=TimeoutType.START_TO_CLOSE, last_heartbeat_details=[]
    )
    assert is_schedule_to_close_timeout(_activity_wrap(start)) is False
    assert is_schedule_to_close_timeout(ValueError("plain")) is False
    assert is_schedule_to_close_timeout(None) is False  # type: ignore[arg-type]


def test_t505_reason_usage_both_shapes() -> None:
    assert reason_usage({"llm_tokens": 7, "llm_cost": 0.2}) == (7, 0.2)
    assert reason_usage(
        {"llm_usage": {"prompt_tokens": 5, "completion_tokens": 3, "cost_usd": 0.1}}
    ) == (8, 0.1)
    assert reason_usage({"llm_tokens": 0, "llm_usage": {"prompt_tokens": 2}}) == (2, 0.0)
    assert reason_usage("garbage") == (0, 0.0)
    assert reason_usage(None) == (0, 0.0)


def test_t513_new_telemetry_keys_are_stamped() -> None:
    from investigation_agent_platform.application.worker.activities.inputs import (
        _derive_loop_telemetry,
    )

    out = _derive_loop_telemetry(
        {"active_seconds": 1790, "paused_seconds": 30, "llm_tokens_total": 5}, {}, 0
    )
    assert out is not None
    assert out["active_seconds"] == 1790
    assert out["paused_seconds"] == 30


def test_t514_arithmetic_pin_and_router_constant() -> None:
    from investigation_agent_platform.application.worker.loop_budget import (
        CONCLUDE_RESERVE_SECONDS,
    )

    assert (
        LOCAL_DURATION_GUARD_SECONDS + PAUSE_MAX_SECONDS + CONCLUDE_RESERVE_SECONDS
        < EXECUTION_TIMEOUT_SECONDS
    )
    assert (
        MAX_DURATION_SECONDS
        == EXECUTION_TIMEOUT_SECONDS - PAUSE_MAX_SECONDS - CONCLUDE_RESERVE_SECONDS
    )
    src = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "investigation_agent_platform"
        / "api"
        / "v1"
        / "routers"
        / "investigations.py"
    ).read_text(encoding="utf-8")
    assert "EXECUTION_TIMEOUT_SECONDS" in src
    assert "run_timeout=_timedelta(seconds=_EXECUTION_TIMEOUT_SECONDS)" in src


@pytest.mark.asyncio
async def test_t516_limits_reach_the_workflow() -> None:
    from datetime import UTC, datetime

    from investigation_agent_platform.api.v1.routers.investigations import (
        _dispatch_workflow,
    )
    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationContext,
        InvestigationRequest,
        InvestigationStatus,
    )

    seen: dict = {}

    async def _start(name, wf_input, **kwargs):  # type: ignore[no-untyped-def]
        seen["input"] = wf_input
        seen["kwargs"] = kwargs
        return SimpleNamespace(workflow_id="wf-1")

    profile = SimpleNamespace(
        status=SimpleNamespace(value="ACTIVE"),
        investigation=SimpleNamespace(
            evidence_budget={"max_duration_seconds": 900, "max_llm_tokens": 5000},
            maximumHypotheses=None,
        ),
    )
    ctx = SimpleNamespace(
        profile_repo=SimpleNamespace(list_revisions=AsyncMock(return_value=[profile])),
        temporal_config=SimpleNamespace(task_queue="investigation-tasks"),
        temporal_client=SimpleNamespace(start_workflow=_start),
        investigation_repo=SimpleNamespace(save=AsyncMock(return_value=None)),
    )
    now = datetime.now(UTC)
    inv = Investigation(
        session_id="sess",
        application_id="app",
        tenant_id="t",
        request=InvestigationRequest(
            application_id="app",
            problem_description="d",
            session_id="sess",
            requested_by="tester",
        ),
        context=InvestigationContext(environment="dev", time_window=(now, now)),
        status=InvestigationStatus.CREATED,
        metadata={},
    )
    await _dispatch_workflow(ctx, inv, "t", str(inv.id), "corr-1")
    wf_input = seen["input"]
    assert wf_input.max_duration_seconds == 900
    assert wf_input.max_llm_tokens == 5000
    assert seen["kwargs"]["execution_timeout"] == timedelta(seconds=7200)
    assert seen["kwargs"]["run_timeout"] == timedelta(seconds=7200)


@pytest.mark.asyncio
async def test_t519_terminal_mapping() -> None:
    from investigation_agent_platform.application.worker.activities.inputs import (
        ConcludeInvestigationInput,
    )
    from investigation_agent_platform.application.worker.activities.lifecycle import (
        _failure_code_for,
        conclude_investigation_activity,
    )
    from investigation_agent_platform.domain.finding.models import (
        ConclusionStatus,
        HandoffState,
    )
    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationContext,
        InvestigationRequest,
        InvestigationStatus,
    )

    assert _failure_code_for("duration_budget_exceeded", False) == "BUDGET_EXCEEDED"
    assert _failure_code_for("token_budget_exceeded", False) == "BUDGET_EXCEEDED"
    assert _failure_code_for("pause_timeout", False) == "BUDGET_EXCEEDED"
    assert _failure_code_for("overconfident_no_progress", False) == "UNVERIFIED"

    from datetime import UTC, datetime

    from investigation_agent_platform.api.dependencies import AppContext, set_app_context

    async def _run(reason):  # type: ignore[no-untyped-def]
        saved: dict = {}

        async def _save_conclusion(tenant_id, conclusion):
            saved["conclusion"] = conclusion
            return conclusion

        now = datetime.now(UTC)
        inv = Investigation(
            session_id="sess-t5",
            application_id="app",
            tenant_id="t",
            request=InvestigationRequest(
                application_id="app",
                problem_description="t5",
                session_id="sess-t5",
                requested_by="tester",
            ),
            context=InvestigationContext(environment="dev", time_window=(now, now)),
            status=InvestigationStatus.INVESTIGATING,
            metadata={"loop_version": "lv13"},
        )
        ctx = AppContext()
        ctx.investigation_repo = SimpleNamespace(
            get_by_id=AsyncMock(return_value=inv), save=AsyncMock()
        )
        ctx.evidence_repo = SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[]))
        ctx.hypothesis_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[]), save=AsyncMock()
        )
        ctx.finding_repo = SimpleNamespace(
            save_conclusion=AsyncMock(side_effect=_save_conclusion),
            save_finding_record=AsyncMock(return_value=None),
        )
        ctx.timeline_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[]),
            append=AsyncMock(return_value=None),
        )
        set_app_context(ctx)
        result = await conclude_investigation_activity(
            ConcludeInvestigationInput(
                tenant_id="t",
                investigation_id=str(inv.id),
                status="FAILED",
                reason=reason,
            )
        )
        assert result.success is True
        assert result.data.get("status") == InvestigationStatus.COMPLETED.value
        return saved.get("conclusion")

    duration = await _run("duration_budget_exceeded")
    assert duration.status == ConclusionStatus.INSUFFICIENT_EVIDENCE
    assert duration.handoff is not None
    assert duration.handoff.state == HandoffState.BUDGET_DEFERRED

    tokens = await _run("token_budget_exceeded")
    assert tokens.handoff is not None
    assert tokens.handoff.state == HandoffState.BUDGET_DEFERRED

    paused = await _run("pause_timeout")
    assert paused.status == ConclusionStatus.INSUFFICIENT_EVIDENCE
    assert paused.handoff is not None
    assert paused.handoff.state == HandoffState.FRONTIER_EXHAUSTED
    assert "paused past the allowed window" in (paused.next_check or "")


def test_t417_exit_contract_names_the_stall_break() -> None:
    import ast
    from pathlib import Path

    from investigation_agent_platform.application.worker.workflows import (
        run_investigation,
    )

    src = Path(run_investigation.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = [
        ast.get_source_segment(src, node.test) or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and "_stalled" in (ast.get_source_segment(src, node.test) or "")
    ]
    assert found, "the terminal break condition must name _stalled"


def test_t518_pause_wait_is_cancel_aware() -> None:
    """The bounded pause wait wakes on cancel (T5-D11). A harness run
    cannot deterministically land cancel inside the wait (the time-skipping
    server fast-forwards the timer), so the predicate is pinned here;
    T5.09 covers the end-to-end cancel path."""
    from pathlib import Path

    from investigation_agent_platform.application.worker.workflows import (
        run_investigation,
    )

    src = Path(run_investigation.__file__).read_text(encoding="utf-8")
    assert "(not self._is_paused) or self._is_cancelled" in src
