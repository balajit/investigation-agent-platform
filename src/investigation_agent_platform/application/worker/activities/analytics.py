# src/investigation_agent_platform/application/worker/activities/analytics.py
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
    OUTCOME_SKIPPED,
    OUTCOME_STARTED,
    _abandon_reservation,
    _application_failure_from_exc,
    _coerced_data,
    _complete_reservation,
    _reserve_or_replay,
    elapsed_ms,
    story_log,
    tick,
)
from .fulfillment import RunClusteringInput, UpdateClusteringJobInput
from .inputs import GenericActivityResult, _get_ctx


@activity.defn
async def run_clustering_activity(
    params: RunClusteringInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — a bounded incremental pass that is
    # re-runnable by design (re-running assigns nothing new); quota-exhaustion
    # returns a failure result rather than raising.
    """Run one bounded incremental clustering pass (Part 11.7)."""
    from investigation_agent_platform.application.finding.clustering_service import (
        FindingClusterService,
        QuotaExceededError,
    )
    from investigation_agent_platform.domain.common.extension import CapabilityScope
    from investigation_agent_platform.domain.common.quotas import QuotaPolicy

    t0 = tick()
    data = _coerced_data(RunClusteringInput, params, activity="run_clustering_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    story_log(
        logger,
        logging.INFO,
        "run_clustering_activity started",
        comp=COMP_ACTVTY,
        func="run_clustering_activity",
        stage="execute",
        step="cluster",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        event="run_clustering_activity started",
        inputs={"job_id": str(data.get("job_id", ""))},
        job_id=str(data.get("job_id", "")),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        if not tenant_id:
            return GenericActivityResult(success=False, error="tenant_id required")
        try:
            limit = max(1, min(int(data.get("limit", 500)), 2000))
            batch_size = max(1, min(int(data.get("batch_size", 25)), 100))
        except (TypeError, ValueError):
            return GenericActivityResult(success=False, error="limit/batch_size invalid")
        ctx = _get_ctx()

        gateway: Any = None
        try:
            from investigation_agent_platform.infrastructure.configuration.config import (
                load_application_config_from_env,
            )
            from investigation_agent_platform.infrastructure.reasoning.factory import (
                create_llm_gateway,
            )

            gateway = create_llm_gateway(load_application_config_from_env().llm)
        except Exception as exc:
            # Lexical-only degraded mode: taxonomy preserved, new findings
            # explicitly UNASSIGNED with provenance. Loud, never silent.
            story_log(
                logger,
                logging.ERROR,
                "Clustering without LLM gateway; lexical-only mode",
                comp=COMP_ACTVTY,
                func="run_clustering_activity",
                stage="execute",
                step="init_gateway",
                outcome=OUTCOME_SKIPPED,
                tenant_id=tenant_id,
                event="run_clustering_activity lexical-only",
                error=str(exc),
                error_type=type(exc).__name__,
                exc_info=True,
            )

        quota_policy: QuotaPolicy | None = None
        quota_guarded = False
        try:
            from investigation_agent_platform.infrastructure.configuration.config import (
                load_application_config_from_env as _load_cfg,
            )

            quota_policy = QuotaPolicy.model_validate(_load_cfg().quotas.model_dump(mode="json"))
            quota_guarded = True
        except Exception as exc:
            story_log(
                logger,
                logging.ERROR,
                "Clustering without quota guard; config unavailable",
                comp=COMP_ACTVTY,
                func="run_clustering_activity",
                stage="execute",
                step="init_quota",
                outcome=OUTCOME_SKIPPED,
                tenant_id=tenant_id,
                event="run_clustering_activity quota unguarded",
                error=str(exc),
                error_type=type(exc).__name__,
                exc_info=True,
            )

        service = FindingClusterService(
            finding_repo=getattr(ctx, "finding_repo", None),
            cluster_repo=getattr(ctx, "finding_cluster_repo", None),
            llm_gateway=gateway,
            embedder=None,
            batch_size=batch_size,
        )
        if service._finding_repo is None or service._cluster_repo is None:
            return GenericActivityResult(
                success=False, error="finding or cluster repository unavailable"
            )
        scope = CapabilityScope(tenant_id=tenant_id)
        try:
            result = await service.run_incremental(
                tenant_id,
                limit=limit,
                quota_enforcer=getattr(ctx, "quota_enforcer", None) if quota_guarded else None,
                quota_policy=quota_policy,
                scope=scope if quota_guarded else None,
            )
        except QuotaExceededError as exc:
            return GenericActivityResult(success=False, error=str(exc))
        result_data = result.model_dump(mode="json")
        result_data["quota_guarded"] = quota_guarded
        result_data["lexical_only"] = gateway is None
        story_log(
            logger,
            logging.INFO,
            "run_clustering_activity completed",
            comp=COMP_ACTVTY,
            func="run_clustering_activity",
            stage="execute",
            step="cluster",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            event="run_clustering_activity completed",
            outputs={"outcome": "clustered", "quota_guarded": quota_guarded},
            elapsed_ms=elapsed_ms(t0),
            quota_guarded=quota_guarded,
        )
        return GenericActivityResult(success=True, data=result_data)
    except ApplicationFailure:
        raise
    except Exception as exc:
        story_log(
            logger,
            logging.ERROR,
            "run_clustering_activity failed",
            comp=COMP_ACTVTY,
            func="run_clustering_activity",
            stage="execute",
            step="cluster",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            event="run_clustering_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def update_clustering_job_activity(
    params: UpdateClusteringJobInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — optimistic-concurrency versioning
    # (expected_version) IS the mechanism: a retried stale update conflicts
    # loudly instead of duplicating.
    """Apply a status/stage/error update to the linked BackgroundJob row."""
    from uuid import UUID as _UUID

    from investigation_agent_platform.domain.common.background_job import (
        BackgroundJobStage,
        BackgroundJobStatus,
    )

    t0 = tick()
    data = _coerced_data(
        UpdateClusteringJobInput, params, activity="update_clustering_job_activity"
    )
    tenant_id_probe = str(data.get("tenant_id", ""))
    story_log(
        logger,
        logging.INFO,
        "update_clustering_job_activity started",
        comp=COMP_ACTVTY,
        func="update_clustering_job_activity",
        stage="execute",
        step="update_job",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        event="update_clustering_job_activity started",
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
            "update_clustering_job_activity completed",
            comp=COMP_ACTVTY,
            func="update_clustering_job_activity",
            stage="execute",
            step="update_job",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            event="update_clustering_job_activity completed",
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
            "update_clustering_job_activity failed",
            comp=COMP_ACTVTY,
            func="update_clustering_job_activity",
            stage="execute",
            step="update_job",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            event="update_clustering_job_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
            job_id=str(data.get("job_id", "")),
        )
        raise _application_failure_from_exc(exc) from exc


@dataclass
class RunReportInput:
    tenant_id: str
    job_id: str = ""
    legal_hold: bool = False


@dataclass
class UpdateReportJobInput:
    tenant_id: str
    job_id: str
    status: str = ""
    stage: dict[str, Any] | None = None
    error: str | None = None
    progress: int | None = None
    total: int | None = None


@activity.defn
async def run_report_activity(
    params: RunReportInput,
) -> GenericActivityResult:
    """Build, render, and store one aggregate report (Part 11.8)."""
    from uuid import UUID as _UUID

    from investigation_agent_platform.application.reporting.aggregate_service import (
        InvestigationAggregateReportService,
    )
    from investigation_agent_platform.application.reporting.artifacts import (
        store_report_run,
    )
    from investigation_agent_platform.infrastructure.reporting.html_renderer import (
        HtmlAggregateReportRenderer,
    )

    t0 = tick()
    data = _coerced_data(RunReportInput, params, activity="run_report_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    story_log(
        logger,
        logging.INFO,
        "run_report_activity started",
        comp=COMP_ACTVTY,
        func="run_report_activity",
        stage="publish",
        step="report",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        event="run_report_activity started",
        inputs={"job_id": str(data.get("job_id", ""))},
        job_id=str(data.get("job_id", "")),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        job_id_str = str(data.get("job_id", ""))
        if not tenant_id:
            return GenericActivityResult(success=False, error="tenant_id required")
        try:
            job_id = _UUID(job_id_str) if job_id_str else None
        except ValueError:
            return GenericActivityResult(success=False, error="job_id invalid")
        legal_hold = bool(data.get("legal_hold", False))
        ctx = _get_ctx()
        cluster_repo = getattr(ctx, "finding_cluster_repo", None)
        store = getattr(ctx, "artifact_store", None)
        if cluster_repo is None or store is None:
            return GenericActivityResult(
                success=False, error="cluster repository or artifact store unavailable"
            )
        service = InvestigationAggregateReportService(
            cluster_repo=cluster_repo,
            finding_repo=getattr(ctx, "finding_repo", None),
        )
        report = await service.build(tenant_id, job_id=job_id)
        html = HtmlAggregateReportRenderer().render(report)
        manifest_json = report.model_dump_json(indent=2).encode("utf-8")
        if job_id is None:
            return GenericActivityResult(
                success=False, error="job_id required for artifact history"
            )
        # TO_DO-011: idempotency-wired — (job_id) is deterministic per report
        # run, so a retried run replays stored artifact keys instead of
        # writing duplicate history artifacts.
        _idem_active_key: str | None = None
        _idem_operation = "activity:run_report"
        _idem_key = f"run_report:{job_id}"
        _replayed, _idem_active_key = await _reserve_or_replay(
            ctx=ctx,
            tenant_id=tenant_id,
            key=_idem_key,
            request_payload={
                "tenant_id": tenant_id,
                "job_id": job_id_str,
                "legal_hold": legal_hold,
            },
            operation=_idem_operation,
            activity="run_report_activity",
        )
        if _replayed is not None:
            return GenericActivityResult(
                success=bool(_replayed.get("success", True)),
                data=dict(_replayed.get("data") or {}),
                error=_replayed.get("error"),
            )
        refs = await store_report_run(
            store, tenant_id, job_id, html, manifest_json, legal_hold=legal_hold
        )
        await _link_report_result(ctx, tenant_id, job_id, refs["history_html"].key)
        if _idem_active_key is not None:
            await _complete_reservation(
                ctx=ctx,
                tenant_id=tenant_id,
                key=_idem_active_key,
                operation=_idem_operation,
                response={
                    "success": True,
                    "data": {
                        "taxonomy_revision": report.taxonomy_revision,
                        "assignment_count": report.assignment_count,
                        "artifact_keys": [refs["history_html"].key, refs["history_json"].key],
                    },
                },
                activity="run_report_activity",
            )
        story_log(
            logger,
            logging.INFO,
            "run_report_activity completed",
            comp=COMP_ACTVTY,
            func="run_report_activity",
            stage="publish",
            step="report",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            event="run_report_activity completed",
            outputs={
                "outcome": "reported",
                "assignment_count": report.assignment_count,
            },
            elapsed_ms=elapsed_ms(t0),
            job_id=job_id_str,
            assignment_count=report.assignment_count,
        )
        return GenericActivityResult(
            success=True,
            data={
                "taxonomy_revision": report.taxonomy_revision,
                "assignment_count": report.assignment_count,
                "artifact_keys": [refs["history_html"].key, refs["history_json"].key],
            },
        )
    except ApplicationFailure:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="run_report_activity",
        )
        raise
    except Exception as exc:
        await _abandon_reservation(
            ctx=locals().get("ctx"),
            tenant_id=str(locals().get("tenant_id", "") or ""),
            key=locals().get("_idem_active_key"),
            operation=str(locals().get("_idem_operation", "") or ""),
            activity="run_report_activity",
        )
        story_log(
            logger,
            logging.ERROR,
            "run_report_activity failed",
            comp=COMP_ACTVTY,
            func="run_report_activity",
            stage="publish",
            step="report",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            event="run_report_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
            job_id=str(data.get("job_id", "")),
        )
        raise _application_failure_from_exc(exc) from exc


async def _link_report_result(ctx: Any, tenant_id: str, job_id: Any, result_ref: str) -> None:
    """Best-effort result_ref linkage; OCC races must not fail the run."""
    try:
        repo = getattr(ctx, "background_job_repo", None)
        if repo is None:
            return
        job = await repo.get_by_id(tenant_id, job_id)
        if job is None:
            return
        updated = job.model_copy(update={"result_ref": result_ref, "version": job.version + 1})
        await repo.save(tenant_id, updated, expected_version=job.version)
    except Exception as exc:
        story_log(
            logger,
            logging.ERROR,
            "Report result_ref linkage skipped",
            comp=COMP_ACTVTY,
            func="_link_report_result",
            stage="publish",
            step="link",
            outcome=OUTCOME_SKIPPED,
            tenant_id=tenant_id,
            event="report result_ref linkage skipped",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )


@activity.defn
async def update_report_job_activity(
    params: UpdateReportJobInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — optimistic-concurrency versioning
    # (expected_version) IS the mechanism: a retried stale update conflicts
    # loudly instead of duplicating.
    """Apply a status/stage/error update to the linked aggregate-report job."""
    from uuid import UUID as _UUID

    from investigation_agent_platform.domain.common.background_job import (
        BackgroundJobStage,
        BackgroundJobStatus,
    )

    t0 = tick()
    data = _coerced_data(UpdateReportJobInput, params, activity="update_report_job_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    story_log(
        logger,
        logging.INFO,
        "update_report_job_activity started",
        comp=COMP_ACTVTY,
        func="update_report_job_activity",
        stage="execute",
        step="update_job",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        event="update_report_job_activity started",
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
            "update_report_job_activity completed",
            comp=COMP_ACTVTY,
            func="update_report_job_activity",
            stage="execute",
            step="update_job",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            event="update_report_job_activity completed",
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
            "update_report_job_activity failed",
            comp=COMP_ACTVTY,
            func="update_report_job_activity",
            stage="execute",
            step="update_job",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            event="update_report_job_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
            job_id=str(data.get("job_id", "")),
        )
        raise _application_failure_from_exc(exc) from exc


@dataclass
class RunReindexInput:
    tenant_id: str
    job_id: str = ""
    source_id: str = ""
    application_id: str = ""


@dataclass
class UpdateReindexJobInput:
    tenant_id: str
    job_id: str
    status: str = ""
    stage: dict[str, Any] | None = None
    error: str | None = None
    progress: int | None = None
    total: int | None = None


@activity.defn
async def run_reindex_activity(
    params: RunReindexInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — index_source writes a new generation
    # per call and is re-runnable (a retry supersedes rather than duplicates);
    # generation-scoped dedup lives in the reference service.
    """Index one reference source into a new generation (Part 11.6)."""
    from investigation_agent_platform.application.reference.reference_service import (
        ReferenceDocumentService,
    )

    t0 = tick()
    data = _coerced_data(RunReindexInput, params, activity="run_reindex_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    story_log(
        logger,
        logging.INFO,
        "run_reindex_activity started",
        comp=COMP_ACTVTY,
        func="run_reindex_activity",
        stage="execute",
        step="reindex",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        event="run_reindex_activity started",
        inputs={"source_id": str(data.get("source_id", ""))},
        source_id=str(data.get("source_id", "")),
    )
    try:
        tenant_id = str(data.get("tenant_id", ""))
        source_id = str(data.get("source_id", ""))
        if not tenant_id or not source_id:
            return GenericActivityResult(success=False, error="tenant_id and source_id required")
        ctx = _get_ctx()
        chunk_repo = getattr(ctx, "reference_repo", None)
        if chunk_repo is None:
            return GenericActivityResult(success=False, error="reference repository unavailable")
        service = ReferenceDocumentService(
            chunk_repo=chunk_repo,
            profile_repo=getattr(ctx, "profile_repo", None),
            embedder=None,
        )
        try:
            summary = await service.index_source(
                tenant_id, source_id, data.get("application_id") or None
            )
        except (RuntimeError, ValueError) as exc:
            return GenericActivityResult(success=False, error=str(exc))
        story_log(
            logger,
            logging.INFO,
            "run_reindex_activity completed",
            comp=COMP_ACTVTY,
            func="run_reindex_activity",
            stage="execute",
            step="reindex",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            event="run_reindex_activity completed",
            outputs={"outcome": "indexed", "source_id": source_id},
            elapsed_ms=elapsed_ms(t0),
            source_id=source_id,
        )
        return GenericActivityResult(success=True, data=summary.model_dump(mode="json"))
    except ApplicationFailure:
        raise
    except Exception as exc:
        story_log(
            logger,
            logging.ERROR,
            "run_reindex_activity failed",
            comp=COMP_ACTVTY,
            func="run_reindex_activity",
            stage="execute",
            step="reindex",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            event="run_reindex_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
            source_id=str(data.get("source_id", "")),
        )
        raise _application_failure_from_exc(exc) from exc


@activity.defn
async def update_reindex_job_activity(
    params: UpdateReindexJobInput,
) -> GenericActivityResult:
    # TO_DO-011: not idempotency-wired — optimistic-concurrency versioning
    # (expected_version) IS the mechanism: a retried stale update conflicts
    # loudly instead of duplicating.
    """Apply a status/stage/error update to the linked reindex job."""
    from uuid import UUID as _UUID

    from investigation_agent_platform.domain.common.background_job import (
        BackgroundJobStage,
        BackgroundJobStatus,
    )

    t0 = tick()
    data = _coerced_data(UpdateReindexJobInput, params, activity="update_reindex_job_activity")
    tenant_id_probe = str(data.get("tenant_id", ""))
    story_log(
        logger,
        logging.INFO,
        "update_reindex_job_activity started",
        comp=COMP_ACTVTY,
        func="update_reindex_job_activity",
        stage="execute",
        step="update_job",
        outcome=OUTCOME_STARTED,
        tenant_id=tenant_id_probe,
        event="update_reindex_job_activity started",
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
            "update_reindex_job_activity completed",
            comp=COMP_ACTVTY,
            func="update_reindex_job_activity",
            stage="execute",
            step="update_job",
            outcome=OUTCOME_COMPLETED,
            tenant_id=tenant_id,
            event="update_reindex_job_activity completed",
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
            "update_reindex_job_activity failed",
            comp=COMP_ACTVTY,
            func="update_reindex_job_activity",
            stage="execute",
            step="update_job",
            outcome=OUTCOME_FAILED,
            tenant_id=tenant_id_probe,
            event="update_reindex_job_activity failed",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
            job_id=str(data.get("job_id", "")),
        )
        raise _application_failure_from_exc(exc) from exc
