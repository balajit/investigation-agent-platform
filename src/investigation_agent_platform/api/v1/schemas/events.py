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
    model_config = ConfigDict(extra="forbid")

    investigation_id: str
    action_id: str
    approved: bool
