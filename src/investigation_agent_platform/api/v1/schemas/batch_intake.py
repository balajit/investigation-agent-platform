# src/investigation_agent_platform/api/v1/schemas/batch_intake.py
"""Response models for the batch-intake router."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.common.background_job import BackgroundJob
from investigation_agent_platform.domain.intake.batch import (
    MAX_BATCH_RECORDS,
    BatchIntakeRecord,
    BatchRecordResult,
)
from investigation_agent_platform.domain.intake.batch import (
    BatchIntakeResult as BatchIntakeSummary,
)


class CsvPayload(BaseModel):
    """CSV half of the batch-intake request body (requires explicit field_mapping)."""

    text: str = Field(..., min_length=1, max_length=1_000_000, description="Raw CSV document text")
    field_mapping: dict[str, Any] = Field(
        ..., description="CSV column to BatchIntakeRecord field mapping"
    )


class BatchIntakeBody(BaseModel):
    """POST /batch-intake request body: JSON records or CSV-with-mapping (exactly one).

    POST returns BatchSubmitAck (acceptance + workflow dispatch); the Detail
    shape (job + summary + records page) is served by GET /batch-intake/{job_id}
    as BatchIntakeDetail (TO_DO-026: code wins, route kept).
    """

    records: list[BatchIntakeRecord] | None = Field(
        default=None,
        max_length=MAX_BATCH_RECORDS,
        description="Validated intake records (up to the batch cap)",
    )
    csv: CsvPayload | None = Field(
        default=None, description="CSV document plus explicit field mapping"
    )


class BatchSubmitAck(BaseModel):
    """POST /batch-intake acceptance (workflow dispatch follows)."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    workflow_id: str
    status: str
    total: int


class BatchIntakeDetail(BaseModel):
    """GET /batch-intake/{job_id}: job + summary (explicit null) + records page."""

    model_config = ConfigDict(extra="forbid")

    job: BackgroundJob
    summary: BatchIntakeSummary | None
    items: list[BatchRecordResult]
    total: int
    offset: int
    limit: int


class BatchIntakeAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: str


class BatchRetryAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    workflow_id: str
    status: str
    retried: int
