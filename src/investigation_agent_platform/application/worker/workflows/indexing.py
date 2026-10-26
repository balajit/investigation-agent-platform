# src/investigation_agent_platform/application/worker/workflows/indexing.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from investigation_agent_platform.domain.common.background_job import (
        BackgroundJobStatus,
    )

with workflow.unsafe.imports_passed_through():
    from investigation_agent_platform.application.worker.activities import (
        RunReindexInput,
        UpdateReindexJobInput,
        run_reindex_activity,
        update_reindex_job_activity,
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
