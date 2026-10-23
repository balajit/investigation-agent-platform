# src/investigation_agent_platform/api/v1/schemas/deletion.py
"""Typed deletion preview/approval/status contracts (Phase 5, D7/D8)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DeletionPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str | None = Field(default=None, max_length=128)
    environment: str = Field(min_length=1, max_length=64)
    deployment_id: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=32)
    requester_id: str = Field(min_length=1, max_length=256)
    approver_ids: list[str] = Field(default_factory=list, max_length=8)
    backup_id: str = Field(min_length=1, max_length=256)
    expires_at: datetime


class DeletionPreviewRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    manifest_id: UUID
    plan_hash: str
    state: str
    blocking_codes: list[str] = Field(default_factory=list)
    resource_counts: dict[str, int] = Field(default_factory=dict)


class DeletionApprovalMintRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_id: UUID
    approver_id: str = Field(min_length=1, max_length=256)
    requester_id: str = Field(min_length=1, max_length=256)
    policy_version: str = Field(min_length=1, max_length=32)
    store_registry_version: str = Field(min_length=1, max_length=32)
    expires_at: datetime
    nonce: str | None = Field(default=None, min_length=32, max_length=128)


class DeletionApprovalReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    manifest_id: UUID
    approval_token: str
    expires_at: datetime
    consumed: bool


class DeletionApprovalConsumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_token: str = Field(min_length=1, max_length=256)
    approver_id: str = Field(min_length=1, max_length=256)


class DeletionConsumeReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    manifest_id: UUID
    purge_job_id: UUID | None = None
    consumed: bool


class DeletionOutcomeRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_key: str
    state: str


class DeletionRequestRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    manifest_id: UUID
    state: str
    plan_hash: str
    store_registry_version: str
    outcomes: list[DeletionOutcomeRead] = Field(default_factory=list)
    blocking_codes: list[str] = Field(default_factory=list)
    expires_at: datetime
