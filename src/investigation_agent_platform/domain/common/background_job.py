# src/investigation_agent_platform/domain/common/background_job.py
"""Background job domain: generic async work tracking (Part 11.3B).

Every BackgroundJob SHOULD be backed by a Temporal workflow execution
(``workflow_id = f"job-{kind}-{job_id}"``). Temporal is authoritative for
execution; the ``background_jobs`` Postgres row is the queryable read model.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class BackgroundJobStatus(StrEnum):
    QUEUED = "QUEUED"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELED = "CANCELED"
    DONE = "DONE"
    FAILED = "FAILED"


TERMINAL_JOB_STATUSES = frozenset(
    {
        BackgroundJobStatus.CANCELED,
        BackgroundJobStatus.DONE,
        BackgroundJobStatus.FAILED,
    }
)


class BackgroundJobStage(BaseModel):
    """One typed, sequenced progress record within a job run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(..., ge=0)
    name: str = Field(..., min_length=1, max_length=128)
    message: str = Field(default="", max_length=1024)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BackgroundJob(BaseModel):
    """Durable background-job record (Part 11.3B)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(..., min_length=1, max_length=128)
    application_id: str | None = Field(default=None, max_length=128)
    kind: str = Field(..., min_length=1, max_length=64)
    contract_version: str = Field(default="1.0", min_length=1, max_length=32)
    status: BackgroundJobStatus = BackgroundJobStatus.QUEUED
    progress: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    created_by: str = Field(default="system", max_length=256)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)
    attempt: int = Field(default=0, ge=0)
    parent_job_id: UUID | None = Field(default=None)
    workflow_id: str | None = Field(default=None, max_length=256)
    run_id: str | None = Field(default=None, max_length=256)
    input_ref: str | None = Field(default=None, max_length=1024)
    result_ref: str | None = Field(default=None, max_length=1024)
    error: str | None = Field(default=None, max_length=2048)
    quota_class: str = Field(default="default", max_length=64)
    retention_class: str = Field(default="default", max_length=64)
    stages: list[BackgroundJobStage] = Field(default_factory=list, max_length=50)
    version: int = Field(default=1, ge=1)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_JOB_STATUSES


class BackgroundJobKindDescriptor(BaseModel):
    """Registered-kind contract (Part 11.3B): what a job kind needs + allows."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(..., min_length=1, max_length=64)
    contract_version: str = Field(default="1.0", min_length=1, max_length=32)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    result_schema: dict[str, Any] = Field(default_factory=dict)
    task_queue: str = Field(default="analytics-tasks", min_length=1, max_length=128)
    timeout_seconds: int = Field(default=3600, ge=60, le=86400)
    max_attempts: int = Field(default=3, ge=1, le=10)
    cancellable: bool = Field(default=True)
    retention_class: str = Field(default="default", max_length=64)
    quota_class: str = Field(default="default", max_length=64)
    required_capability: str | None = Field(default=None, max_length=128)


class JobProgressEvent(BaseModel):
    """Versioned progress event published to Kafka (Part 11.3C)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default="1.0", min_length=1, max_length=32)
    event_id: UUID = Field(default_factory=uuid4)
    job_id: UUID
    tenant_id: str = Field(..., min_length=1, max_length=128)
    kind: str = Field(..., min_length=1, max_length=64)
    status: BackgroundJobStatus
    progress: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    stage: str | None = Field(default=None, max_length=128)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
