# src/investigation_agent_platform/api/v1/schemas/onboarding.py
"""Typed onboarding request/response contracts (Phase 4, design D9)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.application.onboarding.intake import TenantIntakeV1


class IntakePutRequest(BaseModel):
    """Next immutable intake revision with optimistic-concurrency guard."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    intake: TenantIntakeV1


class ApproveRequest(BaseModel):
    """Approve an exact request revision."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)


class RetryRequest(BaseModel):
    """Make one RETRYABLE dispatch due immediately."""

    model_config = ConfigDict(extra="forbid")

    dispatch_id: UUID


class CancelRequest(BaseModel):
    """Request cancellation for one revision."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)


class EvaluateReadinessRequest(BaseModel):
    """Evaluate stored probe evidence for one revision and tier."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)
    tier: str = Field(default="standard", min_length=1, max_length=32)


class ApplyResponse(BaseModel):
    """202 payload: durable accept with job and dispatch identity."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    request_id: UUID
    revision: int
    job_id: UUID
    dispatch_id: UUID
    workflow_id: str
    dispatch_state: str
    status_url: str


class IntakePutRead(BaseModel):
    """New immutable revision receipt; no acceptance implied."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    request_id: UUID
    revision: int
    content_hash: str
    accepted: bool


class OnboardingRequestRead(BaseModel):
    """Tenant-authorized progress and blockers for one request."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    request_id: UUID
    revision: int
    state: str
    state_version: int
    job_id: UUID | None = None
    readiness_decision: str | None = None
    blocking_codes: list[str] = Field(default_factory=list)
    registry_status: str | None = None


class ApprovalReceipt(BaseModel):
    """Activation receipt for an approved revision."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    status: str
    epoch: int
    request_id: UUID | None = None
    revision: int | None = None


class RetryReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    dispatch_id: UUID
    retried: bool


class CancelReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    request_id: UUID
    state: str


class LifecycleReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    status: str
    epoch: int


class CapabilityRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_key: str
    version: str
    required: bool
    disposition: str
    owner: str
    readiness_probe: str


class CapabilityListRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    tier: str
    capabilities: list[CapabilityRead]


class ProbeReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    request_id: UUID
    revision: int
    capability_key: str
    outcome: str


class ReadinessReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    request_id: UUID
    revision: int
    tier: str
    decision: str
    blocking_codes: list[str] = Field(default_factory=list)


class CoverageReportRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    request_id: UUID
    revision: int
    generation: int
    report_content_hash: str
    report: dict[str, Any]


class TelemetryVerifyRequest(BaseModel):
    """Inline telemetry endpoint declarations to verify live (no filesystem)."""

    model_config = ConfigDict(extra="forbid")

    home_service: str = Field(min_length=1, max_length=128)
    endpoints: list[dict[str, Any]] = Field(min_length=1, max_length=20)
    mapping_source_id: str | None = Field(default=None, max_length=128)


class TelemetryCheckItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    passed: bool
    detail: str = ""


class TelemetryVerifyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    passed: bool
    checks: list[TelemetryCheckItem]
