# src/investigation_agent_platform/ports/scheduling/reconciler.py
"""Schedule reconciler port: declarative Temporal schedule management (Part 11.3E)."""

from typing import Protocol, runtime_checkable

from investigation_agent_platform.domain.common.schedules import ScheduleDescriptor


@runtime_checkable
class ScheduleReconcilerPort(Protocol):
    """Provisions and reconciles Temporal schedules from descriptors."""

    async def reconcile(self, desired: list[ScheduleDescriptor]) -> list[str]:
        """Create/update/pause/delete schedules to match desired state.

        Returns the schedule ids now active. Never raises for a single bad
        descriptor when others are valid — collects per-schedule errors into
        the returned summary instead.
        """
        ...

    async def active_schedule_ids(self) -> list[str]: ...
