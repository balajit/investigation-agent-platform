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

from investigation_agent_platform.infrastructure.configuration.config import TemporalConfig

logger = logging.getLogger(__name__)

_DEFAULT_TEMPORAL_CONFIG = TemporalConfig()


async def run_worker(
    task_queue: str = _DEFAULT_TEMPORAL_CONFIG.task_queue,
    address: str = _DEFAULT_TEMPORAL_CONFIG.target_host,
    namespace: str = _DEFAULT_TEMPORAL_CONFIG.namespace,
    max_concurrent_activities: int = 100,
) -> None:
    """Connects to Temporal server and starts the full investigation worker process.

    .. deprecated::
        Use ``investigation_agent_platform.bootstrap.worker.run_temporal_worker``
        instead, which sources all settings from ``TemporalConfig`` and shares
        the canonical workflow/activity registration. This legacy entrypoint
        now delegates its registration lists to that module so queue behavior
        cannot drift between launchers.
    """
    import warnings

    warnings.warn(
        "application.worker.run_worker is deprecated; use bootstrap.worker.run_temporal_worker",
        DeprecationWarning,
        stacklevel=2,
    )
    # Canonical registration lives in bootstrap.worker (Part 11.0); importing
    # here (not at module top) avoids a bootstrap<->worker import cycle.
    from investigation_agent_platform.bootstrap.worker import ACTIVITIES, WORKFLOWS

    logger.info(
        "Starting Temporal investigation worker", extra={"queue": task_queue, "address": address}
    )
    client = await Client.connect(address, namespace=namespace)
    worker = Worker(
        client=client,
        task_queue=task_queue,
        workflows=WORKFLOWS,
        activities=ACTIVITIES,
        max_concurrent_activities=max_concurrent_activities,
    )
    await worker.run()
