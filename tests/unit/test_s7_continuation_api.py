"""Focused API tests for S7 continuation claim and dispatch semantics."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from investigation_agent_platform.api.dependencies import InMemoryContinuationRepository
from investigation_agent_platform.api.errors import ApiError
from investigation_agent_platform.api.v1.routers import continuations
from investigation_agent_platform.domain.continuation.models import S7Continuation
from investigation_agent_platform.domain.finding.models import HandoffState


def _row(investigation_id):
    now = datetime.now(UTC)
    return S7Continuation(
        continuation_id=uuid4(),
        tenant_id="tenant-a",
        investigation_id=investigation_id,
        handoff_version="handoff-v1",
        source_handoff_state=HandoffState.BUDGET_DEFERRED,
        action="GET_CODE",
        parameters={"file_path": "src/app.py"},
        target="GET_CODE src/app.py",
        price=1.0,
        idempotency_key="0123456789abcdef0123456789abcdef",
        created_at=now,
        expires_at=now + timedelta(days=1),
    )


class _Temporal:
    def __init__(self, *, failure=None):
        self.start_workflow = AsyncMock(side_effect=failure)
        self.start_workflow.return_value = SimpleNamespace(run_id="run-1")
        self.get_workflow_handle = lambda workflow_id: SimpleNamespace(run_id=f"adopted-{workflow_id}")


@pytest.mark.asyncio
async def test_execute_claims_before_start_and_replays(monkeypatch):
    investigation_id = uuid4()
    row = _row(investigation_id)
    repo = InMemoryContinuationRepository()
    await repo.insert("tenant-a", row)
    temporal = _Temporal()
    ctx = SimpleNamespace(
        continuation_repo=repo,
        investigation_repo=SimpleNamespace(
            get_by_id=AsyncMock(return_value=SimpleNamespace(application_id="app"))
        ),
        finding_repo=SimpleNamespace(
            get_conclusion=AsyncMock(
                return_value=SimpleNamespace(handoff=SimpleNamespace(version="handoff-v1"))
            )
        ),
        action_authorizer=SimpleNamespace(authorize_action=AsyncMock(return_value=True)),
        temporal_client=temporal,
        temporal_config=SimpleNamespace(task_queue="continuation-tasks"),
    )
    monkeypatch.setattr(continuations, "get_app_context", lambda: ctx)

    first = await continuations.execute_continuation(
        str(investigation_id), str(row.continuation_id), "tenant-a", "principal-a"
    )
    second = await continuations.execute_continuation(
        str(investigation_id), str(row.continuation_id), "tenant-a", "principal-a"
    )

    assert first.status.value == "CLAIMED"
    assert first.idempotent_replay is False
    assert second.idempotent_replay is True
    temporal.start_workflow.assert_awaited_once()
    assert temporal.start_workflow.await_args.kwargs["id"] == (
        f"wf-continuation-{row.continuation_id}-a1"
    )


@pytest.mark.asyncio
async def test_dispatch_failure_is_durable_and_public(monkeypatch):
    investigation_id = uuid4()
    row = _row(investigation_id)
    repo = InMemoryContinuationRepository()
    await repo.insert("tenant-a", row)
    ctx = SimpleNamespace(
        continuation_repo=repo,
        investigation_repo=SimpleNamespace(
            get_by_id=AsyncMock(return_value=SimpleNamespace(application_id="app"))
        ),
        finding_repo=SimpleNamespace(
            get_conclusion=AsyncMock(
                return_value=SimpleNamespace(handoff=SimpleNamespace(version="handoff-v1"))
            )
        ),
        action_authorizer=SimpleNamespace(authorize_action=AsyncMock(return_value=True)),
        temporal_client=_Temporal(failure=RuntimeError("transport details")),
    )
    monkeypatch.setattr(continuations, "get_app_context", lambda: ctx)

    with pytest.raises(ApiError) as error:
        await continuations.execute_continuation(
            str(investigation_id), str(row.continuation_id), "tenant-a", "principal-a"
        )

    assert error.value.code == "CONTINUATION_DISPATCH_UNAVAILABLE"
    stored = await repo.get("tenant-a", row.continuation_id)
    assert stored is not None
    assert stored.status == "FAILED"
    assert stored.last_error_code == "CONTINUATION_DISPATCH_UNAVAILABLE"
