# src/investigation_agent_platform/application/worker/activities/batch.py
"""Temporal activity thin adapters — each delegates to the application/domain layer via AppContext."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from temporalio import activity

try:
    from temporalio.exceptions import ApplicationFailure  # type: ignore[attr-defined]
except ImportError:
    from temporalio.exceptions import (
        ApplicationError as ApplicationFailure,  # type: ignore[assignment, no-redef]
    )

logger = logging.getLogger(__name__)

from .common import (
    COMP_ACTVTY,
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    OUTCOME_STARTED,
    _application_failure_from_exc,
    _coerced_data,
    elapsed_ms,
    story_log,
    tick,
)
from .inputs import GenericActivityResult, _get_ctx


@dataclass
class LoadBatchRecordsInput:
    tenant_id: str
    job_id: str = ""
    start_index: int = 0


@dataclass
class MarkBatchRecordsInput:
    tenant_id: str
    job_id: str = ""
    record_indices: list[int] = None  # type: ignore[assignment]
    status: str = ""
    child_workflow_ids: dict[str, str] | None = None


@dataclass
class MarkBatchRecordResultInput:
    tenant_id: str
    job_id: str = ""
    record_index: int = 0
    status: str = ""
    error: str | None = None


@dataclass
class ResolveBatchStragglersInput:
    tenant_id: str
    job_id: str = ""
    record_indices: list[int] = None  # type: ignore[assignment]


@dataclass
class CancelBatchChildrenInput:
    tenant_id: str
    job_id: str = ""


@dataclass
class UpdateBatchJobInput:
    tenant_id: str
    job_id: str
    status: str = ""
    stage: dict[str, Any] | None = None
    error: str | None = None
    progress: int | None = None
    total: int | None = None


def _batch_uuid(value: str) -> Any:
    from uuid import UUID as _UUID

    try:
        return _UUID(value)
    except ValueError:
        return None


@activity.defn
async def load_batch_records_activity(
    params: LoadBatchRecordsInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — read-only cursor load; no writes.
    """Load PENDING batch records from a dispatch cursor (Part 11.9)."""
    t0 = tick()
    data = _coerced_data(LoadBatchRecordsInput, params, activity="load_batch_records_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    story_log(
        logger,
        logging.INFO,
        "load_batch_records_activity started",
        comp=COMP_ACTVTY,
        func="load_batch_records_activity",
        stage="intake",
        step="load",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        event="load_batch_records_activity started",
        inputs={"job_id": str(data.get("job_id", ""))},
        job_id=str(data.get("job_id", "")),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id = _batch_uuid(str(data.get("job_id", "")))
        if not tenant_id or job_id is None:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        try:
            start_index = max(0, int(data.get("start_index", 0)))
        except (TypeError, ValueError):
            return GenericActivityResult(success=False, error="start_index invalid")
        ctx = _get_ctx()
        repo = getattr(ctx, "batch_repo", None)
        if repo is None:
            return GenericActivityResult(success=False, error="batch repository unavailable")
        records, total = await repo.list_records(tenant_id, job_id, limit=500, offset=0)
        pending = [
            {
                "record_index": record.record_index,
                "investigation_id": str(record.investigation_id)
                if record.investigation_id
                else None,
                "application_id": record.application_id,
                "external_key": record.external_key,
                "attempt": record.attempt,
            }
            for record in records
            if record.record_index >= start_index and record.status.value == "PENDING"
        ]
        story_log(
            logger,
            logging.INFO,
            "load_batch_records_activity completed",
            comp=COMP_ACTVTY,
            func="load_batch_records_activity",
            stage="intake",
            step="load",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            event="load_batch_records_activity completed",
            outputs={"outcome": "loaded", "pending_count": len(pending), "total": total},
            elapsed_ms=elapsed_ms(t0),
            pending_count=len(pending),
            total=total,
        )
        return GenericActivityResult(success=True, data={"records": pending, "total": total})
    except ApplicationFailure:
        raise
    except Exception as exc:
        story_log(
            logger,
            logging.ERROR,
            "load_batch_records_activity failed",
            comp=COMP_ACTVTY,
            func="load_batch_records_activity",
            stage="intake",
            step="load",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            event="load_batch_records_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
            job_id=str(data.get("job_id", "")),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def mark_batch_records_activity(
    params: MarkBatchRecordsInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — convergent status sets: re-marking
    # the same records with the same status/child ids is semantically a
    # no-op, so retries converge instead of duplicating.
    """Bulk-mark records DISPATCHED/RUNNING with child workflow ids."""
    from investigation_agent_platform.domain.intake.batch import BatchRecordStatus

    t0 = tick()
    data = _coerced_data(MarkBatchRecordsInput, params, activity="mark_batch_records_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    story_log(
        logger,
        logging.INFO,
        "mark_batch_records_activity started",
        comp=COMP_ACTVTY,
        func="mark_batch_records_activity",
        stage="execute",
        step="mark",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        event="mark_batch_records_activity started",
        inputs={"job_id": str(data.get("job_id", ""))},
        job_id=str(data.get("job_id", "")),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id = _batch_uuid(str(data.get("job_id", "")))
        if not tenant_id or job_id is None:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        try:
            status = BatchRecordStatus(str(data.get("status", "") or ""))
        except ValueError:
            return GenericActivityResult(success=False, error="unknown record status")
        indices = data.get("record_indices") or []
        child_ids = data.get("child_workflow_ids") or {}
        ctx = _get_ctx()
        repo = getattr(ctx, "batch_repo", None)
        if repo is None:
            return GenericActivityResult(success=False, error="batch repository unavailable")
        marked = 0
        for raw_index in indices:
            record = await repo.get_record(tenant_id, job_id, int(raw_index))
            if record is None:
                continue
            child_id = child_ids.get(str(raw_index))
            await repo.save_record(
                tenant_id,
                job_id,
                record.model_copy(update={"status": status, "child_workflow_id": child_id}),
            )
            marked += 1
        story_log(
            logger,
            logging.INFO,
            "mark_batch_records_activity completed",
            comp=COMP_ACTVTY,
            func="mark_batch_records_activity",
            stage="execute",
            step="mark",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            event="mark_batch_records_activity completed",
            outputs={"outcome": "marked", "marked": marked},
            elapsed_ms=elapsed_ms(t0),
            marked=marked,
        )
        return GenericActivityResult(success=True, data={"marked": marked})
    except ApplicationFailure:
        raise
    except Exception as exc:
        story_log(
            logger,
            logging.ERROR,
            "mark_batch_records_activity failed",
            comp=COMP_ACTVTY,
            func="mark_batch_records_activity",
            stage="execute",
            step="mark",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            event="mark_batch_records_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
            job_id=str(data.get("job_id", "")),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def mark_batch_record_result_activity(
    params: MarkBatchRecordResultInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — terminal marking is convergent
    # (same status/error rewrite is a no-op); guarded to terminal states.
    """Mark one record terminal (DONE/FAILED/CANCELED) with an error."""
    from investigation_agent_platform.domain.intake.batch import BatchRecordStatus

    t0 = tick()
    data = _coerced_data(
        MarkBatchRecordResultInput, params, activity="mark_batch_record_result_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    story_log(
        logger,
        logging.INFO,
        "mark_batch_record_result_activity started",
        comp=COMP_ACTVTY,
        func="mark_batch_record_result_activity",
        stage="execute",
        step="mark_result",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        event="mark_batch_record_result_activity started",
        inputs={"job_id": str(data.get("job_id", ""))},
        job_id=str(data.get("job_id", "")),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id = _batch_uuid(str(data.get("job_id", "")))
        if not tenant_id or job_id is None:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        try:
            index = int(data.get("record_index", -1))
            status = BatchRecordStatus(str(data.get("status", "") or ""))
        except (TypeError, ValueError):
            return GenericActivityResult(success=False, error="record result invalid")
        if status not in (
            BatchRecordStatus.DONE,
            BatchRecordStatus.FAILED,
            BatchRecordStatus.CANCELED,
        ):
            return GenericActivityResult(success=False, error="status must be terminal")
        ctx = _get_ctx()
        repo = getattr(ctx, "batch_repo", None)
        if repo is None:
            return GenericActivityResult(success=False, error="batch repository unavailable")
        record = await repo.get_record(tenant_id, job_id, index)
        if record is None:
            return GenericActivityResult(success=False, error="batch record not found")
        error = data.get("error")
        await repo.save_record(
            tenant_id,
            job_id,
            record.model_copy(
                update={"status": status, "error": str(error)[:2048] if error else None}
            ),
        )
        story_log(
            logger,
            logging.INFO,
            "mark_batch_record_result_activity completed",
            comp=COMP_ACTVTY,
            func="mark_batch_record_result_activity",
            stage="execute",
            step="mark_result",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            event="mark_batch_record_result_activity completed",
            outputs={"outcome": "marked", "status": status.value},
            elapsed_ms=elapsed_ms(t0),
            status=status.value,
        )
        return GenericActivityResult(success=True, data={"status": status.value})
    except ApplicationFailure:
        raise
    except Exception as exc:
        story_log(
            logger,
            logging.ERROR,
            "mark_batch_record_result_activity failed",
            comp=COMP_ACTVTY,
            func="mark_batch_record_result_activity",
            stage="execute",
            step="mark_result",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            event="mark_batch_record_result_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
            job_id=str(data.get("job_id", "")),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def resolve_batch_stragglers_activity(
    params: ResolveBatchStragglersInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — classification from persisted
    # investigation status is convergent (re-running re-derives the same
    # AWAITING_INPUT/RUNNING labels); never invents terminal states.
    """Classify still-running records via the persisted investigation status.

    AWAITING_INPUT investigations keep running (parent completes partial);
    anything else still active stays RUNNING (also left running). Never
    invents terminal states for live executions.
    """
    from investigation_agent_platform.domain.intake.batch import BatchRecordStatus

    t0 = tick()
    data = _coerced_data(
        ResolveBatchStragglersInput, params, activity="resolve_batch_stragglers_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    story_log(
        logger,
        logging.INFO,
        "resolve_batch_stragglers_activity started",
        comp=COMP_ACTVTY,
        func="resolve_batch_stragglers_activity",
        stage="execute",
        step="resolve",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        event="resolve_batch_stragglers_activity started",
        inputs={"job_id": str(data.get("job_id", ""))},
        job_id=str(data.get("job_id", "")),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id = _batch_uuid(str(data.get("job_id", "")))
        if not tenant_id or job_id is None:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        indices = [int(index) for index in (data.get("record_indices") or [])]
        ctx = _get_ctx()
        repo = getattr(ctx, "batch_repo", None)
        investigation_repo = getattr(ctx, "investigation_repo", None)
        if repo is None or investigation_repo is None:
            return GenericActivityResult(success=False, error="repositories unavailable")
        awaiting = 0
        running = 0
        for index in indices:
            record = await repo.get_record(tenant_id, job_id, index)
            if record is None or record.investigation_id is None:
                continue
            investigation = await investigation_repo.get_by_id(tenant_id, record.investigation_id)
            status_value = str(getattr(getattr(investigation, "status", None), "value", ""))
            if status_value == "AWAITING_INPUT":
                await repo.save_record(
                    tenant_id,
                    job_id,
                    record.model_copy(update={"status": BatchRecordStatus.AWAITING_INPUT}),
                )
                awaiting += 1
            else:
                await repo.save_record(
                    tenant_id,
                    job_id,
                    record.model_copy(update={"status": BatchRecordStatus.RUNNING}),
                )
                running += 1
        story_log(
            logger,
            logging.INFO,
            "resolve_batch_stragglers_activity completed",
            comp=COMP_ACTVTY,
            func="resolve_batch_stragglers_activity",
            stage="execute",
            step="resolve",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            event="resolve_batch_stragglers_activity completed",
            outputs={"outcome": "resolved", "awaiting_input": awaiting, "running": running},
            elapsed_ms=elapsed_ms(t0),
            awaiting_input=awaiting,
            running=running,
        )
        return GenericActivityResult(
            success=True, data={"awaiting_input": awaiting, "running": running}
        )
    except ApplicationFailure:
        raise
    except Exception as exc:
        story_log(
            logger,
            logging.ERROR,
            "resolve_batch_stragglers_activity failed",
            comp=COMP_ACTVTY,
            func="resolve_batch_stragglers_activity",
            stage="execute",
            step="resolve",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            event="resolve_batch_stragglers_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
            job_id=str(data.get("job_id", "")),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def cancel_batch_children_activity(
    params: CancelBatchChildrenInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — Temporal workflow cancel is
    # idempotent per child handle and each outcome is recorded on the batch
    # row, so retries converge (already-canceled children are skipped by
    # their CANCELED status filter).
    """Cancel live child executions for RUNNING records (Part 11.9).

    Explicit propagation: the declared parent-close policy is ABANDON (so
    awaiting-input children survive parent completion), therefore parent
    cancellation fans out here — one client cancel per live child, each
    outcome recorded. Failures cancel auditably, never silently.
    """
    from investigation_agent_platform.domain.intake.batch import BatchRecordStatus

    t0 = tick()
    data = _coerced_data(
        CancelBatchChildrenInput, params, activity="cancel_batch_children_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    story_log(
        logger,
        logging.INFO,
        "cancel_batch_children_activity started",
        comp=COMP_ACTVTY,
        func="cancel_batch_children_activity",
        stage="execute",
        step="cancel",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        event="cancel_batch_children_activity started",
        inputs={"job_id": str(data.get("job_id", ""))},
        job_id=str(data.get("job_id", "")),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id = _batch_uuid(str(data.get("job_id", "")))
        if not tenant_id or job_id is None:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        ctx = _get_ctx()
        repo = getattr(ctx, "batch_repo", None)
        client = getattr(ctx, "temporal_client", None)
        if repo is None:
            return GenericActivityResult(success=False, error="batch repository unavailable")
        if client is None:
            return GenericActivityResult(success=False, error="temporal client unavailable")
        records, _ = await repo.list_records(tenant_id, job_id, limit=500, offset=0)
        canceled = 0
        failed: list[str] = []
        for record in records:
            if record.status not in (BatchRecordStatus.RUNNING, BatchRecordStatus.DISPATCHED):
                continue
            if not record.child_workflow_id:
                continue
            try:
                handle = client.get_workflow_handle(record.child_workflow_id)
                await handle.cancel()
                await repo.save_record(
                    tenant_id,
                    job_id,
                    record.model_copy(update={"status": BatchRecordStatus.CANCELED}),
                )
                canceled += 1
            except Exception as exc:
                story_log(
                    logger,
                    logging.ERROR,
                    "cancel_batch_children_activity child cancel failed",
                    comp=COMP_ACTVTY,
                    func="cancel_batch_children_activity",
                    stage="execute",
                    step="cancel_child",
                    outcome=OUTCOME_FAILED,
                    tenant_id=tenant_id,
                    event="cancel_batch_children_activity child cancel failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    exc_info=True,
                    record_index=record.record_index,
                )
                failed.append(f"{record.record_index}:{exc}")
        story_log(
            logger,
            logging.INFO,
            "cancel_batch_children_activity completed",
            comp=COMP_ACTVTY,
            func="cancel_batch_children_activity",
            stage="execute",
            step="cancel",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            event="cancel_batch_children_activity completed",
            outputs={"outcome": "canceled", "canceled": canceled, "failed_count": len(failed)},
            elapsed_ms=elapsed_ms(t0),
            canceled=canceled,
            failed_count=len(failed),
        )
        return GenericActivityResult(success=True, data={"canceled": canceled, "failed": failed})
    except ApplicationFailure:
        raise
    except Exception as exc:
        story_log(
            logger,
            logging.ERROR,
            "cancel_batch_children_activity failed",
            comp=COMP_ACTVTY,
            func="cancel_batch_children_activity",
            stage="execute",
            step="cancel",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            event="cancel_batch_children_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
            job_id=str(data.get("job_id", "")),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def update_batch_job_activity(
    params: UpdateBatchJobInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — optimistic-concurrency versioning
    # (expected_version) IS the mechanism: a retried stale update conflicts
    # loudly instead of duplicating.
    """Apply a status/stage/error update to the linked batch job."""
    from uuid import UUID as _UUID

    from investigation_agent_platform.domain.common.background_job import (
        BackgroundJobStage,
        BackgroundJobStatus,
    )

    t0 = tick()
    data = _coerced_data(UpdateBatchJobInput, params, activity="update_batch_job_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    story_log(
        logger,
        logging.INFO,
        "update_batch_job_activity started",
        comp=COMP_ACTVTY,
        func="update_batch_job_activity",
        stage="execute",
        step="update_job",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        event="update_batch_job_activity started",
        inputs={"job_id": str(data.get("job_id", ""))},
        job_id=str(data.get("job_id", "")),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id_str = str(data.get("job_id", ""))
        if not tenant_id or not job_id_str:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        try:
            job_id = _UUID(job_id_str)
        except ValueError:
            return GenericActivityResult(success=False, error="job_id invalid")
        ctx = _get_ctx()
        repo = getattr(ctx, "background_job_repo", None)
        if repo is None:
            return GenericActivityResult(success=False, error="background job repo unavailable")
        job = await repo.get_by_id(tenant_id, job_id)
        if job is None:
            return GenericActivityResult(success=False, error="background job not found")
        updates: dict[str, Any] = {"version": job.version + 1}
        status_raw = str(data.get("status", "") or "")
        if status_raw:
            try:
                updates["status"] = BackgroundJobStatus(status_raw)
            except ValueError:
                return GenericActivityResult(
                    success=False, error=f"unknown job status {status_raw!r}"
                )
        stage_raw = data.get("stage")
        if isinstance(stage_raw, dict) and stage_raw.get("name"):
            stages = list(job.stages)
            stages.append(
                BackgroundJobStage(
                    seq=len(stages),
                    name=str(stage_raw["name"])[:128],
                    message=str(stage_raw.get("message", ""))[:1024],
                )
            )
            updates["stages"] = stages[-50:]
        if data.get("error") is not None:
            updates["error"] = str(data["error"])[:2048]
        if isinstance(data.get("progress"), int):
            updates["progress"] = max(0, data["progress"])
        if isinstance(data.get("total"), int):
            updates["total"] = max(0, data["total"])
        updated = job.model_copy(update=updates)
        await repo.save(tenant_id, updated, expected_version=job.version)
        from investigation_agent_platform.infrastructure.messaging.job_fanout import (
            maybe_publish_job_progress,
        )

        await maybe_publish_job_progress(ctx, tenant_id, updated)
        story_log(
            logger,
            logging.INFO,
            "update_batch_job_activity completed",
            comp=COMP_ACTVTY,
            func="update_batch_job_activity",
            stage="execute",
            step="update_job",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            event="update_batch_job_activity completed",
            outputs={"outcome": "job_updated", "status": updated.status.value},
            elapsed_ms=elapsed_ms(t0),
            job_id=job_id_str,
            status=updated.status.value,
        )
        return GenericActivityResult(success=True, data={"status": updated.status.value})
    except ApplicationFailure:
        raise
    except Exception as exc:
        story_log(
            logger,
            logging.ERROR,
            "update_batch_job_activity failed",
            comp=COMP_ACTVTY,
            func="update_batch_job_activity",
            stage="execute",
            step="update_job",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            event="update_batch_job_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
            job_id=str(data.get("job_id", "")),
        )
        raise _application_failure_from_exc(exc) from exc


@dataclass
class SummarizeBatchInput:
    tenant_id: str
    job_id: str = ""


@activity.defn
async def summarize_batch_activity(
    params: SummarizeBatchInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — pure aggregation over record rows;
    # no writes.
    """Aggregate batch counts over record rows (Part 11.9)."""
    t0 = tick()
    data = _coerced_data(SummarizeBatchInput, params, activity="summarize_batch_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    story_log(
        logger,
        logging.INFO,
        "summarize_batch_activity started",
        comp=COMP_ACTVTY,
        func="summarize_batch_activity",
        stage="execute",
        step="summarize",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        event="summarize_batch_activity started",
        inputs={"job_id": str(data.get("job_id", ""))},
        job_id=str(data.get("job_id", "")),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id = _batch_uuid(str(data.get("job_id", "")))
        if not tenant_id or job_id is None:
            return GenericActivityResult(success=False, error="tenant_id and job_id required")
        ctx = _get_ctx()
        repo = getattr(ctx, "batch_repo", None)
        if repo is None:
            return GenericActivityResult(success=False, error="batch repository unavailable")
        records, _ = await repo.list_records(tenant_id, job_id, limit=500, offset=0)
        counts = {
            "total": len(records),
            "succeeded": 0,
            "failed": 0,
            "awaiting_input": 0,
            "canceled": 0,
        }
        for record in records:
            status = record.status.value
            if status == "DONE":
                counts["succeeded"] += 1
            elif status == "FAILED":
                counts["failed"] += 1
            elif status == "AWAITING_INPUT":
                counts["awaiting_input"] += 1
            elif status == "CANCELED":
                counts["canceled"] += 1
        story_log(
            logger,
            logging.INFO,
            "summarize_batch_activity completed",
            comp=COMP_ACTVTY,
            func="summarize_batch_activity",
            stage="execute",
            step="summarize",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            event="summarize_batch_activity completed",
            outputs={"outcome": "summarized", "total": counts["total"]},
            elapsed_ms=elapsed_ms(t0),
            total=counts["total"],
        )
        return GenericActivityResult(success=True, data=counts)
    except ApplicationFailure:
        raise
    except Exception as exc:
        story_log(
            logger,
            logging.ERROR,
            "summarize_batch_activity failed",
            comp=COMP_ACTVTY,
            func="summarize_batch_activity",
            stage="execute",
            step="summarize",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            event="summarize_batch_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
            job_id=str(data.get("job_id", "")),
        )
        raise _application_failure_from_exc(exc) from exc
