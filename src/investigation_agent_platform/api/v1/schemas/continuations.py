"""Public S7 continuation API schemas."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from investigation_agent_platform.domain.continuation.models import ContinuationStatus


class ContinuationExecuteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    continuation_id: UUID
    investigation_id: UUID
    status: ContinuationStatus
    action: str
    target: str
    price: float
    idempotent_replay: bool = False
    error_code: str | None = None
