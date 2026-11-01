"""H7b-T14: exhausted retryable reasoning outage under a real Temporal environment.

Opt-in: skips without IAP_TEMPORAL_TEST_SERVER or IAP_TEMPORAL_TEST_DOWNLOAD=1.
The reason stub always raises a retryable ReasoningProviderTransient; Temporal
retries to exhaustion and the workflow degrades the run instead of failing it.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest
from runner import start
from stubs import Script
from temporalio.exceptions import ApplicationError


@pytest.mark.asyncio
async def test_h7t14_exhausted_reasoning_retry_degrades(temporal_env) -> None:
    """An exhausted retryable reason failure concludes; it never raises the run."""
    from investigation_agent_platform.application.worker.reasoning_failure import (
        TRANSIENT_FAILURE_TYPE,
    )

    script = Script(
        reason=[{"next_action": "CONCLUDE"}],
        reason_error=ApplicationError(
            "gateway timeout", type=TRANSIENT_FAILURE_TYPE, non_retryable=False
        ),
    )
    handle, worker = await start(temporal_env, script, loop_version="lv13")
    async with worker:
        result = await handle.result()
    reasons = [c for c in script.calls if c[0] == "reason"]
    assert len(reasons) >= 2, "Temporal must retry the retryable failure"
    concludes = [c for c in script.calls if c[0] == "conclude"]
    assert len(concludes) == 1
    assert str(concludes[0][1].reason or "").startswith("reasoning_provider_unavailable")
    assert result.status in ("COMPLETED", "FAILED")
    assert "WORKFLOW_ERROR" not in str(result.error or "")
