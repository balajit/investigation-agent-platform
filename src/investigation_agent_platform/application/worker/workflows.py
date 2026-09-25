# src/investigation_agent_platform/application/worker/workflows.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from investigation_agent_platform.application.worker.activities import (
        CheckpointInput,
        ConcludeInvestigationInput,
        CreateInvestigationInput,
        ExecuteActionInput,
        PublishEventInput,
        ReasonInput,
        RetrieveEvidenceInput,
        TransitionInvestigationInput,
        VerifyRootCauseInput,
        checkpoint_activity,
        conclude_investigation_activity,
        create_investigation_activity,
        execute_action_activity,
        publish_event_activity,
        reason_activity,
        retrieve_evidence_activity,
        transition_investigation_activity,
        verify_root_cause_activity,
    )


@dataclass
class RunInvestigationInput:
    application_id: str
    session_id: str | None = None
    tenant_id: str = ""
    description: str | None = None
    correlation_id: str | None = None


@dataclass
class RunInvestigationResult:
    investigation_id: str = ""
    status: str = ""
    error: str | None = None


@workflow.defn
class RunInvestigationWorkflow:
    def __init__(self) -> None:
        self._is_paused = False
        self._is_cancelled = False

    @workflow.signal
    async def pause(self) -> None:
        self._is_paused = True

    @workflow.signal
    async def resume(self) -> None:
        self._is_paused = False

    @workflow.signal
    async def cancel(self) -> None:
        self._is_cancelled = True

    @workflow.run
    async def run(self, input_data: RunInvestigationInput) -> RunInvestigationResult:
        retry_policy = RetryPolicy(
            maximum_attempts=3,
            non_retryable_error_types=[
                "SecurityPolicyViolationException",
                "DomainValidationException",
                "InvalidHypothesisException",
                "PlatformConfigurationError",
            ],
        )

        # 1. Initialize investigation (state authority: persisted via InvestigationRepository).
        create_res = await workflow.execute_activity(
            create_investigation_activity,
            CreateInvestigationInput(
                application_id=input_data.application_id,
                session_id=input_data.session_id,
                tenant_id=input_data.tenant_id,
                description=input_data.description,
            ),
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry_policy,
        )

        inv_id = create_res.investigation_id
        tenant_id = input_data.tenant_id

        async def _transition(target: str, reason: str) -> None:
            await workflow.execute_activity(
                transition_investigation_activity,
                TransitionInvestigationInput(
                    tenant_id=tenant_id, investigation_id=inv_id, target_status=target, reason=reason
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )

        # Drive state machine: CREATED -> CONTEXTUALIZING -> INVESTIGATING
        await _transition("CONTEXTUALIZING", "Workflow started contextualizing")
        await _transition("INVESTIGATING", "Workflow entering investigation loop")

        # Budget guard: wall-clock + token/cost + max iterations.
        # Deterministic: use workflow.now() would be ideal but workflow.time is not available in all SDKs;
        # we rely on iteration cap + InvestigationBudget signal from reason/execute metadata.
        max_iterations = 10
        # Lazy import for type safety without failing workflow determinism checks.
        # Budget is evaluated via activity results; workflow keeps local iteration guard.
        iteration = 0
        verified = False
        last_error: str | None = None

        # 2. Main Investigation Loop — state authority is the workflow + checkpoint repo.
        while iteration < max_iterations and not self._is_cancelled:
            # Handle pause/resume/cancel signals.
            await workflow.wait_condition(lambda: not self._is_paused)
            if self._is_cancelled:
                break

            # Reason — produces next action and readiness score.
            reason_res = await workflow.execute_activity(
                reason_activity,
                ReasonInput(tenant_id=tenant_id, investigation_id=inv_id, iteration=iteration),
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=retry_policy,
            )
            # Budget guard: if reason signals budget exceeded, break.
            if reason_res.data.get("budget_exceeded"):
                last_error = "budget_exceeded"
                break

            # Execute proposed action (EvidenceGateway dispatch + sanitization + persistence).
            await workflow.execute_activity(
                execute_action_activity,
                ExecuteActionInput(
                    tenant_id=tenant_id,
                    investigation_id=inv_id,
                    action=str(reason_res.data.get("next_action", "QUERY_STATE")),
                    parameters=dict(reason_res.data.get("action_parameters", {})),
                ),
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=retry_policy,
            )

            # Retrieve evidence (EvidenceGateway).
            await workflow.execute_activity(
                retrieve_evidence_activity,
                RetrieveEvidenceInput(
                    tenant_id=tenant_id,
                    investigation_id=inv_id,
                    application_id=input_data.application_id,
                ),
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=retry_policy,
            )

            # Drive correlation/hypothesis/verification phases via state machine
            await _transition("CORRELATING", f"Iteration {iteration} correlating")
            await _transition("HYPOTHESIZING", f"Iteration {iteration} hypothesizing")
            await _transition("VERIFYING", f"Iteration {iteration} verifying")

            # Verify root cause (VerificationEngine).
            verify_res = await workflow.execute_activity(
                verify_root_cause_activity,
                VerifyRootCauseInput(tenant_id=tenant_id, investigation_id=inv_id),
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=retry_policy,
            )

            # Checkpoint — persist InvestigationState snapshot via CheckpointRepository.
            await workflow.execute_activity(
                checkpoint_activity,
                CheckpointInput(tenant_id=tenant_id, investigation_id=inv_id, step=iteration),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )

            if verify_res.data.get("verified"):
                verified = True
                break

            # Also break on high conclusion readiness.
            readiness = reason_res.data.get("conclusion_readiness")
            if isinstance(readiness, (int, float)) and float(readiness) >= 0.95:
                break

            # Loop back: VERIFYING -> HYPOTHESIZING or INVESTIGATING for next iteration
            if iteration < max_iterations - 1 and not self._is_cancelled:
                await _transition("HYPOTHESIZING", f"Iteration {iteration} loop back to hypothesizing")

            iteration += 1

        # 3. Conclude & Publish Events (EventPublisher on completion).
        await _transition("CONCLUDING", "Workflow entering concluding phase")
        if self._is_cancelled:
            final_status = "CANCELLED"
        elif verified:
            final_status = "COMPLETED"
        elif last_error == "budget_exceeded":
            final_status = "FAILED"
        else:
            # Not verified: reached max_iterations or readiness threshold without verification -> FAILED with limitations
            final_status = "FAILED"
            if last_error is None:
                last_error = "verification_failed_or_limit_reached"

        await workflow.execute_activity(
            conclude_investigation_activity,
            ConcludeInvestigationInput(
                tenant_id=tenant_id, investigation_id=inv_id, status=final_status
            ),
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry_policy,
        )

        await workflow.execute_activity(
            publish_event_activity,
            PublishEventInput(
                tenant_id=tenant_id, investigation_id=inv_id, event="INVESTIGATION_FINISHED"
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )

        return RunInvestigationResult(
            investigation_id=inv_id, status=final_status, error=last_error
        )
