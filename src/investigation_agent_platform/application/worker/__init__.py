# src/investigation_agent_platform/application/worker/__init__.py
"""Temporal worker entrypoint configuring workflows and activities.

NOTE: production must use ``investigation_agent_platform.bootstrap.worker.run_temporal_worker``,
which sources ``task_queue``/``target_host``/``namespace`` from the single
``TemporalConfig`` source of truth (F-020). ``run_worker`` below is a legacy,
unused, standalone entrypoint retained for compatibility; its defaults are
kept in sync with ``TemporalConfig`` defaults to avoid queue-name drift.
"""

import logging

from temporalio.client import Client
from temporalio.worker import Worker

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
from investigation_agent_platform.infrastructure.configuration.config import TemporalConfig

logger = logging.getLogger(__name__)

_DEFAULT_TEMPORAL_CONFIG = TemporalConfig()


async def run_worker(
    task_queue: str = _DEFAULT_TEMPORAL_CONFIG.task_queue,
    address: str = _DEFAULT_TEMPORAL_CONFIG.target_host,
    namespace: str = _DEFAULT_TEMPORAL_CONFIG.namespace,
    max_concurrent_activities: int = 100,
) -> None:
    """Connects to Temporal server and starts the full investigation worker process."""
    logger.info(
        "Starting Temporal investigation worker", extra={"queue": task_queue, "address": address}
    )
    client = await Client.connect(address, namespace=namespace)
    worker = Worker(
        client=client,
        task_queue=task_queue,
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
        max_concurrent_activities=max_concurrent_activities,
    )
    await worker.run()
