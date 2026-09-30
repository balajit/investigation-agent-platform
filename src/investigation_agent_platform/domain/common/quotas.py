# src/investigation_agent_platform/domain/common/quotas.py
"""Quota policy: tenant/application fairness limits (Part 11.3D).

Quotas are checked atomically before workflow dispatch by a
QuotaEnforcerPort implementation. ``429`` + ``Retry-After`` signals
rate/concurrency exhaustion; ``413`` signals payload/source size violations.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class QuotaPolicy(BaseModel):
    """Limits resolved per (tenant_id, application_id, quota_class)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_concurrent_investigations: int = Field(default=10, ge=0, le=1000)
    max_concurrent_jobs: int = Field(default=5, ge=0, le=500)
    max_queued_jobs: int = Field(default=50, ge=0, le=5000)
    max_batch_records: int = Field(default=500, ge=1, le=5000)
    max_reference_source_bytes: int = Field(default=500_000_000, ge=0)
    max_reference_files: int = Field(default=50_000, ge=0)
    max_vector_rows: int = Field(default=5_000_000, ge=0)
    max_llm_tokens_per_day: int = Field(default=10_000_000, ge=0)
    max_active_chat_sessions: int = Field(default=100, ge=0, le=10000)
    max_active_streams: int = Field(default=100, ge=0, le=10000)
    max_report_history: int = Field(default=50, ge=0, le=1000)
    max_artifact_bytes: int = Field(default=10_000_000_000, ge=0)


class QuotaCheckResult(BaseModel):
    """Outcome of one atomic quota check (Part 11.3D)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    quota_class: str = Field(default="default", max_length=64)
    violated_limit: str | None = Field(default=None, max_length=128)
    current_usage: int = Field(default=0, ge=0)
    limit: int = Field(default=0, ge=0)
    retry_after_seconds: int | None = Field(default=None, ge=0)
