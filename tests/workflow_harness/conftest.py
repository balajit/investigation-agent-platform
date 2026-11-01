"""D3: opt-in Temporal workflow-harness fixture (time-skipping test server)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from temporalio.testing import WorkflowEnvironment


def _server_path() -> str | None:
    # Operator-provided binary; avoids a network download in CI. Absent ->
    # download on first use only when explicitly allowed.
    return os.environ.get("IAP_TEMPORAL_TEST_SERVER") or None


def _allowed() -> bool:
    return bool(_server_path()) or os.environ.get("IAP_TEMPORAL_TEST_DOWNLOAD") == "1"


@pytest_asyncio.fixture(scope="session")
async def temporal_env() -> AsyncIterator[WorkflowEnvironment]:
    if not _allowed():
        pytest.skip(
            "workflow harness needs IAP_TEMPORAL_TEST_SERVER or IAP_TEMPORAL_TEST_DOWNLOAD=1"
        )
    env = await WorkflowEnvironment.start_time_skipping(
        test_server_existing_path=_server_path()
    )
    try:
        yield env
    finally:
        await env.shutdown()
