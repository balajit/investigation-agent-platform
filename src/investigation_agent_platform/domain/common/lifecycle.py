# src/investigation_agent_platform/domain/common/lifecycle.py
"""Resource lifecycle, retention, and audit contracts (Part 11.3D)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


class CoreRetentionClass(StrEnum):
    """Retention class per core investigation table (TO_DO-023).

    Stored on each core row (``retention_class`` column) and keyed into
    ``CORE_RETENTION_POLICIES`` for purge cutoffs.
    """

    INVESTIGATION = "investigation"
    EVIDENCE = "evidence"
    TIMELINE = "timeline"
    TRANSITION = "transition"


#: Purge policy per core table (TO_DO-023). Only terminal investigations
#: (COMPLETED/FAILED/CANCELLED) are ever purge-eligible — open, paused, or
#: awaiting-input investigations are pinned regardless of age. Child rows
#: (evidence, timeline, transitions, facts, findings) cascade with their
#: investigation; standalone purge only collects orphan rows whose
#: ``investigation_id`` is NULL and whose timestamp predates the cutoff.
#: Transitions are never purged standalone: ``investigation_id`` is
#: non-nullable and cascades, so they disappear with their investigation.
CORE_RETENTION_POLICIES: dict[str, RetentionPolicy] = {
    CoreRetentionClass.INVESTIGATION.value: RetentionPolicy(
        retention_class=CoreRetentionClass.INVESTIGATION.value,
        retain_days=365,
        soft_delete=False,
        legal_hold_supported=False,
        purge_job_kind="core-retention-purge",
    ),
    CoreRetentionClass.EVIDENCE.value: RetentionPolicy(
        retention_class=CoreRetentionClass.EVIDENCE.value,
        retain_days=180,
        soft_delete=False,
        legal_hold_supported=False,
        purge_job_kind="core-retention-purge",
    ),
    CoreRetentionClass.TIMELINE.value: RetentionPolicy(
        retention_class=CoreRetentionClass.TIMELINE.value,
        retain_days=180,
        soft_delete=False,
        legal_hold_supported=False,
        purge_job_kind="core-retention-purge",
    ),
    CoreRetentionClass.TRANSITION.value: RetentionPolicy(
        retention_class=CoreRetentionClass.TRANSITION.value,
        retain_days=365,
        soft_delete=False,
        legal_hold_supported=False,
        purge_job_kind="core-retention-purge",
    ),
}


def retention_cutoff(policy: RetentionPolicy, now: datetime) -> datetime:
    """Purge cutoff for a retention policy: rows at or before this instant expire."""
    return now - timedelta(days=policy.retain_days)
