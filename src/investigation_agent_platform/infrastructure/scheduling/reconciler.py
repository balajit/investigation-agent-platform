# src/investigation_agent_platform/infrastructure/scheduling/reconciler.py
"""Temporal schedule reconciler from declarative descriptors (Part 11.3E)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from investigation_agent_platform.domain.common.schedules import (
    ScheduleDescriptor,
    ScheduleOverlapPolicy,
)

logger = logging.getLogger(__name__)


def _to_temporal_overlap(policy: ScheduleOverlapPolicy) -> Any:
    from temporalio.client import ScheduleOverlapPolicy as TemporalOverlap

    return {
        ScheduleOverlapPolicy.SKIP: TemporalOverlap.SKIP,
        ScheduleOverlapPolicy.BUFFER_ONE: TemporalOverlap.BUFFER_ONE,
        ScheduleOverlapPolicy.ALLOW_ALL: TemporalOverlap.ALLOW_ALL,
        ScheduleOverlapPolicy.CANCEL_OTHER: TemporalOverlap.CANCEL_OTHER,
        ScheduleOverlapPolicy.TERMINATE_OTHER: TemporalOverlap.TERMINATE_OTHER,
    }[policy]


def _build_schedule(descriptor: ScheduleDescriptor, workflow: Any) -> Any:
    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleIntervalSpec,
        ScheduleSpec,
        ScheduleState,
    )

    return Schedule(
        action=ScheduleActionStartWorkflow(
            workflow,
            id=f"{descriptor.schedule_id}-wf",
            task_queue=descriptor.task_queue,
        ),
        spec=ScheduleSpec(
            intervals=[ScheduleIntervalSpec(every=timedelta(seconds=descriptor.interval_seconds))],
            jitter=timedelta(seconds=descriptor.jitter_seconds),
        ),
        state=ScheduleState(paused=descriptor.paused),
        policy=_to_temporal_overlap(descriptor.overlap_policy),
    )


class TemporalScheduleReconciler:
    """Reconciles Temporal schedules against desired descriptors (Part 11.3E).

    ``workflow_resolver`` maps ``descriptor.workflow_type`` to the workflow
    definition; unknown types are collected as errors, never raised
    mid-reconciliation.
    """

    def __init__(self, client: Any, workflow_resolver: Any | None = None) -> None:
        self._client = client
        self._workflow_resolver = workflow_resolver or {}

    async def reconcile(self, desired: list[ScheduleDescriptor]) -> list[str]:
        active: list[str] = []
        errors: list[str] = []
        for descriptor in desired:
            try:
                workflow = self._resolve_workflow(descriptor.workflow_type)
                schedule = _build_schedule(descriptor, workflow)
                try:
                    handle = self._client.get_schedule_handle(descriptor.schedule_id)
                    await handle.describe()
                    await handle.update(schedule)
                    if descriptor.paused:
                        await handle.pause()
                    else:
                        await handle.unpause()
                except Exception:
                    await self._client.create_schedule(descriptor.schedule_id, schedule)
                active.append(descriptor.schedule_id)
            except Exception as exc:
                errors.append(f"{descriptor.schedule_id}: {exc}")
                logger.warning(
                    "Schedule reconciliation failed",
                    extra={"schedule_id": descriptor.schedule_id, "error": str(exc)},
                )
        if errors:
            logger.warning(
                "Schedule reconciliation completed with errors",
                extra={"errors": errors},
            )
        return active

    async def active_schedule_ids(self) -> list[str]:
        ids: list[str] = []
        async for schedule in await self._client.list_schedules():
            schedule_id = getattr(schedule, "id", None) or getattr(schedule, "schedule_id", None)
            if schedule_id:
                ids.append(str(schedule_id))
        return ids

    def _resolve_workflow(self, workflow_type: str) -> Any:
        resolver = self._workflow_resolver
        if isinstance(resolver, dict):
            workflow = resolver.get(workflow_type)
        else:
            workflow = resolver(workflow_type)
        if workflow is None:
            raise ValueError(f"unknown workflow_type: {workflow_type!r}")
        return workflow
