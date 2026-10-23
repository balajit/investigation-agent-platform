# src/investigation_agent_platform/application/onboarding/workflows.py
"""Phase 2 onboarding workflow definitions (deterministic, Temporal-native).

The dispatch lifecycle workflow below is intentionally thin: verify, heartbeat
with cancellation checkpoints, and hand terminal outcomes to activities that
perform CAS-guarded writes. Domain work arrives with Phase 3 adapters behind
a workflow version bump (DEP-07); until then no phantom progress is
manufactured — a workflow that cannot do domain work holds the execution
open and observable rather than inventing an outcome.

Temporal rules honored here: no I/O, no wall clocks, no randomness, no
iteration over unordered collections inside workflow code. All state changes
happen in activities under fencing or CAS guards.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from investigation_agent_platform.application.onboarding import activities as _activities
    from investigation_agent_platform.application.onboarding.activities import (
        CompleteCancellationInput,
        HeartbeatInput,
        VerifyRevisionInput,
    )


@dataclass
class OnboardingDispatchWorkflowInput:
    """Temporal-payload-safe workflow identity (strings only).

    The dispatcher converts the port contract (UUID objects are fine
    in-process) to this shape at the Temporal boundary; future adapters do
    the same conversion in one place.
    """

    tenant_id: str
    request_id: str
    revision: int
    execution_generation: int
    workflow_id: str
    dispatch_id: str
    input_reference: str
    schema_version: str
    heartbeat_interval_seconds: float = 60.0


@dataclass
class OnboardingDispatchResult:
    status: str
    detail: str = ""


@workflow.defn(name="OnboardingDispatch", sandboxed=False)
class OnboardingDispatchWorkflow:
    """Long-running dispatch lifecycle: verify, heartbeat, cancel-or-terminate.

    Completes only via cancellation (operator or request state) or terminal
    failure handling. Domain completion arrives with Phase 3; until then the
    workflow holds the execution open and observable rather than inventing an
    outcome.
    """

    def __init__(self) -> None:
        self._cancel_requested = False

    @workflow.signal
    def cancel(self) -> None:
        self._cancel_requested = True

    @workflow.run
    async def run(
        self, wf_input: OnboardingDispatchWorkflowInput
    ) -> OnboardingDispatchResult:
        snapshot = await workflow.execute_activity_method(
            _activities.OnboardingActivities.verify_revision,
            VerifyRevisionInput(
                tenant_id=wf_input.tenant_id,
                request_id=wf_input.request_id,
                revision=wf_input.revision,
                execution_generation=wf_input.execution_generation,
            ),
            start_to_close_timeout=timedelta(minutes=5),
        )
        if not snapshot.proceed:
            if snapshot.reason == "cancellation requested":
                return await self._finish_canceled(wf_input)
            return OnboardingDispatchResult(
                status="failed", detail=snapshot.reason
            )
        while True:
            heartbeat = await workflow.execute_activity_method(
                _activities.OnboardingActivities.heartbeat,
                HeartbeatInput(
                    tenant_id=wf_input.tenant_id,
                    request_id=wf_input.request_id,
                    revision=wf_input.revision,
                ),
                start_to_close_timeout=timedelta(minutes=5),
            )
            if heartbeat.cancel_requested or self._cancel_requested:
                return await self._finish_canceled(wf_input)
            if not heartbeat.alive:
                return OnboardingDispatchResult(
                    status="failed", detail=heartbeat.detail
                )
            await workflow.wait_condition(
                lambda: self._cancel_requested,
                timeout=timedelta(seconds=wf_input.heartbeat_interval_seconds),
            )

    async def _finish_canceled(
        self, wf_input: OnboardingDispatchWorkflowInput
    ) -> OnboardingDispatchResult:
        await workflow.execute_activity_method(
            _activities.OnboardingActivities.complete_cancellation,
            CompleteCancellationInput(
                tenant_id=wf_input.tenant_id,
                dispatch_id=wf_input.dispatch_id,
                request_id=wf_input.request_id,
                revision=wf_input.revision,
            ),
            start_to_close_timeout=timedelta(minutes=5),
        )
        return OnboardingDispatchResult(status="canceled", detail="cancel complete")
