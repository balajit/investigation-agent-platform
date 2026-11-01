"""Typed, immutable records for exact S7 continuation fetches."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from investigation_agent_platform.domain.finding.models import (
    MAX_FETCH_PRICE,
    HandoffState,
)
from investigation_agent_platform.domain.profile.capability_policy import ACTION_POLICY

S7_CONTINUATION_VERSION_CURRENT = "s7-continuation-v1"
S7_CONTINUATION_MAX_ATTEMPTS = 20
S7_CONTINUATION_TTL_SECONDS = 7 * 24 * 60 * 60
S7_CONTINUATION_LEASE_SECONDS = 600


class ContinuationStatus(StrEnum):
    """Lifecycle states of a single exact-fetch continuation."""

    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"
    SUPERSEDED = "SUPERSEDED"


class S7Continuation(BaseModel):
    """One durable, single-use edge from an S7 handoff to an action."""

    model_config = ConfigDict(frozen=True)

    continuation_id: UUID
    version: str = Field(default=S7_CONTINUATION_VERSION_CURRENT, max_length=32)
    tenant_id: str = Field(..., max_length=128)
    investigation_id: UUID
    handoff_version: str = Field(..., max_length=16)
    source_handoff_state: HandoffState
    action: str = Field(..., max_length=64)
    parameters: dict[str, Any]
    target: str = Field(..., max_length=1024)
    price: float = Field(..., gt=0.0, le=MAX_FETCH_PRICE)
    idempotency_key: str = Field(..., min_length=16, max_length=128)
    status: ContinuationStatus = ContinuationStatus.PENDING
    attempt_count: int = Field(default=0, ge=0, le=S7_CONTINUATION_MAX_ATTEMPTS)
    created_at: datetime
    expires_at: datetime
    claimed_at: datetime | None = None
    completed_at: datetime | None = None
    last_error_code: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_payload(self) -> S7Continuation:
        if self.source_handoff_state not in {
            HandoffState.BUDGET_DEFERRED,
            HandoffState.TELEMETRY_DEFERRED,
            HandoffState.CAPABILITY_DENIED,
        }:
            raise ValueError("only resumable S7 handoff states may create continuations")
        if not self.target.strip():
            raise ValueError("continuation target is required")
        if self.action not in {action.value for action in ACTION_POLICY}:
            raise ValueError("continuation action is not an executable acquiring action")
        return self
