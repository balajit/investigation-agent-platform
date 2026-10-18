# src/investigation_agent_platform/api/v1/schemas/clusters.py
"""Response models for the clusters router."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.finding.clustering import (
    FindingCluster,
    FindingClusterAssignment,
)
from investigation_agent_platform.domain.finding.models import Finding


class RunClustersBody(BaseModel):
    """POST /clusters/run request body."""

    limit: int = Field(default=500, ge=1, le=2000, description="Maximum findings to cluster in one run")
    batch_size: int = Field(default=25, ge=1, le=100, description="Findings per clustering batch")


class ClusterDetail(BaseModel):
    """Cluster detail with paginated member findings (not the bare cluster)."""

    model_config = ConfigDict(extra="forbid")

    cluster: FindingCluster
    assignments: list[FindingClusterAssignment]
    findings: list[Finding]
    total: int
    offset: int
    limit: int


class ClusterRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    workflow_id: str
    status: str
