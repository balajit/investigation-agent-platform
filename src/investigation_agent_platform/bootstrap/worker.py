# src/investigation_agent_platform/bootstrap/worker.py
"""Temporal self-hosted worker bootstrap (Phase 1.3)."""

import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import Any

from temporalio.client import Client
from temporalio.worker import Worker

from investigation_agent_platform.api.dependencies import AppContext, set_app_context
from investigation_agent_platform.application.worker.activities import (
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
from investigation_agent_platform.application.worker.workflows import (
    AggregateReportWorkflow,
    BulkIntakeWorkflow,
    FindingClusteringWorkflow,
    KnowledgeArtifactJanitorWorkflow,
    ReferenceDocsIndexWorkflow,
    RunInvestigationWorkflow,
    TopologySnapshotRetentionWorkflow,
)
from investigation_agent_platform.bootstrap import build_app_context as _build_app_context
from investigation_agent_platform.infrastructure.configuration.config import (
    ApplicationConfig,
    TemporalConfig,
)

logger = logging.getLogger(__name__)


# Part 11.0: canonical workflow/activity registration. Every supported worker
# launcher must use these lists so queue behavior cannot differ by entrypoint.
# Annotated as generic sequences: Temporal's Worker accepts any callables and
# activity/defn decorators, not one static signature.
WORKFLOWS: Sequence[type] = [
    RunInvestigationWorkflow,
    TopologySnapshotRetentionWorkflow,
    KnowledgeArtifactJanitorWorkflow,
    BulkIntakeWorkflow,
]

# Part 11.7: analytics-queue workflows run on a separate Worker (see
# run_temporal_worker) so CPU-heavy batch analytics never starve the
# investigation queue.
ANALYTICS_WORKFLOWS: Sequence[type] = [
    FindingClusteringWorkflow,
    AggregateReportWorkflow,
]

ACTIVITIES: Sequence[Callable[..., Any]] = [
    create_investigation_activity,
    retrieve_evidence_activity,
    reason_activity,
    execute_action_activity,
    verify_root_cause_activity,
    checkpoint_activity,
    conclude_investigation_activity,
    publish_event_activity,
    collect_snapshots_activity,
    sweep_knowledge_activity,
    record_input_requirement_activity,
    set_input_requirement_state_activity,
    promote_fulfillment_evidence_activity,
    load_batch_records_activity,
    mark_batch_records_activity,
    mark_batch_record_result_activity,
    resolve_batch_stragglers_activity,
    cancel_batch_children_activity,
    summarize_batch_activity,
    retrieve_knowledge_activity,
    capture_knowledge_activity,
    transition_investigation_activity,
    update_batch_job_activity,
]

# Part 11.7: analytics-queue activities (subset — only what analytics
# workflows invoke, so the analytics worker carries no investigation-only
# activities).
ANALYTICS_ACTIVITIES: Sequence[Callable[..., Any]] = [
    run_clustering_activity,
    update_clustering_job_activity,
    run_report_activity,
    update_report_job_activity,
]

# Part 11.6: indexing-queue workflows run on a third Worker so expensive
# source indexing never starves investigation or analytics queues.
INDEXING_WORKFLOWS: Sequence[type] = [
    ReferenceDocsIndexWorkflow,
]
INDEXING_ACTIVITIES: Sequence[Callable[..., Any]] = [
    run_reindex_activity,
    update_reindex_job_activity,
]


def build_app_context(config: ApplicationConfig) -> AppContext:
    """Build AppContext for the worker using the shared production composition root.

    Delegates to ``investigation_agent_platform.bootstrap.build_app_context`` so
    API and worker processes share a single authoritative wiring path. In
    production this raises rather than silently falling back to in-memory
    dependencies (see PlatformConfigurationError in ``bootstrap/__init__.py``).
    """
    return _build_app_context(config)


async def _reconcile_schedules(client: Any, temporal_cfg: Any) -> None:
    """Reconcile desired Temporal schedules (Part 11.3E, verified in 11.12)."""
    import os

    tenants = [
        t.strip() for t in os.environ.get("IAP_SCHEDULING_TENANTS", "").split(",") if t.strip()
    ]
    if not tenants:
        logger.info("Schedule reconciliation skipped (IAP_SCHEDULING_TENANTS empty)")
        return
    try:
        from investigation_agent_platform.application.finding.clustering_service import (
            finding_clustering_schedules,
        )
        from investigation_agent_platform.application.worker.workflows import (
            FindingClusteringWorkflow,
        )
        from investigation_agent_platform.infrastructure.scheduling.reconciler import (
            TemporalScheduleReconciler,
        )

        descriptors = finding_clustering_schedules(
            tenants,
            task_queue=getattr(temporal_cfg, "analytics_task_queue", "analytics-tasks"),
        )
        reconciler = TemporalScheduleReconciler(
            client, workflow_resolver={"FindingClusteringWorkflow": FindingClusteringWorkflow}
        )
        active = await reconciler.reconcile(descriptors)
        logger.info(
            "Schedule reconciliation complete", extra={"active": active, "tenants": tenants}
        )
    except Exception as exc:
        logger.error("Schedule reconciliation failed; continuing", extra={"error": str(exc)})


async def run_temporal_worker(config: ApplicationConfig | TemporalConfig) -> None:
    """Connect to self-hosted Temporal (docker-compose temporal:7233) and run worker."""
    # Support both ApplicationConfig (preferred) and legacy TemporalConfig
    if isinstance(config, ApplicationConfig):
        temporal_cfg: TemporalConfig = config.temporal
        # Wire AppContext according to environment via the shared composition root
        worker_ctx = build_app_context(config)
    else:
        temporal_cfg = config
        # Legacy TemporalConfig-only invocation carries no environment info.
        # Only permitted outside production; production must supply ApplicationConfig.
        logger.warning(
            "run_temporal_worker invoked with bare TemporalConfig; using in-memory "
            "AppContext. This path must never be used in production."
        )
        worker_ctx = AppContext()
        set_app_context(worker_ctx)

    client = await Client.connect(temporal_cfg.target_host, namespace=temporal_cfg.namespace)
    # Part 11.12: dev worker reads/writes the same Postgres-backed stores as
    # the API (buffered proxies); otherwise jobs/records created by the API
    # are invisible here and vice versa. Production already uses SQL.
    if isinstance(config, ApplicationConfig):
        try:
            from investigation_agent_platform.bootstrap.buffered_dev import (
                ensure_buffered_dev_context,
            )

            await ensure_buffered_dev_context(worker_ctx, config)
        except Exception as exc:
            logger.warning("Buffered worker context unavailable", extra={"error": str(exc)})
    # Part 11.12: the worker publishes job progress events; start its broker
    # handle best-effort (dev has none; Kafka outages degrade to DB state).
    # Bounded wait: broker.start() can hang on unresolvable listeners.
    try:
        _wbroker = getattr(worker_ctx, "broker", None)
        if _wbroker is not None and hasattr(_wbroker, "start"):
            await asyncio.wait_for(_wbroker.start(), timeout=20)
    except Exception as exc:
        logger.warning(
            "Worker broker unavailable; progress publish degraded", extra={"error": str(exc)}
        )
    try:
        _wctx_client = client
        _wctx = worker_ctx
        if getattr(_wctx, "temporal_client", None) is None:
            _wctx.temporal_client = _wctx_client  # type: ignore[attr-defined]
    except Exception:
        pass
    # Part 11.3E/11.12: reconcile periodic schedules (stable ids, overlap,
    # jitter, pause/backfill/update/delete live in the reconciler). Tenant
    # enumeration comes from IAP_SCHEDULING_TENANTS (empty = skip: on-demand
    # runs stay available). Non-fatal by design — a scheduler outage must
    # never take down workflow execution.
    await _reconcile_schedules(client, temporal_cfg)
    worker = Worker(
        client,
        task_queue=temporal_cfg.task_queue,
        workflows=WORKFLOWS,
        activities=ACTIVITIES,
        # Part 11.3E: explicit concurrency bounds from TemporalConfig instead
        # of SDK/library defaults, so investigation/indexing/analytics load
        # cannot silently saturate the worker.
        max_concurrent_activities=temporal_cfg.max_concurrent_activities,
        max_concurrent_workflow_tasks=temporal_cfg.max_concurrent_workflows,
    )
    logger.info(
        "Starting Temporal worker",
        extra={"task_queue": temporal_cfg.task_queue, "target": temporal_cfg.target_host},
    )
    # Part 11.7: analytics queue runs on a second Worker in the same process
    # so batch analytics (clustering, reports) never compete with the
    # investigation queue for workflow/activity slots.
    analytics_worker = Worker(
        client,
        task_queue=temporal_cfg.analytics_task_queue,
        workflows=ANALYTICS_WORKFLOWS,
        activities=ANALYTICS_ACTIVITIES,
        max_concurrent_activities=temporal_cfg.max_concurrent_activities,
        max_concurrent_workflow_tasks=temporal_cfg.max_concurrent_workflows,
    )
    logger.info(
        "Starting Temporal analytics worker",
        extra={
            "task_queue": temporal_cfg.analytics_task_queue,
            "target": temporal_cfg.target_host,
        },
    )
    # Part 11.6: indexing queue runs on a third Worker for the same
    # isolation reason (source indexing is I/O- and embedding-heavy).
    indexing_worker = Worker(
        client,
        task_queue=temporal_cfg.indexing_task_queue,
        workflows=INDEXING_WORKFLOWS,
        activities=INDEXING_ACTIVITIES,
        max_concurrent_activities=temporal_cfg.max_concurrent_activities,
        max_concurrent_workflow_tasks=temporal_cfg.max_concurrent_workflows,
    )
    logger.info(
        "Starting Temporal indexing worker",
        extra={
            "task_queue": temporal_cfg.indexing_task_queue,
            "target": temporal_cfg.target_host,
        },
    )
    await asyncio.gather(worker.run(), analytics_worker.run(), indexing_worker.run())


if __name__ == "__main__":
    from investigation_agent_platform.infrastructure.configuration.config import (
        load_application_config_from_env,
    )

    cfg = load_application_config_from_env()
    asyncio.run(run_temporal_worker(cfg))
