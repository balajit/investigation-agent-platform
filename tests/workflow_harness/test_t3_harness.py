"""T3 workflow-harness tests: D6 fires, skips, and carries statements (D3 layer).

Opt-in: skips without IAP_TEMPORAL_TEST_SERVER or IAP_TEMPORAL_TEST_DOWNLOAD=1.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest
from runner import start
from stubs import Script

NC_STATEMENT = "the design is over-engineered"


def _all_nc(next_action="CONCLUDE"):
    return {
        "next_action": next_action,
        "conclusion_readiness": 0.4,
        "hypotheses_evaluated": [{"statement": NC_STATEMENT}],
        "claims": [{"statement": NC_STATEMENT, "claim_kind": "NOT_CHECKABLE"}],
    }


def _gather():
    return {
        "next_action": "SEARCH_CODE",
        "conclusion_readiness": 0.2,
        "action_parameters": {"query": "OrderServiceHandler"},
        "hypotheses_evaluated": [],
        "claims": [],
    }


def _concludes(script):
    return [c for c in script.calls if c[0] == "conclude"]


def _executions(script):
    return [c for c in script.calls if c[0] == "execute"]


@pytest.mark.asyncio
async def test_t310_turn0_all_nc_blocked_by_cap(temporal_env) -> None:
    """Turn 0 all-NC with nothing banked is blocked by the cap (D4 rate key stamped)."""
    script = Script(reason=[_all_nc(), _all_nc()])
    handle, worker = await start(temporal_env, script, loop_version="lv13")
    async with worker:
        await handle.result()
    assert _executions(script), "the blocked turn must still dispatch the gather"
    concludes = _concludes(script)
    assert len(concludes) == 1
    payload = concludes[0][1]
    assert payload.status == "COMPLETED"
    assert list(payload.opinion_only_statements or []) == [NC_STATEMENT]
    assert (payload.loop_telemetry or {}).get("d6_blocked_no_evidence_count") == 1


@pytest.mark.asyncio
async def test_t311_turn0_all_nc_on_lv3_still_fires(temporal_env) -> None:
    """Pre-cap pin: turn 0 all-NC on lv3 fires D6 (no execute, conclude requested)."""
    script = Script(reason=[_all_nc(), _all_nc()])
    handle, worker = await start(temporal_env, script, loop_version="lv3")
    async with worker:
        await handle.result()
    assert _executions(script) == []
    concludes = _concludes(script)
    assert len(concludes) == 1
    assert concludes[0][1].status == "COMPLETED"


@pytest.mark.asyncio
async def test_t312_all_nc_after_one_fetch_fires_d6(temporal_env) -> None:
    """Turn 0 gathers, turn 1 all-NC fires D6: no execute/retrieve/verify, statements ride along."""
    script = Script(reason=[_gather(), _all_nc()])
    handle, worker = await start(temporal_env, script, loop_version="lv13")
    async with worker:
        await handle.result()
    executions = _executions(script)
    assert len(executions) == 1
    assert executions[0][1].iteration == 0
    assert [c for c in script.calls if c[0] == "retrieve"] == []
    assert [c for c in script.calls if c[0] == "verify"] == []
    concludes = _concludes(script)
    assert len(concludes) == 1
    payload = concludes[0][1]
    assert payload.status == "COMPLETED"
    assert list(payload.opinion_only_statements or []) == [NC_STATEMENT]


@pytest.mark.asyncio
async def test_t313_d6_never_fires_on_a_defer_turn(temporal_env) -> None:
    """A defer turn with all-NC claims takes the defer path; D6 is not counted blocked."""
    deferred = dict(_all_nc())
    deferred["disposition"] = "DEFER_GATHERING"
    script = Script(reason=[deferred, _gather()])
    handle, worker = await start(
        temporal_env, script, loop_version="lv13", max_iterations=3
    )
    async with worker:
        await handle.result()
    for name, payload in _concludes(script):
        assert list(getattr(payload, "opinion_only_statements", None) or []) == []
        telemetry = getattr(payload, "loop_telemetry", None) or {}
        assert telemetry.get("d6_blocked_no_evidence_count", 0) == 0


@pytest.mark.asyncio
async def test_gap03_pin_default_matches_explicit_lv13(temporal_env) -> None:
    """Forward compatibility: unmarked pin behaves exactly like explicit lv13."""
    scripts = [Script(reason=[_gather(), _all_nc()]), Script(reason=[_gather(), _all_nc()])]
    payloads = []
    for script, overrides in zip(scripts, ({}, {"loop_version": "lv13"})):
        handle, worker = await start(temporal_env, script, **overrides)
        async with worker:
            await handle.result()
        concludes = _concludes(script)
        assert len(concludes) == 1
        payload = concludes[0][1]
        payloads.append(
            (payload.status, list(payload.opinion_only_statements or []))
        )
    assert payloads[0] == payloads[1]
