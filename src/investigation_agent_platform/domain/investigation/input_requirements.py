# src/investigation_agent_platform/domain/investigation/input_requirements.py
"""Structured input requirements: durable workflow-blocked-on-caller-data (Part 11.5).

When an activity discovers prerequisite data the profile cannot source
automatically, the workflow persists an ``InputRequirement``, transitions to
``AWAITING_INPUT``, and waits for a Temporal Update carrying the data.
Fulfillment is compare-and-set on ``(requirement_id, requirement_version)``:
identical replays are no-ops, conflicting replays fail. Raw fulfillment data
travels via the Update (durable in workflow history); the fulfillment audit
row stores a content digest, never the raw payload.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.investigation.models import InvestigationStatus

#: Active workflow states that may suspend into AWAITING_INPUT.
AWAITING_INPUT_SOURCES = frozenset(
    {
        InvestigationStatus.CONTEXTUALIZING,
        InvestigationStatus.INVESTIGATING,
        InvestigationStatus.CORRELATING,
        InvestigationStatus.HYPOTHESIZING,
        InvestigationStatus.VERIFYING,
        InvestigationStatus.CONCLUDING,
        InvestigationStatus.PAUSED,
    }
)

#: States an AWAITING_INPUT investigation may resume into or divert to.
AWAITING_INPUT_TARGETS = frozenset(
    {
        InvestigationStatus.CONTEXTUALIZING,
        InvestigationStatus.INVESTIGATING,
        InvestigationStatus.CORRELATING,
        InvestigationStatus.HYPOTHESIZING,
        InvestigationStatus.VERIFYING,
        InvestigationStatus.CONCLUDING,
        InvestigationStatus.PAUSED,
        InvestigationStatus.CANCELLED,
        InvestigationStatus.FAILED,
    }
)

#: Maximum serialized fulfillment payload (64 KiB) — bounds Update history.
MAX_FULFILLMENT_BYTES = 65_536


class RequirementState(StrEnum):
    PENDING = "PENDING"
    FULFILLED = "FULFILLED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class InputRequirement(BaseModel):
    """A structured description of data the workflow cannot proceed without."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    requirement_id: UUID = Field(default_factory=uuid4)
    requirement_version: int = Field(default=1, ge=1)
    tenant_id: str = Field(..., min_length=1, max_length=128)
    investigation_id: UUID
    reason: str = Field(..., min_length=1, max_length=512)
    json_schema: dict[str, Any] = Field(default_factory=dict, max_length=50)
    classification: str = Field(default="INTERNAL", min_length=1, max_length=32)
    resume_status: InvestigationStatus = InvestigationStatus.INVESTIGATING
    promote_to_evidence: bool = Field(default=False)
    state: RequirementState = RequirementState.PENDING
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = Field(default=None)
    provenance: dict[str, Any] = Field(default_factory=dict, max_length=20)


class InputFulfillment(BaseModel):
    """Immutable audit record for one requirement fulfillment (Part 11.5).

    Carries a content digest and size — never the raw payload, which lives in
    workflow history and, when permitted, in evidence storage.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    requirement_id: UUID
    requirement_version: int = Field(ge=1)
    tenant_id: str = Field(..., min_length=1, max_length=128)
    investigation_id: UUID
    fulfilled_by: str = Field(..., min_length=1, max_length=256)
    content_digest: str = Field(..., min_length=1, max_length=128)
    content_bytes: int = Field(ge=0)
    fulfilled_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
