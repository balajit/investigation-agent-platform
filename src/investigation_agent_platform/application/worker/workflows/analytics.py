# src/investigation_agent_platform/application/worker/workflows/analytics.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from investigation_agent_platform.domain.common.background_job import (
        BackgroundJobStatus,
    )

with workflow.unsafe.imports_passed_through():
    from investigation_agent_platform.application.worker.activities import (
        RunClusteringInput,
        RunReportInput,
        UpdateClusteringJobInput,
        UpdateReportJobInput,
        run_clustering_activity,
        run_report_activity,
        update_clustering_job_activity,
        update_report_job_activity,
    )

with workflow.unsafe.imports_passed_through():
    # G6-measure shadow capture: pure compute-and-log helpers (no I/O,
    # no clock, no execution). Imported for the per-turn shadow replay;
    # the workflow never branches on its output.

    # D3 Phase 0 shadow definitions: pure receipt pairing + summary
    # (canonical digests only — deterministic, replay-safe). The
    # workflow never branches on the paired output.
    pass

with workflow.unsafe.imports_passed_through():
    # G3: ledger directive helpers — pure, deterministic, replay-safe
    # (hashlib/re only at call time; no I/O, no clock, no process-local
    # ledger reads). Imported passed-through like the shadow helpers
    # because the application layer is not import-safe in the sandbox.
    pass


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
