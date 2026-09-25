# src/investigation_agent_platform/bootstrap/worker.py
"""Temporal self-hosted worker bootstrap (Phase 1.3)."""

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from investigation_agent_platform.api.dependencies import AppContext, set_app_context
from investigation_agent_platform.application.worker.activities import (
    checkpoint_activity,
    conclude_investigation_activity,
    create_investigation_activity,
    execute_action_activity,
    publish_event_activity,
    reason_activity,
    retrieve_evidence_activity,
    verify_root_cause_activity,
)
from investigation_agent_platform.application.worker.workflows import RunInvestigationWorkflow
from investigation_agent_platform.infrastructure.configuration.config import (
    ApplicationConfig,
    TemporalConfig,
)

logger = logging.getLogger(__name__)


def build_app_context(config: ApplicationConfig) -> AppContext:
    """Build AppContext selecting real vs in-memory deps by environment."""
    env = (config.environment or "").lower()
    if env == "production":
        logger.info("Building production AppContext for worker", extra={"environment": env})
        ctx = AppContext()
    else:
        logger.info("Building in-memory AppContext for worker", extra={"environment": env})
        ctx = AppContext()
    set_app_context(ctx)
    return ctx


async def run_temporal_worker(config: ApplicationConfig | TemporalConfig) -> None:
    """Connect to self-hosted Temporal (docker-compose temporal:7233) and run worker."""
    # Support both ApplicationConfig (preferred) and legacy TemporalConfig
    if isinstance(config, ApplicationConfig):
        temporal_cfg: TemporalConfig = config.temporal
        # Wire AppContext according to environment
        build_app_context(config)
    else:
        temporal_cfg = config
        # No environment info — default to in-memory
        ctx = AppContext()
        set_app_context(ctx)

    client = await Client.connect(temporal_cfg.target_host, namespace=temporal_cfg.namespace)
    worker = Worker(
        client,
        task_queue=temporal_cfg.task_queue,
        workflows=[RunInvestigationWorkflow],
        activities=[
            create_investigation_activity,
            retrieve_evidence_activity,
            reason_activity,
            execute_action_activity,
            verify_root_cause_activity,
            checkpoint_activity,
            conclude_investigation_activity,
            publish_event_activity,
        ],
    )
    logger.info(
        "Starting Temporal worker",
        extra={"task_queue": temporal_cfg.task_queue, "target": temporal_cfg.target_host},
    )
    await worker.run()


if __name__ == "__main__":
    from investigation_agent_platform.infrastructure.configuration.config import (
        load_application_config_from_env,
    )

    cfg = load_application_config_from_env()
    asyncio.run(run_temporal_worker(cfg))
