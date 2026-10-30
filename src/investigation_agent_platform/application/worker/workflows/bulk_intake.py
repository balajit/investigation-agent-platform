# src/investigation_agent_platform/application/worker/workflows/bulk_intake.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
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
        LoadBatchRecordsInput,
        MarkBatchRecordResultInput,
        MarkBatchRecordsInput,
        ResolveBatchStragglersInput,
        SummarizeBatchInput,
        UpdateBatchJobInput,
        cancel_batch_children_activity,
        load_batch_records_activity,
        mark_batch_record_result_activity,
        mark_batch_records_activity,
        resolve_batch_stragglers_activity,
        summarize_batch_activity,
        update_batch_job_activity,
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


from .run_investigation import RunInvestigationInput


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
            if terminal in ("CANCELED", "CANCELLED"):
                # P0 create-race fix: a not-startable duplicate reports
                # CANCELED with its primary link (recorded, not retried —
                # retrying the same CANCELLED investigation could never
                # succeed). DONE here would overclaim a run that never ran.
                return (index, "CANCELED", getattr(result, "error", None))
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
