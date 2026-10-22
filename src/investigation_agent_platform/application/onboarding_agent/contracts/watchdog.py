# src/investigation_agent_platform/application/onboarding_agent/contracts/watchdog.py
"""Watchdog diagnosis contract shapes (agent design §§2, 7).

The invoking workflow supplies the snapshot; the agent never queries control
state on its own authority. The clock is caller-supplied (``now``) so
diagnosis is deterministic: identical snapshots diagnose identically, and no
wall-clock read can leak into a hashed or audited artifact.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.application.onboarding_agent.contracts.coverage import (
    Confidence,
)


class StageObservation(BaseModel):
    """One stage visit as reported by the invoking workflow."""

    model_config = ConfigDict(extra="forbid")

    stage: str = Field(min_length=1, max_length=64)
    entered_at: datetime
    lease_fencing_token: int | None = Field(default=None, ge=0)


class WatchdogSnapshot(BaseModel):
    """Tenant-scoped control-store state the workflow explicitly supplies."""

    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    revision: int = Field(ge=1)
    generation: int = Field(ge=1)
    current_stage: str = Field(min_length=1, max_length=64)
    stages: list[StageObservation] = Field(default_factory=list)
    stuck_bounds_seconds: dict[str, int] = Field(default_factory=dict)
    consecutive_failures: int = Field(default=0, ge=0)
    now: datetime
    notes: list[str] = Field(default_factory=list)


class RankedCause(BaseModel):
    """One ranked diagnostic hypothesis with its distinguishing tests.

    Mirrors the Claim probe rule: a non-VERIFIED cause without a
    distinguishing test is a guess, not a diagnosis.
    """

    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=1)
    cause: str = Field(min_length=1)
    confidence: Confidence
    evidence: list[str] = Field(default_factory=list)
    distinguishing_tests: list[str] = Field(min_length=1)


class Diagnosis(BaseModel):
    """Ranked diagnosis for one snapshot. Always carries at least one cause:
    ``no anomaly`` and ``insufficient evidence`` are explicit causes, never
    empty output.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    revision: int = Field(ge=1)
    generation: int = Field(ge=1)
    causes: list[RankedCause] = Field(min_length=1)
