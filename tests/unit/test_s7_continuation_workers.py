"""Focused WP-04 continuation worker contract tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from investigation_agent_platform.application.investigation.continuation import (
    continuation_idempotency_key,
)
from investigation_agent_platform.application.worker.activities.continuation import (
    continuation_reasoning_activity,
    execute_continuation_activity,
    normalize_correlation_id,
)
from investigation_agent_platform.application.worker.activities.inputs import (
    ContinuationReasoningInput,
    ExecuteContinuationInput,
    GenericActivityResult,
)
from investigation_agent_platform.domain.continuation.models import ContinuationStatus


def test_correlation_id_normalization_keeps_non_uuid_bounded() -> None:
    correlation = str(uuid4())
    assert normalize_correlation_id(correlation) == (UUID(correlation), None)

    normalized, request_value = normalize_correlation_id("request-not-a-uuid" * 40)
    assert normalized is None
    assert request_value is not None
    assert len(request_value) == 256


@pytest.mark.asyncio
async def test_fetch_reloads_durable_parameters_and_calls_normal_executor() -> None:
    continuation_id = uuid4()
    investigation_id = uuid4()
    continuation = MagicMock(
        tenant_id="tenant",
        investigation_id=investigation_id,
        handoff_version="handoff-v1",
        version="s7-continuation-v1",
        action="GET_CODE",
        parameters={"file_path": "src/app.py"},
        idempotency_key=continuation_idempotency_key(
            "tenant", investigation_id, "handoff-v1", "GET_CODE", {"file_path": "src/app.py"}
        ),
        status=ContinuationStatus.CLAIMED,
        attempt_count=1,
    )
    repo = MagicMock()
    repo.get = AsyncMock(return_value=continuation)
    repo.set_status = AsyncMock(return_value=True)
    repo.acquire_reasoning_lease = AsyncMock(return_value=False)
    investigation_repo = MagicMock()
    investigation_repo.get_by_id = AsyncMock(return_value=MagicMock(metadata={}))
    ctx = MagicMock(continuation_repo=repo, investigation_repo=investigation_repo)
    ctx.timeline_repo = None

    with patch(
        "investigation_agent_platform.application.worker.activities.continuation._get_ctx",
        return_value=ctx,
    ), patch(
        "investigation_agent_platform.application.worker.activities.continuation.execute_action_activity",
        new=AsyncMock(
            return_value=GenericActivityResult(success=True, data={"items_count": 2})
        ),
    ) as executor:
        result = await execute_continuation_activity(
            ExecuteContinuationInput("tenant", str(continuation_id), "principal")
        )

    assert result.success is True
    assert result.data["evidence_added"] == 2
    executed = executor.await_args.args[0]
    assert executed.investigation_id == str(investigation_id)
    assert executed.action == "GET_CODE"
    assert executed.parameters == {"file_path": "src/app.py"}
    assert repo.set_status.await_count == 2
    assert repo.set_status.await_args_list[0].args[2] == "SUCCEEDED"
    assert repo.set_status.await_args_list[1].args[2] == "SUCCEEDED"
    assert repo.set_status.await_args_list[1].kwargs["error_code"] == (
        "CONTINUATION_REASONING_ALREADY_ACTIVE"
    )
    repo.acquire_reasoning_lease.assert_awaited_once()


@pytest.mark.asyncio
async def test_reasoning_reentry_is_typed_failure_when_archive_path_is_unavailable() -> None:
    ctx = MagicMock(continuation_repo=None)
    with patch(
        "investigation_agent_platform.application.worker.activities.continuation._get_ctx",
        return_value=ctx,
    ):
        result = await continuation_reasoning_activity(
            ContinuationReasoningInput(
                tenant_id="tenant",
                investigation_id=str(uuid4()),
                continuation_id=str(uuid4()),
                principal_id="principal",
            )
        )

    assert result.success is False
    assert result.error == "CONTINUATION_REASONING_REENTRY_UNAVAILABLE"
