# src/investigation_agent_platform/application/worker/__init__.py
"""Temporal worker entrypoint configuring workflows and activities."""

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

logger = logging.getLogger(__name__)


async def run_worker(
    task_queue: str = "investigations",
    address: str = "localhost:7233",
    namespace: str = "default",
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
