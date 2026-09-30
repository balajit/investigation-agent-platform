# src/investigation_agent_platform/domain/common/lifecycle.py
"""Resource lifecycle, retention, and audit contracts (Part 11.3D)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class ResourceLifecycle(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    DELETING = "DELETING"
    DELETED = "DELETED"
    FAILED = "FAILED"


class RetentionPolicy(BaseModel):
    """How long a resource class is kept and how it is purged (Part 11.3D)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    retention_class: str = Field(..., min_length=1, max_length=64)
    retain_days: int = Field(default=90, ge=0, le=3650)
    soft_delete: bool = Field(default=True)
    legal_hold_supported: bool = Field(default=False)
    purge_job_kind: str | None = Field(default=None, max_length=64)


class AuditEvent(BaseModel):
    """Immutable security audit record, distinct from diagnostic logs (Part 11.3D).

    Emitted for configuration activation, credential use, indexing runs,
    artifact access/deletion, input fulfillment, and suggested-action
    execution. Never carries secret values, tokens, or raw prompt content.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default="1.0", min_length=1, max_length=32)
    event_id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(..., min_length=1, max_length=128)
    application_id: str | None = Field(default=None, max_length=128)
    actor: str = Field(..., min_length=1, max_length=256)
    action: str = Field(..., min_length=1, max_length=128)
    resource_type: str = Field(..., min_length=1, max_length=128)
    resource_id: str = Field(default="", max_length=256)
    outcome: str = Field(default="success", min_length=1, max_length=32)
    detail: dict[str, Any] = Field(default_factory=dict, max_length=20)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
