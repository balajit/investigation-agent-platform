# src/investigation_agent_platform/domain/reporting/aggregate.py
"""Versioned aggregate-report domain models (Part 11.8).

Reports consume an exact cluster-taxonomy revision and assignment
generation: they never regroup findings independently. Every report binds
the taxonomy revision, assignment counts, renderer version, input digests,
and provenance so the same generation always reproduces identical bytes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.common.provenance import ProvenanceRecord

#: Contract version for aggregate-report payloads and renderer input.
AGGREGATE_REPORT_CONTRACT_VERSION = "1.0"

#: Renderer identifier for the inaugural Jinja2 HTML renderer.
HTML_AGGREGATE_RENDERER_ID = "report-renderer:html-aggregate-v1"

#: Renderer implementation version pinned into every report.
HTML_AGGREGATE_RENDERER_VERSION = "1.0"

#: History retention for stored report runs (days); enforced on purge.
REPORT_HISTORY_RETENTION_DAYS = 90

#: Bounds keeping workflow inputs and rendered output small.
MAX_REPORT_CLUSTERS = 25
MAX_MEMBERS_PER_CLUSTER = 200


class AggregateReportRow(BaseModel):
    """One taxonomy member rendered into a report (Part 11.8)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cluster_key: str = Field(..., min_length=1, max_length=16)
    label: str = Field(..., min_length=1, max_length=128)
    description: str = Field(default="", max_length=512)
    member_count: int = Field(default=0, ge=0)
    member_finding_ids: list[UUID] = Field(default_factory=list, max_length=MAX_MEMBERS_PER_CLUSTER)
    truncated: bool = Field(
        default=False,
        description="True when members exceeded the per-cluster cap.",
    )


class AggregateReport(BaseModel):
    """A reproducible aggregate report over one taxonomy generation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(..., min_length=1, max_length=128)
    taxonomy_revision: int = Field(..., ge=1)
    assignment_count: int = Field(default=0, ge=0)
    unassigned_count: int = Field(default=0, ge=0)
    rows: list[AggregateReportRow] = Field(default_factory=list, max_length=MAX_REPORT_CLUSTERS)
    empty: bool = Field(
        default=False,
        description="True when the taxonomy had no members to render.",
    )
    renderer_id: str = Field(default=HTML_AGGREGATE_RENDERER_ID, max_length=128)
    renderer_version: str = Field(default=HTML_AGGREGATE_RENDERER_VERSION, max_length=32)
    contract_version: str = Field(default=AGGREGATE_REPORT_CONTRACT_VERSION, max_length=32)
    input_digests: dict[str, str] = Field(default_factory=dict, max_length=32)
    provenance: ProvenanceRecord | None = Field(default=None)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
