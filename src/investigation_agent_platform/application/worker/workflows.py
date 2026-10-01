# src/investigation_agent_platform/application/worker/workflows.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from investigation_agent_platform.domain.common.background_job import (
        BackgroundJobStatus,
    )
    from investigation_agent_platform.domain.intake.batch import (
        MAX_ACTIVE_CHILDREN,
        child_workflow_id,
    )

with workflow.unsafe.imports_passed_through():
    from investigation_agent_platform.application.worker.activities import (
        CancelBatchChildrenInput,
        CaptureKnowledgeInput,
        CheckpointInput,
        CollectSnapshotsInput,
        CollectSnapshotsOutput,
        ConcludeInvestigationInput,
        CreateInvestigationInput,
        ExecuteActionInput,
        LoadBatchRecordsInput,
        MarkBatchRecordResultInput,
        MarkBatchRecordsInput,
        PromoteFulfillmentEvidenceInput,
        PublishEventInput,
        ReasonInput,
        RecordInputRequirementInput,
        ResolveBatchStragglersInput,
        RetrieveEvidenceInput,
        RetrieveKnowledgeInput,
        RunClusteringInput,
        RunReindexInput,
        RunReportInput,
        SetInputRequirementStateInput,
        SummarizeBatchInput,
        SweepKnowledgeInput,
        TransitionInvestigationInput,
        UpdateBatchJobInput,
        UpdateClusteringJobInput,
        UpdateReindexJobInput,
        UpdateReportJobInput,
        VerifyRootCauseInput,
        cancel_batch_children_activity,
        capture_knowledge_activity,
        checkpoint_activity,
        collect_snapshots_activity,
        conclude_investigation_activity,
        create_investigation_activity,
        execute_action_activity,
        load_batch_records_activity,
        mark_batch_record_result_activity,
        mark_batch_records_activity,
        promote_fulfillment_evidence_activity,
        publish_event_activity,
        reason_activity,
        record_input_requirement_activity,
        resolve_batch_stragglers_activity,
        retrieve_evidence_activity,
        retrieve_knowledge_activity,
        run_clustering_activity,
        run_reindex_activity,
        run_report_activity,
        set_input_requirement_state_activity,
        summarize_batch_activity,
        sweep_knowledge_activity,
        transition_investigation_activity,
        update_batch_job_activity,
        update_clustering_job_activity,
        update_reindex_job_activity,
        update_report_job_activity,
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
        # Part 11.5: pending/fulfilled input requirement state. Plain JSON-able
        # dicts only, so a future Continue-As-New carries them without custom
        # serialization. The durable record of truth is the input_requirements
        # table; this memory is just the wait gate.
        self._pending_requirement: dict[str, Any] | None = None
        self._fulfilled_input: dict[str, Any] | None = None

    @workflow.signal
    async def pause(self) -> None:
        self._is_paused = True

    @workflow.signal
    async def resume(self) -> None:
        self._is_paused = False

    @workflow.signal
    async def cancel(self) -> None:
        self._is_cancelled = True

    @workflow.update
    async def fulfill_input(
        self, requirement_id: str, version: int, data: dict[str, Any]
    ) -> dict[str, Any]:
        """Accept caller-supplied input for the pending requirement.

        Validation split: the API layer validates auth, tenant, JSON Schema,
        size, and the durable compare-and-set before invoking this Update; the
        validator below re-checks id/version against in-memory workflow state
        (validators cannot do I/O). Identical replays overwrite with identical
        content (no-op); conflicting content for the same version never
        reaches here because the API rejects it with 409 first.
        """
        self._fulfilled_input = {
            "requirement_id": requirement_id,
            "version": version,
            "data": data,
        }
        return {
            "accepted": True,
            "requirement_id": requirement_id,
            "version": version,
        }

    @fulfill_input.validator
    def validate_fulfill_input(
        self, requirement_id: str, version: int, data: dict[str, Any]
    ) -> None:
        pending = self._pending_requirement
        if pending is None:
            raise ApplicationError("no pending input requirement", non_retryable=True)
        if requirement_id != pending.get("requirement_id") or version != pending.get(
            "requirement_version"
        ):
            raise ApplicationError("stale or unknown input requirement", non_retryable=True)

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

        async def _maybe_handle_input_required(
            result_data: dict[str, Any], resume_status: str
        ) -> str:
            """Handle an activity's ``input_required`` marker (Part 11.5).

            Returns "continue", "cancelled", or "expired". A pause does NOT
            block input fulfillment (pause halts work, not data delivery);
            cancellation and expiry do terminate the wait. On expiry the
            investigation transitions FAILED fail-closed: proceeding without
            required input would fabricate conclusions.
            """
            marker = result_data.get("input_required")
            if not marker or not isinstance(marker, dict):
                return "continue"
            record_res = await workflow.execute_activity(
                record_input_requirement_activity,
                RecordInputRequirementInput(
                    tenant_id=tenant_id,
                    investigation_id=inv_id,
                    reason=str(marker.get("reason", "Additional input required")),
                    json_schema=dict(marker.get("json_schema", {}) or {}),
                    classification=str(marker.get("classification", "INTERNAL")),
                    resume_status=resume_status,
                    promote_to_evidence=bool(marker.get("promote_to_evidence", False)),
                    expires_in_seconds=marker.get("expires_in_seconds"),
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
            if not record_res.success:
                workflow.logger.warning("input requirement recording failed: %s", record_res.error)
                return "continue"
            req_id = str(record_res.data.get("requirement_id", ""))
            req_version = int(record_res.data.get("requirement_version", 1))
            raw_timeout = record_res.data.get("wait_timeout_seconds")
            wait_timeout = float(raw_timeout) if isinstance(raw_timeout, (int, float)) else None
            await _transition("AWAITING_INPUT", f"Waiting for input: {req_id}")
            self._pending_requirement = {
                "requirement_id": req_id,
                "requirement_version": req_version,
            }
            self._fulfilled_input = None
            try:
                await workflow.wait_condition(
                    lambda: self._fulfilled_input is not None or self._is_cancelled,
                    timeout=wait_timeout,
                )
            except TimeoutError:
                await workflow.execute_activity(
                    set_input_requirement_state_activity,
                    SetInputRequirementStateInput(
                        tenant_id=tenant_id,
                        requirement_id=req_id,
                        expected_version=req_version,
                        state="EXPIRED",
                    ),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=retry_policy,
                )
                self._pending_requirement = None
                await _transition("FAILED", f"Input requirement {req_id} expired")
                return "expired"
            if self._is_cancelled and self._fulfilled_input is None:
                await workflow.execute_activity(
                    set_input_requirement_state_activity,
                    SetInputRequirementStateInput(
                        tenant_id=tenant_id,
                        requirement_id=req_id,
                        expected_version=req_version,
                        state="CANCELLED",
                    ),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=retry_policy,
                )
                self._pending_requirement = None
                return "cancelled"
            fulfilled: dict[str, Any] = self._fulfilled_input or {}
            await workflow.execute_activity(
                set_input_requirement_state_activity,
                SetInputRequirementStateInput(
                    tenant_id=tenant_id,
                    requirement_id=req_id,
                    expected_version=req_version,
                    state="FULFILLED",
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
            if record_res.data.get("promote_to_evidence"):
                await workflow.execute_activity(
                    promote_fulfillment_evidence_activity,
                    PromoteFulfillmentEvidenceInput(
                        tenant_id=tenant_id,
                        investigation_id=inv_id,
                        requirement_id=req_id,
                        data=dict(fulfilled.get("data", {}) or {}),
                        classification=str(marker.get("classification", "INTERNAL")),
                    ),
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=retry_policy,
                )
            resume = str(record_res.data.get("resume_status", resume_status))
            self._pending_requirement = None
            self._fulfilled_input = None
            await _transition(resume, f"Input {req_id} fulfilled; resuming")
            return "continue"

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

            # Part 11.5: an activity may declare it needs caller-supplied
            # input before the run can proceed.
            input_state = await _maybe_handle_input_required(reason_res.data, "INVESTIGATING")
            if input_state == "cancelled":
                break
            if input_state == "expired":
                last_error = "input_expired"
                break

            # Execute proposed action (EvidenceGateway dispatch + sanitization + persistence).
            execute_res = await workflow.execute_activity(
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

            input_state = await _maybe_handle_input_required(execute_res.data, "INVESTIGATING")
            if input_state == "cancelled":
                break
            if input_state == "expired":
                last_error = "input_expired"
                break

            # Retrieve evidence (EvidenceGateway).
            retrieve_res = await workflow.execute_activity(
                retrieve_evidence_activity,
                RetrieveEvidenceInput(
                    tenant_id=tenant_id,
                    investigation_id=inv_id,
                    application_id=input_data.application_id,
                ),
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=retry_policy,
            )

            input_state = await _maybe_handle_input_required(retrieve_res.data, "INVESTIGATING")
            if input_state == "cancelled":
                break
            if input_state == "expired":
                last_error = "input_expired"
                break

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

            input_state = await _maybe_handle_input_required(verify_res.data, "VERIFYING")
            if input_state == "cancelled":
                break
            if input_state == "expired":
                last_error = "input_expired"
                break

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
        return KnowledgeArtifactJanitorResult(per_tenant=dict(data.get("per_tenant", {})))


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


@dataclass
class FindingClusteringInput:
    tenant_id: str
    job_id: str = ""
    limit: int = 500
    batch_size: int = 25


@dataclass
class FindingClusteringResult:
    tenant_id: str = ""
    job_id: str = ""
    examined: int = 0
    assigned: int = 0
    unassigned: int = 0
    new_clusters: int = 0
    taxonomy_revision: int = 0
    error: str | None = None


@workflow.defn
class FindingClusteringWorkflow:
    """Scheduled/on-demand incremental finding clustering (Part 11.7).

    Single-activity run keeps history tiny regardless of corpus size;
    resumability comes from assignment persistence (re-invocation skips
    assigned findings). Progress is tracked on the linked BackgroundJob row.
    """

    @workflow.run
    async def run(self, input_data: FindingClusteringInput) -> FindingClusteringResult:
        retry_policy = RetryPolicy(
            maximum_attempts=3,
            non_retryable_error_types=[
                "SecurityPolicyViolationException",
                "DomainValidationException",
                "PlatformConfigurationError",
            ],
        )
        if input_data.job_id:
            await workflow.execute_activity(
                update_clustering_job_activity,
                UpdateClusteringJobInput(
                    tenant_id=input_data.tenant_id,
                    job_id=input_data.job_id,
                    status=BackgroundJobStatus.RUNNING.value,
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
        res = await workflow.execute_activity(
            run_clustering_activity,
            RunClusteringInput(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                limit=input_data.limit,
                batch_size=input_data.batch_size,
            ),
            start_to_close_timeout=timedelta(seconds=1800),
            retry_policy=retry_policy,
        )
        data = res.data
        if not res.success:
            await workflow.execute_activity(
                update_clustering_job_activity,
                UpdateClusteringJobInput(
                    tenant_id=input_data.tenant_id,
                    job_id=input_data.job_id,
                    status=BackgroundJobStatus.FAILED.value,
                    error=data.get("error"),
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
            return FindingClusteringResult(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                error=data.get("error"),
            )
        await workflow.execute_activity(
            update_clustering_job_activity,
            UpdateClusteringJobInput(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                status=BackgroundJobStatus.DONE.value,
                stage={
                    "name": "clustered",
                    "message": (
                        f"examined={data.get('examined', 0)} "
                        f"assigned={data.get('assigned', 0)} "
                        f"unassigned={data.get('unassigned', 0)}"
                    ),
                },
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )
        return FindingClusteringResult(
            tenant_id=input_data.tenant_id,
            job_id=input_data.job_id,
            examined=int(data.get("examined", 0)),
            assigned=int(data.get("assigned", 0)),
            unassigned=int(data.get("unassigned", 0)),
            new_clusters=int(data.get("new_clusters", 0)),
            taxonomy_revision=int(data.get("taxonomy_revision", 0)),
        )


@dataclass
class AggregateReportInput:
    tenant_id: str
    job_id: str = ""
    legal_hold: bool = False


@dataclass
class AggregateReportResult:
    tenant_id: str = ""
    job_id: str = ""
    taxonomy_revision: int = 0
    assignment_count: int = 0
    artifact_keys: list[str] = field(default_factory=list)
    error: str | None = None


@workflow.defn
class AggregateReportWorkflow:
    """On-demand aggregate reporting over one taxonomy generation (Part 11.8).

    Bounded by construction: taxonomy ≤25 clusters, members ≤200 per
    cluster, one render activity. History stays tiny, so no Continue-As-New
    is required (inputs cannot approach history/event thresholds).
    Progress is tracked on the linked BackgroundJob row.
    """

    @workflow.run
    async def run(self, input_data: AggregateReportInput) -> AggregateReportResult:
        retry_policy = RetryPolicy(
            maximum_attempts=3,
            non_retryable_error_types=[
                "SecurityPolicyViolationException",
                "DomainValidationException",
                "PlatformConfigurationError",
            ],
        )
        if input_data.job_id:
            await workflow.execute_activity(
                update_report_job_activity,
                UpdateReportJobInput(
                    tenant_id=input_data.tenant_id,
                    job_id=input_data.job_id,
                    status=BackgroundJobStatus.RUNNING.value,
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
        res = await workflow.execute_activity(
            run_report_activity,
            RunReportInput(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                legal_hold=input_data.legal_hold,
            ),
            start_to_close_timeout=timedelta(seconds=1800),
            retry_policy=retry_policy,
        )
        data = res.data
        if not res.success:
            await workflow.execute_activity(
                update_report_job_activity,
                UpdateReportJobInput(
                    tenant_id=input_data.tenant_id,
                    job_id=input_data.job_id,
                    status=BackgroundJobStatus.FAILED.value,
                    error=data.get("error"),
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
            return AggregateReportResult(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                error=data.get("error"),
            )
        await workflow.execute_activity(
            update_report_job_activity,
            UpdateReportJobInput(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                status=BackgroundJobStatus.DONE.value,
                stage={
                    "name": "reported",
                    "message": (
                        f"revision={data.get('taxonomy_revision', 0)} "
                        f"assignments={data.get('assignment_count', 0)}"
                    ),
                },
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )
        return AggregateReportResult(
            tenant_id=input_data.tenant_id,
            job_id=input_data.job_id,
            taxonomy_revision=int(data.get("taxonomy_revision", 0)),
            assignment_count=int(data.get("assignment_count", 0)),
            artifact_keys=list(data.get("artifact_keys", [])),
        )


@dataclass
class ReferenceDocsIndexInput:
    tenant_id: str
    job_id: str = ""
    source_id: str = ""
    application_id: str = ""


@dataclass
class ReferenceDocsIndexResult:
    tenant_id: str = ""
    job_id: str = ""
    source_id: str = ""
    generation: int = 0
    files_seen: int = 0
    chunks_written: int = 0
    tombstoned: int = 0
    error: str | None = None


@workflow.defn
class ReferenceDocsIndexWorkflow:
    """Source indexing over bounded runs (Part 11.6).

    One bounded activity per run (file/chunk caps enforced in the service);
    larger sources partition into multiple jobs rather than growing one
    history. Resumability across failures comes from content-hash compare
    (re-invocation rewrites nothing unchanged). Progress is tracked on the
    linked BackgroundJob row.
    """

    @workflow.run
    async def run(self, input_data: ReferenceDocsIndexInput) -> ReferenceDocsIndexResult:
        retry_policy = RetryPolicy(
            maximum_attempts=3,
            non_retryable_error_types=[
                "SecurityPolicyViolationException",
                "DomainValidationException",
                "PlatformConfigurationError",
            ],
        )
        if input_data.job_id:
            await workflow.execute_activity(
                update_reindex_job_activity,
                UpdateReindexJobInput(
                    tenant_id=input_data.tenant_id,
                    job_id=input_data.job_id,
                    status=BackgroundJobStatus.RUNNING.value,
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
        res = await workflow.execute_activity(
            run_reindex_activity,
            RunReindexInput(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                source_id=input_data.source_id,
                application_id=input_data.application_id,
            ),
            start_to_close_timeout=timedelta(seconds=1800),
            retry_policy=retry_policy,
        )
        data = res.data
        if not res.success:
            await workflow.execute_activity(
                update_reindex_job_activity,
                UpdateReindexJobInput(
                    tenant_id=input_data.tenant_id,
                    job_id=input_data.job_id,
                    status=BackgroundJobStatus.FAILED.value,
                    error=data.get("error"),
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
            return ReferenceDocsIndexResult(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                source_id=input_data.source_id,
                error=data.get("error"),
            )
        await workflow.execute_activity(
            update_reindex_job_activity,
            UpdateReindexJobInput(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                status=BackgroundJobStatus.DONE.value,
                stage={
                    "name": "indexed",
                    "message": (
                        f"generation={data.get('generation', 0)} "
                        f"files={data.get('files_seen', 0)} "
                        f"chunks={data.get('chunks_written', 0)}"
                    ),
                },
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )
        return ReferenceDocsIndexResult(
            tenant_id=input_data.tenant_id,
            job_id=input_data.job_id,
            source_id=input_data.source_id,
            generation=int(data.get("generation", 0)),
            files_seen=int(data.get("files_seen", 0)),
            chunks_written=int(data.get("chunks_written", 0)),
            tombstoned=int(data.get("tombstoned", 0)),
        )


@dataclass
class BulkIntakeInput:
    tenant_id: str
    job_id: str = ""
    start_index: int = 0
    attempt: int = 0
    patience_seconds: int = 3600


@dataclass
class BulkIntakeResult:
    tenant_id: str = ""
    job_id: str = ""
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    awaiting_input: int = 0
    canceled: int = 0
    continued_as_new: bool = False
    error: str | None = None


def should_continue_as_new(
    start_index: int, dispatched_this_run: int, remaining: int, active_count: int
) -> bool:
    """Pure CAN predicate (Part 11.9): roll only with zero active children.

    Fires when this run already dispatched past CAN_RECORD_THRESHOLD and
    work remains. Results and idempotency live in record rows, so the new
    execution resumes purely from the dispatch cursor.
    """
    from investigation_agent_platform.domain.intake.batch import CAN_RECORD_THRESHOLD

    return (
        active_count == 0
        and remaining > 0
        and start_index + dispatched_this_run >= CAN_RECORD_THRESHOLD
    )


@workflow.defn
class BulkIntakeWorkflow:
    """Bounded bulk fan-out over pre-created investigations (Part 11.9).

    Children are `RunInvestigationWorkflow` executions with deterministic
    opaque ids (`wf-batch-{job}-{index}` + `-r{attempt}` on retry). At most
    MAX_ACTIVE_CHILDREN run concurrently — a window slot is held from child
    start through completion, never start-only. Parent close policy is
    ABANDON (awaiting-input children survive parent completion); parent
    cancellation fans out explicitly through cancel_batch_children_activity
    (auditable per child). Continue-As-New fires only with zero active
    children, carrying the dispatch cursor + attempt; results and
    idempotency live in the record rows, never in workflow memory. Children
    never retry at workflow level (maximum_attempts=1): a re-run would
    replay CREATED→CONTEXTUALIZING against an already-advanced
    investigation and violate the lifecycle. Transient faults are absorbed
    by activity-level retries; permanent child failure lands on the record
    row for API-level partial retry with attempt-suffixed child ids.
    """

    @workflow.run
    async def run(self, input_data: BulkIntakeInput) -> BulkIntakeResult:
        import asyncio as _asyncio

        retry_policy = RetryPolicy(
            maximum_attempts=3,
            non_retryable_error_types=[
                "SecurityPolicyViolationException",
                "DomainValidationException",
                "PlatformConfigurationError",
            ],
        )
        if input_data.job_id:
            await workflow.execute_activity(
                update_batch_job_activity,
                UpdateBatchJobInput(
                    tenant_id=input_data.tenant_id,
                    job_id=input_data.job_id,
                    status=BackgroundJobStatus.RUNNING.value,
                ),
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=retry_policy,
            )
        loaded = await workflow.execute_activity(
            load_batch_records_activity,
            LoadBatchRecordsInput(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                start_index=input_data.start_index,
            ),
            start_to_close_timeout=timedelta(seconds=120),
            retry_policy=retry_policy,
        )
        if not loaded.success:
            return await self._fail(input_data, retry_policy, str(loaded.error or "load failed"))
        pending: list[dict[str, Any]] = list(loaded.data.get("records", []))
        total: int = int(loaded.data.get("total", 0))
        if not pending:
            return await self._finish(input_data, retry_policy, total)

        active: dict[int, Any] = {}
        queue = pending
        dispatched = 0
        try:
            try:
                async with _asyncio.timeout(input_data.patience_seconds):
                    while queue or active:
                        while queue and len(active) < MAX_ACTIVE_CHILDREN:
                            record = queue.pop(0)
                            index = int(record["record_index"])
                            child_id = child_workflow_id(
                                input_data.job_id,
                                index,
                                input_data.attempt,
                            )
                            # String workflow type: the SDK resolves the
                            # definition at runtime; the class object does
                            # not satisfy the static child-workflow signature.
                            task = _asyncio.create_task(
                                self._run_one_child(input_data, record, index, child_id)
                            )
                            await workflow.execute_activity(
                                mark_batch_records_activity,
                                MarkBatchRecordsInput(
                                    tenant_id=input_data.tenant_id,
                                    job_id=input_data.job_id,
                                    record_indices=[index],
                                    status="RUNNING",
                                    child_workflow_ids={str(index): child_id},
                                ),
                                start_to_close_timeout=timedelta(seconds=60),
                                retry_policy=retry_policy,
                            )
                            active[index] = task
                            dispatched += 1
                        if not active:
                            break
                        done, _pending_tasks = await _asyncio.wait(
                            set(active.values()), return_when=_asyncio.FIRST_COMPLETED
                        )
                        for task in done:
                            index, child_status, child_error = await task
                            active.pop(index, None)
                            await workflow.execute_activity(
                                mark_batch_record_result_activity,
                                MarkBatchRecordResultInput(
                                    tenant_id=input_data.tenant_id,
                                    job_id=input_data.job_id,
                                    record_index=index,
                                    status=child_status,
                                    error=child_error,
                                ),
                                start_to_close_timeout=timedelta(seconds=60),
                                retry_policy=retry_policy,
                            )
                            if should_continue_as_new(
                                input_data.start_index, dispatched, len(queue), len(active)
                            ):
                                return await self._continue_as_new(
                                    input_data, retry_policy, queue[0]["record_index"]
                                )
            except TimeoutError:
                await workflow.execute_activity(
                    resolve_batch_stragglers_activity,
                    ResolveBatchStragglersInput(
                        tenant_id=input_data.tenant_id,
                        job_id=input_data.job_id,
                        record_indices=sorted(active),
                    ),
                    start_to_close_timeout=timedelta(seconds=300),
                    retry_policy=retry_policy,
                )
        except _asyncio.CancelledError:
            # Cancellation propagates explicitly to live children (ABANDON
            # close policy keeps them alive otherwise); best-effort here,
            # the API already marked the job CANCEL_REQUESTED.
            try:
                await workflow.execute_activity(
                    cancel_batch_children_activity,
                    CancelBatchChildrenInput(
                        tenant_id=input_data.tenant_id, job_id=input_data.job_id
                    ),
                    start_to_close_timeout=timedelta(seconds=300),
                    retry_policy=retry_policy,
                )
                await workflow.execute_activity(
                    update_batch_job_activity,
                    UpdateBatchJobInput(
                        tenant_id=input_data.tenant_id,
                        job_id=input_data.job_id,
                        status=BackgroundJobStatus.CANCELED.value,
                    ),
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=retry_policy,
                )
            except Exception:
                pass
            raise
        return await self._finish(input_data, retry_policy, total)

    async def _run_one_child(
        self, input_data: BulkIntakeInput, record: dict[str, Any], index: int, child_id: str
    ) -> tuple[int, str, str | None]:
        """Execute one child investigation; never raises (Part 11.9).

        Returns (record_index, terminal_status, error). Awaiting the child
        handle directly keeps the window slot held start→completion.
        """
        try:
            result = await workflow.execute_child_workflow(
                "RunInvestigationWorkflow",
                RunInvestigationInput(
                    application_id=str(record.get("application_id", "")),
                    tenant_id=input_data.tenant_id,
                    description=None,
                    investigation_id=record.get("investigation_id"),
                ),
                id=child_id,
                parent_close_policy=ParentClosePolicy.ABANDON,
                execution_timeout=timedelta(seconds=7200),
                run_timeout=timedelta(seconds=7200),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            terminal = str(getattr(result, "status", "") or "").upper()
            if terminal in ("COMPLETED", "DONE", "SUCCESS"):
                return (index, "DONE", None)
            if terminal in ("FAILED", "ERROR"):
                return (index, "FAILED", getattr(result, "error", None))
            if getattr(result, "error", None):
                return (index, "FAILED", str(getattr(result, "error", None))[:500])
            return (index, "DONE", None)
        except Exception as exc:
            return (index, "FAILED", str(exc)[:500])

    async def _finish(
        self, input_data: BulkIntakeInput, retry_policy: Any, total: int
    ) -> BulkIntakeResult:
        summary = await workflow.execute_activity(
            summarize_batch_activity,
            SummarizeBatchInput(tenant_id=input_data.tenant_id, job_id=input_data.job_id),
            start_to_close_timeout=timedelta(seconds=60),
            retry_policy=retry_policy,
        )
        data = summary.data if summary.success else {}
        await workflow.execute_activity(
            update_batch_job_activity,
            UpdateBatchJobInput(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                status=BackgroundJobStatus.DONE.value,
                stage={
                    "name": "intake-complete",
                    "message": (
                        f"total={data.get('total', total)} "
                        f"succeeded={data.get('succeeded', 0)} "
                        f"failed={data.get('failed', 0)} "
                        f"awaiting={data.get('awaiting_input', 0)}"
                    ),
                },
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )
        return BulkIntakeResult(
            tenant_id=input_data.tenant_id,
            job_id=input_data.job_id,
            total=int(data.get("total", total)),
            succeeded=int(data.get("succeeded", 0)),
            failed=int(data.get("failed", 0)),
            awaiting_input=int(data.get("awaiting_input", 0)),
            canceled=int(data.get("canceled", 0)),
        )

    async def _fail(
        self, input_data: BulkIntakeInput, retry_policy: Any, error: str
    ) -> BulkIntakeResult:
        await workflow.execute_activity(
            update_batch_job_activity,
            UpdateBatchJobInput(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                status=BackgroundJobStatus.FAILED.value,
                error=error,
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )
        return BulkIntakeResult(
            tenant_id=input_data.tenant_id, job_id=input_data.job_id, error=error
        )

    async def _continue_as_new(
        self, input_data: BulkIntakeInput, retry_policy: Any, next_index: int
    ) -> BulkIntakeResult:
        # Zero active children is guaranteed by the caller (post-settle,
        # `active` empty). Results + idempotency live in record rows.
        await workflow.execute_activity(
            update_batch_job_activity,
            UpdateBatchJobInput(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                status="RUNNING",
                stage={"name": "continue-as-new", "message": f"cursor={next_index}"},
            ),
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )
        workflow.continue_as_new(
            BulkIntakeInput(
                tenant_id=input_data.tenant_id,
                job_id=input_data.job_id,
                start_index=next_index,
                attempt=input_data.attempt,
                patience_seconds=input_data.patience_seconds,
            )
        )
        raise RuntimeError("unreachable: continue_as_new never returns")
