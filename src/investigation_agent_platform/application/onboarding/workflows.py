# src/investigation_agent_platform/application/onboarding/workflows.py
"""Phase 2 onboarding workflow definitions (deterministic, Temporal-native).

The dispatch lifecycle workflow below is intentionally thin: verify, file a
deterministic coverage analysis (DEP-07), heartbeat with cancellation
checkpoints, and hand terminal outcomes to activities that
perform CAS-guarded writes. Domain adapters arrived with Phase 3; the
workflow files their deterministic analysis output and never invents an
outcome beyond what activities durably record.

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
    from investigation_agent_platform.application.onboarding.steps import (
        AnalyzeCoverageInput,
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
    """Long-running dispatch lifecycle: verify, analyze, heartbeat, or terminate.

    Completes via cancellation, terminal failure handling, or a failed
    deterministic coverage analysis (DEP-07: assembled from stored probe
    evidence, no model judgment, no phantom progress).
    """

    def __init__(self) -> None:
        self._cancel_requested = False

    @workflow.signal
    def cancel(self) -> None:
        self._cancel_requested = True

    @workflow.run
    async def run(self, wf_input: OnboardingDispatchWorkflowInput) -> OnboardingDispatchResult:
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
            return OnboardingDispatchResult(status="failed", detail=snapshot.reason)
        try:
            await workflow.execute_activity_method(
                _activities.OnboardingActivities.analyze_coverage,
                AnalyzeCoverageInput(
                    tenant_id=wf_input.tenant_id,
                    request_id=wf_input.request_id,
                    revision=wf_input.revision,
                    generation=wf_input.execution_generation,
                    snapshot_reference=wf_input.input_reference,
                    dispatch_id=wf_input.dispatch_id,
                ),
                start_to_close_timeout=timedelta(minutes=5),
            )
        except Exception as exc:
            return OnboardingDispatchResult(
                status="failed", detail=f"coverage analysis failed: {exc}"
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
                return OnboardingDispatchResult(status="failed", detail=heartbeat.detail)
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
