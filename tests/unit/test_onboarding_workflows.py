"""Phase 2 activities, workflow contract, and step ledger tests."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from investigation_agent_platform.application.onboarding import activities as activities_mod
from investigation_agent_platform.application.onboarding.activities import (
    CompleteCancellationInput,
    HeartbeatInput,
    OnboardingActivities,
    RecordTerminalInput,
    VerifyRevisionInput,
)
from investigation_agent_platform.application.onboarding.steps import (
    AnalyzeCoverageInput,
    DispatchStep,
    summarize_dispatch_steps,
)


class FakeDispatches:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.marked: list[tuple[str, str]] = []

    def add_request(self, state: str = "QUEUED") -> str:
        request_id = str(uuid4())
        self.rows[request_id] = {
            "request_id": request_id,
            "state": state,
            "content_hash": "c" * 64,
            "state_version": 1,
        }
        return request_id

    async def read_request_row(
        self, tenant_id: str, request_id: Any, revision: int
    ) -> dict[str, Any] | None:
        return self.rows.get(str(request_id))

    async def read_request_state(
        self, tenant_id: str, request_id: Any, revision: int
    ) -> str | None:
        row = self.rows.get(str(request_id))
        return row["state"] if row is not None else None

    async def mark_terminal(
        self, tenant_id: str, dispatch_id: Any, error_code: str, fencing_token: Any = None
    ) -> bool:
        self.marked.append((str(dispatch_id), error_code))
        return True

    async def complete_request_cancellation(
        self, tenant_id: str, request_id: Any, revision: int
    ) -> bool:
        row = self.rows.get(str(request_id))
        if row is None or row["state"] not in ("QUEUED", "CANCEL_REQUESTED"):
            return False
        row["state"] = "CANCELED"
        row["state_version"] += 1
        return True


async def test_verify_proceeds_only_from_queued() -> None:
    repo = FakeDispatches()
    request_id = repo.add_request()
    acts = OnboardingActivities(repo)
    ok = await acts.verify_revision(
        VerifyRevisionInput(
            tenant_id="t", request_id=request_id, revision=1, execution_generation=1
        )
    )
    assert ok.proceed is True
    assert ok.content_hash == "c" * 64
    missing = await acts.verify_revision(
        VerifyRevisionInput(
            tenant_id="t",
            request_id=str(uuid4()),
            revision=1,
            execution_generation=1,
        )
    )
    assert missing.proceed is False
    assert missing.reason == "request revision not found"


async def test_verify_routes_cancellation_away_from_work() -> None:
    repo = FakeDispatches()
    request_id = repo.add_request(state="CANCEL_REQUESTED")
    acts = OnboardingActivities(repo)
    result = await acts.verify_revision(
        VerifyRevisionInput(
            tenant_id="t", request_id=request_id, revision=1, execution_generation=1
        )
    )
    assert result.proceed is False
    assert result.reason == "cancellation requested"


async def test_heartbeat_reports_cancel_and_missing() -> None:
    repo = FakeDispatches()
    acts = OnboardingActivities(repo)
    missing = await acts.heartbeat(
        HeartbeatInput(tenant_id="t", request_id=str(uuid4()), revision=1)
    )
    assert missing.alive is False
    request_id = repo.add_request(state="CANCEL_REQUESTED")
    cancel = await acts.heartbeat(
        HeartbeatInput(tenant_id="t", request_id=request_id, revision=1)
    )
    assert cancel.alive is True
    assert cancel.cancel_requested is True
    repo.rows[request_id]["state"] = "QUEUED"
    ok = await acts.heartbeat(
        HeartbeatInput(tenant_id="t", request_id=request_id, revision=1)
    )
    assert ok.alive is True
    assert ok.cancel_requested is False


async def test_record_terminal_requires_error_code() -> None:
    repo = FakeDispatches()
    acts = OnboardingActivities(repo)
    with pytest.raises(ValueError, match="error_code"):
        await acts.record_terminal(
            RecordTerminalInput(
                tenant_id="t", dispatch_id=str(uuid4()), error_code=""
            )
        )
    done = await acts.record_terminal(
        RecordTerminalInput(
            tenant_id="t", dispatch_id=str(uuid4()), error_code="boom"
        )
    )
    assert done.terminal is True
    assert repo.marked[0][1] == "boom"


async def test_complete_cancellation_is_idempotent() -> None:
    repo = FakeDispatches()
    request_id = repo.add_request()
    acts = OnboardingActivities(repo)
    first = await acts.complete_cancellation(
        CompleteCancellationInput(
            tenant_id="t",
            dispatch_id=str(uuid4()),
            request_id=request_id,
            revision=1,
        )
    )
    assert first.terminal is True
    assert repo.rows[request_id]["state"] == "CANCELED"
    second = await acts.complete_cancellation(
        CompleteCancellationInput(
            tenant_id="t",
            dispatch_id=str(uuid4()),
            request_id=request_id,
            revision=1,
        )
    )
    assert second.terminal is True


async def test_analysis_slot_refuses_without_judgment() -> None:
    repo = FakeDispatches()
    acts = OnboardingActivities(repo)
    with pytest.raises(NotImplementedError, match="not implemented"):
        await acts.analyze_coverage(
            AnalyzeCoverageInput(
                tenant_id="t",
                request_id=str(uuid4()),
                revision=1,
                generation=1,
                snapshot_reference="e" * 64,
                evidence_hashes=["a" * 64],
            )
        )


def test_workflow_definition_imports_and_names_activity_contracts() -> None:
    from investigation_agent_platform.application.onboarding import (
        activities,
        workflows,
    )

    assert workflows.OnboardingDispatchWorkflow.__name__ == "OnboardingDispatchWorkflow"
    assert hasattr(activities.OnboardingActivities, "verify_revision")
    assert hasattr(activities.OnboardingActivities, "heartbeat")
    assert hasattr(activities.OnboardingActivities, "complete_cancellation")
    assert hasattr(activities.OnboardingActivities, "analyze_coverage")
    probe = workflows.OnboardingDispatchWorkflowInput(
        tenant_id="t",
        request_id=str(uuid4()),
        revision=1,
        execution_generation=1,
        workflow_id="onboarding-t-r1g1",
        dispatch_id=str(uuid4()),
        input_reference="c" * 64,
        schema_version="onboarding-dispatch/v1",
    )
    assert probe.workflow_id.startswith("onboarding-t-")


def test_step_ledger_renders_history_in_order() -> None:
    row = {"temporal_run_id": "run-9", "state": "ACCEPTED", "last_error_code": None}
    steps = summarize_dispatch_steps(row, ["CLAIM", "RENEW", "STALE_FENCE"])
    assert [step.step for step in steps] == [
        DispatchStep.CLAIMED,
        DispatchStep.RENEWED,
        DispatchStep.REQUEUED,
        DispatchStep.STARTED,
    ]
    assert steps[-1].detail == "run run-9"


def test_step_ledger_terminal_and_cancel_branches() -> None:
    failed = summarize_dispatch_steps(
        {"temporal_run_id": None, "state": "TERMINAL", "last_error_code": "boom"},
        ["CLAIM"],
    )
    assert failed[-1].step == DispatchStep.TERMINAL
    assert failed[-1].detail == "boom"
    canceled = summarize_dispatch_steps(
        {"temporal_run_id": None, "state": "TERMINAL", "last_error_code": "canceled"},
        ["CLAIM"],
    )
    assert canceled[-1].step == DispatchStep.CANCELED


def test_step_ledger_rejects_unknown_audit_actions() -> None:
    with pytest.raises(ValueError, match="unknown dispatch audit action"):
        summarize_dispatch_steps({"state": "CLAIMED"}, ["SOMEDAY"])


def test_analyze_coverage_input_carries_idempotency_key() -> None:
    request_id = str(uuid4())
    probe = AnalyzeCoverageInput(
        tenant_id="tenant-a",
        request_id=request_id,
        revision=2,
        generation=3,
        snapshot_reference="e" * 64,
    )
    assert (probe.request_id, probe.revision, probe.generation) == (
        request_id,
        2,
        3,
    )
    assert probe.evidence_hashes == []


def test_activities_module_imports_cleanly() -> None:
    assert activities_mod.OnboardingActivities is not None
