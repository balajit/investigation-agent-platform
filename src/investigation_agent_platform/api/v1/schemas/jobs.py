# src/investigation_agent_platform/api/v1/schemas/jobs.py
"""Response models for the jobs router."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from investigation_agent_platform.domain.common.background_job import BackgroundJob


class JobDetail(BaseModel):
    """`GET /jobs/{id}` returns the `{job}` wrapper, not the bare job."""

    model_config = ConfigDict(extra="forbid")

    job: BackgroundJob


class JobCancelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job: BackgroundJob
    cancelled: bool
