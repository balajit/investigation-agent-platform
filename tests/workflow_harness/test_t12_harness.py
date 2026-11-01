"""T12 workflow-harness tests: post-conclude capture failure and turn-0 retrieval (D3 layer).

Opt-in: skips without IAP_TEMPORAL_TEST_SERVER or IAP_TEMPORAL_TEST_DOWNLOAD=1.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest
from runner import start
from stubs import Script
from temporalio.exceptions import ApplicationError

NC_STATEMENT = "the design is over-engineered"


def _all_nc(next_action="CONCLUDE"):
    return {
        "next_action": next_action,
        "conclusion_readiness": 0.4,
        "hypotheses_evaluated": [{"statement": NC_STATEMENT}],
        "claims": [{"statement": NC_STATEMENT, "claim_kind": "NOT_CHECKABLE"}],
    }


def _calls(script, name):
    return [c for c in script.calls if c[0] == name]


@pytest.mark.asyncio
async def test_t1212_post_conclude_capture_failure_stays_completed(temporal_env) -> None:
    """A non-retryable capture failure after a durable conclusion never rewrites the run."""
    from temporalio import activity as _activity

    def _boom(p):
        raise ApplicationError("capture store down", non_retryable=True)

    script = Script(reason=[_all_nc(), _all_nc()], capture=_boom)
    assert _activity is not None  # the stubs run under the real activity decorator
    handle, worker = await start(temporal_env, script, loop_version="lv13")
    async with worker:
        result = await handle.result()
    assert result.status == "COMPLETED"
    assert result.error in (None, "")
    assert _calls(script, "capture"), "capture must still be attempted"


@pytest.mark.asyncio
async def test_t1213_nc_at_turn0_does_not_skip_retrieval(temporal_env) -> None:
    """Turn 0 all-NC with nothing banked still dispatches the gather (EMPTY),
    then routes to the D6 abstention — retrieval ran, nothing was fabricated."""
    script = Script(reason=[_all_nc(), _all_nc()])
    handle, worker = await start(temporal_env, script, loop_version="lv13")
    async with worker:
        result = await handle.result()
    executions = _calls(script, "execute")
    assert executions, "the turn-0 gather must still dispatch"
    assert executions[0][1].iteration == 0
    concludes = _calls(script, "conclude")
    assert len(concludes) == 1
    assert concludes[0][1].status == "COMPLETED"
    assert list(concludes[0][1].opinion_only_statements or []) == [NC_STATEMENT]
    assert result.status == "COMPLETED"
