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
    checkpoint_activity,
    collect_snapshots_activity,
    conclude_investigation_activity,
    create_investigation_activity,
    execute_action_activity,
    promote_fulfillment_evidence_activity,
    publish_event_activity,
    reason_activity,
    record_input_requirement_activity,
    retrieve_evidence_activity,
    run_clustering_activity,
    set_input_requirement_state_activity,
    sweep_knowledge_activity,
    update_clustering_job_activity,
    verify_root_cause_activity,
)
from investigation_agent_platform.application.worker.workflows import (
    FindingClusteringWorkflow,
    KnowledgeArtifactJanitorWorkflow,
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
]

# Part 11.7: analytics-queue workflows run on a separate Worker (see
# run_temporal_worker) so CPU-heavy batch analytics never starve the
# investigation queue.
ANALYTICS_WORKFLOWS: Sequence[type] = [
    FindingClusteringWorkflow,
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
]

# Part 11.7: analytics-queue activities (subset — only what analytics
# workflows invoke, so the analytics worker carries no investigation-only
# activities).
ANALYTICS_ACTIVITIES: Sequence[Callable[..., Any]] = [
    run_clustering_activity,
    update_clustering_job_activity,
]


def build_app_context(config: ApplicationConfig) -> AppContext:
    """Build AppContext for the worker using the shared production composition root.

    Delegates to ``investigation_agent_platform.bootstrap.build_app_context`` so
    API and worker processes share a single authoritative wiring path. In
    production this raises rather than silently falling back to in-memory
    dependencies (see PlatformConfigurationError in ``bootstrap/__init__.py``).
    """
    return _build_app_context(config)


async def run_temporal_worker(config: ApplicationConfig | TemporalConfig) -> None:
    """Connect to self-hosted Temporal (docker-compose temporal:7233) and run worker."""
    # Support both ApplicationConfig (preferred) and legacy TemporalConfig
    if isinstance(config, ApplicationConfig):
        temporal_cfg: TemporalConfig = config.temporal
        # Wire AppContext according to environment via the shared composition root
        build_app_context(config)
    else:
        temporal_cfg = config
        # Legacy TemporalConfig-only invocation carries no environment info.
        # Only permitted outside production; production must supply ApplicationConfig.
        logger.warning(
            "run_temporal_worker invoked with bare TemporalConfig; using in-memory "
            "AppContext. This path must never be used in production."
        )
        ctx = AppContext()
        set_app_context(ctx)

    client = await Client.connect(temporal_cfg.target_host, namespace=temporal_cfg.namespace)
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
    await asyncio.gather(worker.run(), analytics_worker.run())


if __name__ == "__main__":
    from investigation_agent_platform.infrastructure.configuration.config import (
        load_application_config_from_env,
    )

    cfg = load_application_config_from_env()
    asyncio.run(run_temporal_worker(cfg))
