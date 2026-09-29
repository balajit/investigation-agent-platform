# src/investigation_agent_platform/application/worker/workflows.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from investigation_agent_platform.application.worker.activities import (
        CaptureKnowledgeInput,
        CheckpointInput,
        CollectSnapshotsInput,
        CollectSnapshotsOutput,
        ConcludeInvestigationInput,
        CreateInvestigationInput,
        ExecuteActionInput,
        PublishEventInput,
        ReasonInput,
        RetrieveEvidenceInput,
        RetrieveKnowledgeInput,
        SweepKnowledgeInput,
        TransitionInvestigationInput,
        VerifyRootCauseInput,
        capture_knowledge_activity,
        checkpoint_activity,
        collect_snapshots_activity,
        conclude_investigation_activity,
        create_investigation_activity,
        execute_action_activity,
        publish_event_activity,
        reason_activity,
        retrieve_evidence_activity,
        retrieve_knowledge_activity,
        sweep_knowledge_activity,
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
    # The investigation was already created by the API before the workflow was
    # started (`wf-investigation-{investigation_id}`). This field is the
    # single source of truth for the investigation identity across the whole
    # run: API response, workflow ID, activity inputs, checkpoints, evidence,
    # and topology attribution must all refer to this ID. When absent (legacy
    # callers / tests), the workflow falls back to creating a new investigation.
    investigation_id: str | None = None


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
        # F-IDENTITY: if the API already created the investigation, load it
        # instead of creating a second aggregate under a different ID.
        create_res = await workflow.execute_activity(
            create_investigation_activity,
            CreateInvestigationInput(
                application_id=input_data.application_id,
                session_id=input_data.session_id,
                tenant_id=input_data.tenant_id,
                description=input_data.description,
                investigation_id=input_data.investigation_id,
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
                    tenant_id=tenant_id,
                    investigation_id=inv_id,
                    target_status=target,
                    reason=reason,
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
        # WORKFLOW_LOCAL_ITERATION_GUARD is a determinism-safe backstop only, not
        # the budget: real limits live in infra BudgetConfig (operator knob) and
        # are enforced via activity metadata signals (domain InvestigationBudget).
        # Do not tune investigation budgets here; change BudgetConfig instead.
        WORKFLOW_LOCAL_ITERATION_GUARD = 10
        max_iterations = WORKFLOW_LOCAL_ITERATION_GUARD
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

            # Part 6 Slice 0: validity-gated knowledge retrieval before
            # reasoning. Degrades to unassisted reasoning on store trouble;
            # never fails the investigation.
            await workflow.execute_activity(
                retrieve_knowledge_activity,
                RetrieveKnowledgeInput(tenant_id=tenant_id, investigation_id=inv_id),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )

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
                await _transition(
                    "HYPOTHESIZING", f"Iteration {iteration} loop back to hypothesizing"
                )

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

        # Part 6 Slice 0: distill concluded state into knowledge envelopes.
        # Capture failures never fail the investigation.
        await workflow.execute_activity(
            capture_knowledge_activity,
            CaptureKnowledgeInput(tenant_id=tenant_id, investigation_id=inv_id),
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


@dataclass
class TopologySnapshotRetentionInput:
    """One scheduled retention pass over a set of repositories (ISSUE-4).

    Intended to be started on a Temporal ``Schedule`` (e.g. daily) — either
    one schedule per ``(tenant_id, repository_id)`` pair, or one schedule
    invoking this workflow with the full list an operator maintains. Each
    repository is collected via its own retried activity so one failing
    repository never blocks the rest of the pass.
    """

    repositories: list[tuple[str, str]]  # (tenant_id, repository_id) pairs


@dataclass
class TopologySnapshotRetentionResult:
    per_repository: dict[str, CollectSnapshotsOutput]


@dataclass
class KnowledgeArtifactJanitorInput:
    """Scheduled janitor pass over a set of tenants (ISSUE-9)."""

    tenants: list[str]
    reverify_conditional: bool = False


@dataclass
class KnowledgeArtifactJanitorResult:
    per_tenant: dict[str, int]


@workflow.defn
class KnowledgeArtifactJanitorWorkflow:
    """Scheduled sweep: TTL-elapsed -> EXPIRED via the retrieval transition
    path. Idempotent; never mutates history, only advances status."""

    @workflow.run
    async def run(
        self, input_data: KnowledgeArtifactJanitorInput
    ) -> KnowledgeArtifactJanitorResult:
        retry_policy = RetryPolicy(maximum_attempts=3)
        output = await workflow.execute_activity(
            sweep_knowledge_activity,
            SweepKnowledgeInput(
                tenants=list(input_data.tenants),
                reverify_conditional=input_data.reverify_conditional,
            ),
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=retry_policy,
        )
        data = output.data if hasattr(output, "data") else {}
        return KnowledgeArtifactJanitorResult(
            per_tenant=dict(data.get("per_tenant", {}))
        )


@workflow.defn
class TopologySnapshotRetentionWorkflow:
    """Scheduled janitor: classifies and collects unpinned, out-of-window
    topology snapshots per repository (ISSUE-4). Never touches a snapshot
    referenced by an open investigation's own recorded evidence, and never
    deletes a ``TopologySnapshot`` audit node — only the AST subgraph
    beneath it.
    """

    @workflow.run
    async def run(
        self, input_data: TopologySnapshotRetentionInput
    ) -> TopologySnapshotRetentionResult:
        retry_policy = RetryPolicy(maximum_attempts=3)
        results: dict[str, CollectSnapshotsOutput] = {}
        for tenant_id, repository_id in input_data.repositories:
            output = await workflow.execute_activity(
                collect_snapshots_activity,
                CollectSnapshotsInput(tenant_id=tenant_id, repository_id=repository_id),
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=retry_policy,
            )
            results[f"{tenant_id}/{repository_id}"] = output
        return TopologySnapshotRetentionResult(per_repository=results)
