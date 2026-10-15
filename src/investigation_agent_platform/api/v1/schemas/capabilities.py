# src/investigation_agent_platform/api/v1/schemas/capabilities.py
"""Response models for the capabilities router."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class CapabilityView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_id: str
    contract_version: str
    status: str
    modes: list[str]
    limits: dict[str, Any]
