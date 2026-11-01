"""T5 workflow-harness tests: the priced bound ends runs honestly (D3 layer).

Opt-in: skips without IAP_TEMPORAL_TEST_SERVER or IAP_TEMPORAL_TEST_DOWNLOAD=1.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest
from runner import start
from stubs import Script

DURATION_REASON = "duration_budget_exceeded"
TOKENS_REASON = "token_budget_exceeded"
PAUSE_REASON = "pause_timeout"


def _search_next(query="Filter"):
    return {
        "next_action": "SEARCH_CODE",
        "conclusion_readiness": 0.2,
        "action_parameters": {"query": query},
        "hypotheses_evaluated": [],
        "claims": [],
    }


def _verify_next(readiness=0.9):
    return {
        "next_action": "VERIFY_HYPOTHESIS",
        "conclusion_readiness": readiness,
        "hypotheses_evaluated": [{"statement": "the filter calls the handler"}],
        "claims": [
            {
                "statement": "the filter calls the handler",
                "claim_kind": "CALL_EDGE",
                "evidence_ids": [],
            }
        ],
    }


def _concludes(script):
    return [c for c in script.calls if c[0] == "conclude"]


async def _pace(name):
    if name == "reason":
        await asyncio.sleep(0.5)


def _reasons(script):
    return [c for c in script.calls if c[0] == "reason"]


@pytest.mark.asyncio
async def test_t500_spike_real_sleep_accrues_active_time(temporal_env) -> None:
    """Spike outcome: env.sleep does NOT advance this server combo (it
    returns without moving workflow.now()), so the budget tests use
    real-time sleeps plus the server's timer auto-skip. This pins the
    fallback premise: a real 2 s sleep IS visible as active time."""
    slept = {"n": 0}

    async def _on_activity(name):
        if name == "reason" and slept["n"] == 0:
            slept["n"] += 1
            await asyncio.sleep(2)

    script = Script(reason=[_search_next()] * 3)
    handle, worker = await start(
        temporal_env, script, loop_version="lv13", max_iterations=3, on_activity=_on_activity
    )
    async with worker:
        await handle.result()
    concludes = _concludes(script)
    assert len(concludes) == 1
    tele = concludes[0][1].loop_telemetry or {}
    assert tele.get("active_seconds", 0) >= 1.5


@pytest.mark.asyncio
async def test_t506_workflow_ends_on_the_duration_budget(temporal_env) -> None:
    """Real 2 s sleeps with a 10 s budget: the loop-top check exits early."""

    async def _on_activity(name):
        if name == "reason":
            await asyncio.sleep(2)

    script = Script(reason=[_search_next()] * 12)
    handle, worker = await start(
        temporal_env,
        script,
        loop_version="lv13",
        max_duration_seconds=10,
        on_activity=_on_activity,
    )
    async with worker:
        await handle.result()
    assert 2 < len(_reasons(script)) < 10
    concludes = _concludes(script)
    assert len(concludes) == 1
    assert concludes[0][1].reason == DURATION_REASON


@pytest.mark.asyncio
async def test_t507_workflow_ends_on_the_token_budget(temporal_env) -> None:
    """600 tokens per turn with a 1000-token budget: exit at the loop top after turn 2."""
    reasons = []
    for _ in range(4):
        r = _search_next()
        r["llm_tokens"] = 600
        reasons.append(r)
    script = Script(reason=reasons)
    handle, worker = await start(
        temporal_env, script, loop_version="lv13", max_llm_tokens=1000
    )
    async with worker:
        await handle.result()
    assert len(_reasons(script)) == 2
    concludes = _concludes(script)
    assert len(concludes) == 1
    assert concludes[0][1].reason == TOKENS_REASON


@pytest.mark.asyncio
async def test_t508_schedule_to_close_past_the_budget_is_a_budget_exit(temporal_env) -> None:
    """A real 12 s reason call against a 10 s budget: the schedule-to-close
    timeout maps to the duration budget exit, not a workflow error."""

    async def _on_activity(name):
        if name == "reason":
            _on_activity.n += 1
            if _on_activity.n == 1:
                await asyncio.sleep(12)

    _on_activity.n = 0
    script = Script(reason=[_search_next()] * 3)
    handle, worker = await start(
        temporal_env,
        script,
        loop_version="lv13",
        max_duration_seconds=10,
        on_activity=_on_activity,
    )
    async with worker:
        result = await handle.result()
    # The stub conclude echoes the requested FAILED status; the real
    # activity converts epistemic reasons to COMPLETED (unit-pinned by
    # T5.19). The harness asserts the budget mapping, not the conversion.
    assert result.status == "FAILED"
    concludes = _concludes(script)
    assert len(concludes) == 1
    assert concludes[0][1].reason == DURATION_REASON


@pytest.mark.asyncio
async def test_t508b_timeout_with_budget_remaining_reraises(temporal_env) -> None:
    """Negative: a schedule-to-close timeout with budget left re-raises
    (reconciles FAILED as today). The timeout is injected directly — a
    real 400 s wait is impractical; the timer path itself is T5.08."""
    from temporalio.exceptions import TimeoutError, TimeoutType

    def _boom(p):
        raise TimeoutError(
            "deadline", type=TimeoutType.SCHEDULE_TO_CLOSE, last_heartbeat_details=[]
        )

    script = Script(reason=[_search_next()] * 3, execute=_boom)
    handle, worker = await start(
        temporal_env, script, loop_version="lv13", max_duration_seconds=1800
    )
    async with worker:
        result = await handle.result()
    assert result.status == "FAILED"


@pytest.mark.asyncio
async def test_t509_cancel_while_paused_ends_the_run(temporal_env) -> None:
    """Pause, then cancel: the run completes CANCELLED instead of hanging.
    Both signals go out immediately so they are queued before the first
    loop top that can observe them; the wait then sees cancelled set and
    never arms its timer (the server would otherwise auto-skip straight
    to the pause timeout — that path is T5.10)."""
    script = Script(reason=[_search_next()] * 30)
    handle, worker = await start(
        temporal_env, script, loop_version="lv13", max_iterations=30
    )
    async with worker:
        task = asyncio.ensure_future(handle.result())
        await handle.signal("pause")
        await handle.signal("cancel")
        result = await asyncio.wait_for(task, timeout=120)
    assert result.status == "CANCELLED"
    concludes = _concludes(script)
    assert len(concludes) == 1
    assert concludes[0][1].status == "CANCELLED"


@pytest.mark.asyncio
async def test_t510_pause_past_the_bound_abstains(temporal_env) -> None:
    """A pause longer than PAUSE_MAX_SECONDS ends as pause_timeout. The
    server auto-skips the bounded wait, so this completes in real seconds."""

    async def _pace(name):
        if name == "reason":
            await asyncio.sleep(0.5)

    async def _driver(handle):
        await asyncio.sleep(1)
        await handle.signal("pause")

    script = Script(reason=[_search_next()] * 30)
    handle, worker = await start(
        temporal_env, script, loop_version="lv13", max_iterations=30, on_activity=_pace
    )
    async with worker:
        task = asyncio.ensure_future(handle.result())
        driver = asyncio.ensure_future(_driver(handle))
        await asyncio.wait_for(task, timeout=180)
        await driver
    concludes = _concludes(script)
    assert len(concludes) == 1
    assert concludes[0][1].reason == PAUSE_REASON
    tele = concludes[0][1].loop_telemetry or {}
    assert tele.get("paused_seconds", 0) >= 3600


@pytest.mark.asyncio
async def test_t511_older_pin_keeps_the_unbounded_pause(temporal_env) -> None:
    """lv7 keeps the old pause: cancel while paused does NOT end the run
    CANCELLED. The gate waits for the PAUSED transition (proof the old
    wait, which ignores cancel, was entered); only then is cancel sent,
    so a CANCELLED conclusion would prove the old branch observed it.
    The time-skipping server then fast-forwards the idle wait to the run
    timeout (production does the same at 7200 s) — either way, no
    CANCELLED conclusion."""
    script = Script(reason=[_search_next()] * 30)
    handle, worker = await start(
        temporal_env, script, loop_version="lv7", max_iterations=30
    )
    try:
        async with worker:
            task = asyncio.ensure_future(handle.result())
            await handle.signal("pause")
            seen = False
            for _ in range(150):
                await asyncio.sleep(0.2)
                if any(n == "transition" and p == "PAUSED" for n, p in script.calls):
                    seen = True
                    break
                if task.done():
                    break
            assert seen, "pause was never observed; the old wait was not entered"
            try:
                await handle.signal("cancel")
            except Exception:
                pass  # already timed out by auto-skip; still no CANCELLED below
            await asyncio.sleep(2)
            concludes = _concludes(script)
            assert all(c[1].status != "CANCELLED" for c in concludes)
            if not task.done():
                task.cancel()
    finally:
        try:
            await handle.terminate()
        except Exception:
            pass


@pytest.mark.asyncio
async def test_t512_usage_accumulates_on_a_budget_break(temporal_env) -> None:
    """A budget_exceeded turn banks its usage: telemetry sees the 900 tokens."""
    reasons = [
        dict(_search_next(), budget_exceeded=True, llm_tokens=900),
    ]
    script = Script(reason=reasons)
    handle, worker = await start(temporal_env, script, loop_version="lv13")
    async with worker:
        await handle.result()
    concludes = _concludes(script)
    assert len(concludes) == 1
    tele = concludes[0][1].loop_telemetry or {}
    assert tele.get("llm_tokens_total") == 900
