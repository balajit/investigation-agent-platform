"""Continuation-only fresh-context reasoning integration tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from investigation_agent_platform.application.worker.activities.continuation import (
    continuation_reasoning_activity,
)
from investigation_agent_platform.application.worker.activities.inputs import (
    ContinuationReasoningInput,
    GenericActivityResult,
)
from investigation_agent_platform.domain.continuation.models import ContinuationStatus
from investigation_agent_platform.domain.investigation.models import (
    Investigation,
    InvestigationContext,
    InvestigationRequest,
    InvestigationState,
    InvestigationStatus,
)


def _context(
    investigation_id: UUID,
    continuation_id: UUID,
    status: InvestigationStatus = InvestigationStatus.COMPLETED,
) -> tuple[SimpleNamespace, MagicMock, MagicMock]:
    investigation = MagicMock(
        id=investigation_id,
        status=status,
        metadata={"loop_version": ""},
    )
    continuation = MagicMock(
        continuation_id=continuation_id,
        investigation_id=investigation_id,
        status=ContinuationStatus.SUCCEEDED,
        attempt_count=1,
    )
    repo = MagicMock()
    repo.get = AsyncMock(return_value=continuation)
    repo.set_status = AsyncMock(return_value=True)
    repo.release_reasoning_lease = AsyncMock(return_value=True)
    repo.replace_conclusion_with_history = AsyncMock(return_value=uuid4())
    finding_repo = MagicMock()
    finding_repo.get_conclusion = AsyncMock(return_value=MagicMock())
    checkpoint_investigation = Investigation.model_construct(
        id=investigation_id,
        tenant_id="tenant",
        session_id="session",
        application_id="app",
        request=InvestigationRequest(
            problem_description="problem",
            application_id="app",
            session_id="session",
            requested_by="principal",
        ),
        context=InvestigationContext(
            environment="test",
            time_window=(datetime.now(UTC) - timedelta(hours=1), datetime.now(UTC)),
        ),
        status=status,
        metadata={"loop_version": ""},
        version=1,
    )
    checkpoint = InvestigationState(investigation=checkpoint_investigation).model_dump(mode="json")
    ctx = SimpleNamespace(
        continuation_repo=repo,
        investigation_repo=SimpleNamespace(get_by_id=AsyncMock(return_value=investigation)),
        finding_repo=finding_repo,
        checkpoint_repo=SimpleNamespace(
            get_latest_checkpoint=AsyncMock(return_value=checkpoint)
        ),
        evidence_repo=SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[])),
    )
    return ctx, repo, investigation


@pytest.mark.asyncio
async def test_reasoning_reopens_runs_existing_activities_and_replaces_with_history() -> None:
    investigation_id = uuid4()
    continuation_id = uuid4()
    ctx, repo, investigation = _context(investigation_id, continuation_id)
    with (
        patch(
            "investigation_agent_platform.application.worker.activities.continuation._get_ctx",
            return_value=ctx,
        ),
        patch(
            "investigation_agent_platform.application.worker.activities.continuation.transition_investigation_activity",
            new=AsyncMock(return_value=GenericActivityResult(success=True)),
        ) as transition,
        patch(
            "investigation_agent_platform.application.worker.activities.continuation.reason_activity",
            new=AsyncMock(return_value=GenericActivityResult(success=True, data={"iteration": 2})),
        ) as reason,
        patch(
            "investigation_agent_platform.application.worker.activities.continuation.verify_root_cause_activity",
            new=AsyncMock(
                return_value=GenericActivityResult(
                    success=True, data={"verified": False, "reason": "insufficient"}
                )
            ),
        ) as verify,
        patch(
            "investigation_agent_platform.application.worker.activities.continuation.conclude_investigation_activity",
            new=AsyncMock(
                return_value=GenericActivityResult(success=True, data={"status": "COMPLETED"})
            ),
        ) as conclude,
    ):
        result = await continuation_reasoning_activity(
            ContinuationReasoningInput(
                tenant_id="tenant",
                investigation_id=str(investigation_id),
                continuation_id=str(continuation_id),
                principal_id="principal",
            )
        )

    assert result.success is True
    transition.assert_awaited_once()
    reason.assert_awaited_once()
    verify.assert_awaited_once()
    conclude.assert_awaited_once()
    repo.release_reasoning_lease.assert_awaited_once_with("tenant", investigation_id)
    assert investigation.status == InvestigationStatus.COMPLETED


@pytest.mark.asyncio
async def test_reasoning_fails_typed_when_checkpoint_is_unavailable_and_releases_lease() -> None:
    investigation_id = uuid4()
    continuation_id = uuid4()
    ctx, repo, _ = _context(investigation_id, continuation_id)
    ctx.checkpoint_repo.get_latest_checkpoint = AsyncMock(return_value=None)
    with patch(
        "investigation_agent_platform.application.worker.activities.continuation._get_ctx",
        return_value=ctx,
    ):
        result = await continuation_reasoning_activity(
            ContinuationReasoningInput(
                tenant_id="tenant",
                investigation_id=str(investigation_id),
                continuation_id=str(continuation_id),
                principal_id="principal",
            )
        )

    assert result.success is False
    assert result.error == "CONTINUATION_STATE_UNAVAILABLE"
    repo.set_status.assert_awaited_once()
    repo.release_reasoning_lease.assert_awaited_once_with("tenant", investigation_id)
