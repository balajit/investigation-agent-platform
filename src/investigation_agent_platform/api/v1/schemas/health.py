# src/investigation_agent_platform/api/v1/schemas/health.py
"""Response models for the health router."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class HealthView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    dependencies: dict[str, str] | None = None
