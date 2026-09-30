# src/investigation_agent_platform/domain/common/schedules.py
"""Temporal schedule descriptors and workflow-history policy (Part 11.3E).

Schedules are provisioned by an explicit reconciler (infrastructure), never
assumed to exist. History policy keeps long-running batch/indexing/clustering
workflows inside Temporal event limits.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ScheduleOverlapPolicy(StrEnum):
    SKIP = "SKIP"
    BUFFER_ONE = "BUFFER_ONE"
    ALLOW_ALL = "ALLOW_ALL"
    CANCEL_OTHER = "CANCEL_OTHER"
    TERMINATE_OTHER = "TERMINATE_OTHER"


class ScheduleDescriptor(BaseModel):
    """Declarative desired state for one Temporal schedule (Part 11.3E)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule_id: str = Field(..., min_length=1, max_length=256)
    workflow_type: str = Field(..., min_length=1, max_length=128)
    task_queue: str = Field(..., min_length=1, max_length=128)
    interval_seconds: int = Field(default=3600, ge=60, le=2_592_000)
    overlap_policy: ScheduleOverlapPolicy = ScheduleOverlapPolicy.SKIP
    jitter_seconds: int = Field(default=300, ge=0, le=3600)
    paused: bool = Field(default=False)
    catchup_backfill: bool = Field(default=False)
    tenant_id: str | None = Field(default=None, max_length=128)


class WorkflowHistoryPolicy(BaseModel):
    """Event-history guardrails for long-running workflows (Part 11.3E)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    warn_at_events: int = Field(default=8_000, ge=100, le=50_000)
    rollover_at_events: int = Field(default=10_000, ge=100, le=51_200)
    forbid_continue_as_new_with_active_children: bool = Field(default=True)
    forbid_continue_as_new_with_pending_handlers: bool = Field(default=True)

    def should_roll_over(self, history_length: int) -> bool:
        """True when the workflow should Continue-As-New at the next checkpoint."""
        return history_length >= self.rollover_at_events

    def should_warn(self, history_length: int) -> bool:
        return history_length >= self.warn_at_events
