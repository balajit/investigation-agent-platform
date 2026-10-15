# src/investigation_agent_platform/integrations/spring_config/models.py
"""Typed Spring Config Server results and tenant binding declarations."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConfigResult(BaseModel):
    """Merged, allowlisted configuration evidence from a Config Server fetch."""

    model_config = ConfigDict(frozen=True)

    application: str = Field(..., max_length=256)
    profile: str = Field(..., max_length=256)
    label: str | None = Field(default=None, max_length=256)
    properties: dict[str, Any] = Field(default_factory=dict)
    property_sources: tuple[str, ...] = Field(default_factory=tuple)


class SpringBinding(BaseModel):
    """Operator-supplied (server, application, profile, label) import binding."""

    model_config = ConfigDict(frozen=True)

    server_url: str = Field(..., max_length=1024)
    application: str = Field(..., max_length=256)
    profile: str = Field(..., max_length=256)
    label: str | None = Field(default=None, max_length=256)
