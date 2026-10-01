# src/investigation_agent_platform/domain/intake/batch.py
"""Bulk investigation intake domain (Part 11.9).

A batch is one `BackgroundJob` (kind `batch-intake`) plus per-record rows
in `batch_intake_records`. Storage decision (see tasks-todo 11.a): the job
row carries lifecycle/retention/quotas, while per-record state (external
key, parameters hash, child workflow id, status, attempt, error) needs
row-level reads for paginated children, idempotent resume, and partial
retries — 500 records cannot fit the job's 50-stage bound. No new lifecycle
semantics are invented: record statuses mirror the investigation/job
vocabulary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

#: Contract version for batch payloads.
BATCH_INTAKE_CONTRACT_VERSION = "1.0"

#: Maximum records per parent execution; larger imports partition into
#: multiple jobs rather than growing one parent history.
MAX_BATCH_RECORDS = 500

#: Maximum serialized batch request size (bytes); larger payloads are 413.
MAX_BATCH_PAYLOAD_BYTES = 1_000_000

#: Maximum concurrent *active* child executions per parent (permit held
#: from child start through completion, never start-only).
MAX_ACTIVE_CHILDREN = 10

#: Continue-As-New threshold: runs over this many records roll to a new
#: execution (with zero active children) carrying dispatch cursor + attempt.
CAN_RECORD_THRESHOLD = 250

#: Retention for batch records (days); enforced on purge.
BATCH_RETENTION_DAYS = 90


class BatchRecordStatus(StrEnum):
    """Per-record execution state (Part 11.9)."""

    PENDING = "PENDING"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    AWAITING_INPUT = "AWAITING_INPUT"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class BatchIntakeRecord(BaseModel):
    """One validated intake record (Part 11.9)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    external_key: str = Field(..., min_length=1, max_length=256)
    application_id: str = Field(..., min_length=1, max_length=128)
    problem_description: str = Field(..., min_length=1, max_length=8000)
    session_id: str | None = Field(default=None, max_length=256)
    priority: str = Field(default="NORMAL", min_length=1, max_length=16)
    parameters: dict[str, Any] = Field(default_factory=dict, max_length=50)


class BatchIntakeRequest(BaseModel):
    """Versioned batch intake request (Part 11.9)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    records: list[BatchIntakeRecord] = Field(
        default_factory=list, min_length=1, max_length=MAX_BATCH_RECORDS
    )
    contract_version: str = Field(default=BATCH_INTAKE_CONTRACT_VERSION, max_length=32)


class BatchRecordResult(BaseModel):
    """Read model for one record with its child mapping (Part 11.9)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_index: int = Field(..., ge=0)
    external_key: str = Field(..., min_length=1, max_length=256)
    application_id: str = Field(..., min_length=1, max_length=128)
    status: BatchRecordStatus = BatchRecordStatus.PENDING
    investigation_id: UUID | None = Field(default=None)
    child_workflow_id: str | None = Field(default=None, max_length=256)
    attempt: int = Field(default=0, ge=0)
    error: str | None = Field(default=None, max_length=2048)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BatchIntakeResult(BaseModel):
    """Versioned batch outcome (Part 11.9)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(..., min_length=1, max_length=128)
    total: int = Field(default=0, ge=0)
    succeeded: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    awaiting_input: int = Field(default=0, ge=0)
    canceled: int = Field(default=0, ge=0)
    contract_version: str = Field(default=BATCH_INTAKE_CONTRACT_VERSION, max_length=32)


def child_workflow_id(job_id: UUID | str, record_index: int, attempt: int = 0) -> str:
    """Deterministic opaque child ID (no external keys; attempt-suffixed).

    Accepts the UUID object or its hex/string form; string-only handling
    keeps this usable inside Temporal workflow sandboxes (no uuid import).
    """
    hex_id = job_id.hex if isinstance(job_id, UUID) else str(job_id).replace("-", "")
    base = f"wf-batch-{hex_id}-{record_index:04d}"
    return base if attempt <= 0 else f"{base}-r{attempt}"
