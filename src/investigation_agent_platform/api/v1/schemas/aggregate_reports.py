# src/investigation_agent_platform/api/v1/schemas/aggregate_reports.py
"""Response models for the aggregate-reports router."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.ports.artifacts.store import ArtifactRef


class BuildReportBody(BaseModel):
    """POST /aggregate-reports request body."""

    legal_hold: bool = Field(
        default=False,
        description="Pin a legal hold on the produced artifacts (blocks purge).",
    )


class ReportBuildAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    workflow_id: str
    status: str


class ReportMetadataView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact: ArtifactRef
    metadata: dict[str, str]
    signed_url: str
    expires_in_seconds: int


class ReportPurgeAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    purged: list[str]
