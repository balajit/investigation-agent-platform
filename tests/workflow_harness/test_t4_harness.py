"""T4 workflow-harness tests: the stall exit ends confident empty loops (D3 layer).

Opt-in: skips without IAP_TEMPORAL_TEST_SERVER or IAP_TEMPORAL_TEST_DOWNLOAD=1.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest
from runner import start
from stubs import Script

STALL_REASON = "overconfident_no_progress"


def _confident_empty(next_action="VERIFY_HYPOTHESIS", readiness=0.9):
    return {
        "next_action": next_action,
        "conclusion_readiness": readiness,
        "hypotheses_evaluated": [],
        "claims": [],
    }


def _banking_execute(eids_by_iteration):
    def _execute(p):
        eids = eids_by_iteration.get(p.iteration, [])
        status = "SUCCEEDED" if eids else "EMPTY"
        return {"action_outcome": {"status": status, "evidence_ids": list(eids)}}

    return _execute


def _concludes(script):
    return [c for c in script.calls if c[0] == "conclude"]


def _reasons(script):
    return [c for c in script.calls if c[0] == "reason"]


@pytest.mark.asyncio
async def test_t407_two_confident_empty_turns_end_the_run(temporal_env) -> None:
    """VERIFY at 0.9 with EMPTY turns ends after the second turn as a stall."""
    script = Script(reason=[_confident_empty()] * 4)
    handle, worker = await start(temporal_env, script, loop_version="lv13")
    async with worker:
        await handle.result()
    assert len(_reasons(script)) == 2
    concludes = _concludes(script)
    assert len(concludes) == 1
    payload = concludes[0][1]
    assert payload.status == "FAILED"
    assert payload.reason == STALL_REASON


@pytest.mark.asyncio
async def test_t408_a_banked_row_prevents_the_exit(temporal_env) -> None:
    """Banked rows every turn: progress resets the streak, no stall exit."""
    script = Script(
        reason=[_confident_empty()] * 5,
        execute=_banking_execute({0: ["e0"], 1: ["e1"], 2: ["e2"], 3: ["e3"]}),
    )
    handle, worker = await start(
        temporal_env, script, loop_version="lv13", max_iterations=4
    )
    async with worker:
        await handle.result()
    assert len(_reasons(script)) == 4
    concludes = _concludes(script)
    assert len(concludes) == 1
    payload = concludes[0][1]
    assert payload.reason != STALL_REASON
    assert (payload.loop_telemetry or {}).get("stall_exits", 0) == 0
    assert (payload.loop_telemetry or {}).get("stall_streak_max", 0) == 0


@pytest.mark.asyncio
async def test_t409_older_pin_never_exits_on_stall(temporal_env) -> None:
    """lv7 keeps the old loop: no stall exit, runs to the bound."""
    script = Script(reason=[_confident_empty()] * 5)
    handle, worker = await start(
        temporal_env, script, loop_version="lv7", max_iterations=3
    )
    async with worker:
        await handle.result()
    assert len(_reasons(script)) == 3
    concludes = _concludes(script)
    assert len(concludes) == 1
    assert concludes[0][1].reason == "verification_failed_or_limit_reached"


@pytest.mark.asyncio
async def test_t410_readiness_alone_never_exits(temporal_env) -> None:
    """Readiness 0.95 with a banked row every turn: no stall exit."""
    script = Script(
        reason=[_confident_empty(readiness=0.95)] * 4,
        execute=_banking_execute({0: ["e1"], 1: ["e2"], 2: ["e3"]}),
    )
    handle, worker = await start(
        temporal_env, script, loop_version="lv13", max_iterations=3
    )
    async with worker:
        await handle.result()
    concludes = _concludes(script)
    assert len(concludes) == 1
    assert concludes[0][1].reason != STALL_REASON


@pytest.mark.asyncio
async def test_t411_defer_turn_neither_extends_nor_ends_a_stall(temporal_env) -> None:
    """A defer turn between two confident empties is neutral; a banking
    defer resets the streak (trip takes one extra turn)."""
    deferred = _confident_empty()
    deferred["disposition"] = "DEFER_GATHERING"
    deferred["defer_gathering"] = {
        "next_action": "SEARCH_CODE",
        "next_action_parameters": {"query": "Filter"},
    }

    # Neutral defer: trip still needs two non-defer confident empties, so
    # the run ends after the third reason call, not the second.
    script = Script(reason=[_confident_empty(), deferred, _confident_empty(), _confident_empty()])
    handle, worker = await start(
        temporal_env, script, loop_version="lv13", max_iterations=6
    )
    async with worker:
        await handle.result()
    assert len(_reasons(script)) == 3
    concludes = _concludes(script)
    assert len(concludes) == 1
    assert concludes[0][1].reason == STALL_REASON

    # Banking defer: streak resets, so the trip needs re-accumulation.
    script2 = Script(
        reason=[_confident_empty(), deferred, _confident_empty(), _confident_empty(), _confident_empty()],
        execute=_banking_execute({1: ["e1"]}),
    )
    handle2, worker2 = await start(
        temporal_env, script2, loop_version="lv13", max_iterations=6
    )
    async with worker2:
        await handle2.result()
    assert len(_reasons(script2)) == 4
    concludes2 = _concludes(script2)
    assert len(concludes2) == 1
    assert concludes2[0][1].reason == STALL_REASON
