# tests/unit/test_failure_record.py
"""Terminal failure records: every FAILED aggregate reports its cause via API.

Covers the workflow-error reconcile path (TransitionInvestigationInput
failure_code/failure_reason stamped onto metadata) and the shared
_stamp_failure helper. The conclude-activity path is covered by
test_investigations_wire.py::TestConclusionFailure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from investigation_agent_platform.api.dependencies import (
    AppContext,
    set_app_context,
)
from investigation_agent_platform.application.worker.activities import (
    TransitionInvestigationInput,
    _stamp_failure,
    transition_investigation_activity,
)
from investigation_agent_platform.domain.investigation.models import (
    Investigation,
    InvestigationContext,
    InvestigationRequest,
    InvestigationStatus,
)


class TestStampFailure:
    def test_shape(self) -> None:
        now = datetime.now(UTC)
        inv = Investigation(
            session_id="s",
            application_id="a",
            tenant_id="t",
            request=InvestigationRequest(
                application_id="a",
                problem_description="p",
                session_id="s",
                requested_by="tester",
            ),
            context=InvestigationContext(
                environment="dev", time_window=(now - timedelta(hours=1), now)
            ),
            status=InvestigationStatus.INVESTIGATING,
            created_at=now,
            updated_at=now,
            version=3,
        )
        stamped = _stamp_failure(inv, "WORKFLOW_ERROR", "ActivityError: boom")
        assert stamped.version == 3
        assert stamped.status == InvestigationStatus.INVESTIGATING
        failure = stamped.metadata["failure"]
        assert failure["code"] == "WORKFLOW_ERROR"
        assert failure["reason"] == "ActivityError: boom"
        assert failure["failed_at"]
        # Original untouched (model_copy).
        assert "failure" not in inv.metadata


class TestTransitionFailureStamp:
    @pytest.mark.asyncio
    async def test_reconcile_failed_carries_cause(self) -> None:
        ctx = AppContext()
        now = datetime.now(UTC)
        inv = Investigation(
            session_id="s",
            application_id="a",
            tenant_id="tenant-a",
            request=InvestigationRequest(
                application_id="a",
                problem_description="p",
                session_id="s",
                requested_by="tester",
            ),
            context=InvestigationContext(
                environment="dev", time_window=(now - timedelta(hours=1), now)
            ),
            status=InvestigationStatus.INVESTIGATING,
            created_at=now,
            updated_at=now,
            version=1,
        )
        await ctx.investigation_repo.create("tenant-a", inv)
        ctx.transition_repo = AsyncMock()
        ctx.transition_repo.record_transition = AsyncMock(return_value=None)
        ctx.timeline_repo = AsyncMock()
        ctx.timeline_repo.find_by_investigation_id = AsyncMock(return_value=[])
        ctx.timeline_repo.append = AsyncMock(return_value=None)
        set_app_context(ctx)

        result = await transition_investigation_activity(
            TransitionInvestigationInput(
                tenant_id="tenant-a",
                investigation_id=str(inv.id),
                target_status="FAILED",
                reason="Workflow error reconciled: ActivityError",
                failure_code="WORKFLOW_ERROR",
                failure_reason="ApplicationFailure: search failed",
            )
        )
        assert result.success is True
        stored = await ctx.investigation_repo.get_by_id("tenant-a", inv.id)
        assert stored is not None
        assert stored.status == InvestigationStatus.FAILED
        assert stored.metadata["failure"]["code"] == "WORKFLOW_ERROR"
        assert "ApplicationFailure" in stored.metadata["failure"]["reason"]

    @pytest.mark.asyncio
    async def test_no_stamp_without_code(self) -> None:
        ctx = AppContext()
        now = datetime.now(UTC)
        inv = Investigation(
            session_id="s",
            application_id="a",
            tenant_id="tenant-a",
            request=InvestigationRequest(
                application_id="a",
                problem_description="p",
                session_id="s",
                requested_by="tester",
            ),
            context=InvestigationContext(
                environment="dev", time_window=(now - timedelta(hours=1), now)
            ),
            status=InvestigationStatus.INVESTIGATING,
            created_at=now,
            updated_at=now,
            version=1,
        )
        await ctx.investigation_repo.create("tenant-a", inv)
        set_app_context(ctx)
        result = await transition_investigation_activity(
            TransitionInvestigationInput(
                tenant_id="tenant-a",
                investigation_id=str(inv.id),
                target_status="FAILED",
                reason="plain transition",
            )
        )
        assert result.success is True
        stored = await ctx.investigation_repo.get_by_id("tenant-a", inv.id)
        assert stored is not None
        assert "failure" not in (stored.metadata or {})
