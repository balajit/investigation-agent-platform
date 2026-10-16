# src/investigation_agent_platform/api/v1/schemas/events.py
"""Response models for the events router (pause/resume/approve-action)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PauseAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investigation_id: str
    status: str


class ResumeAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investigation_id: str
    status: str


class ApproveActionAck(BaseModel):
    """Approval signal acknowledgement.

    `status` is the investigation status observed when the signal was
    accepted — a trace point, not a delivery guarantee for the running
    workflow. Callers that need confirmation must re-read the
    investigation.
    """

    model_config = ConfigDict(extra="forbid")

    investigation_id: str
    action_id: str
    approved: bool
    status: str
