# src/investigation_agent_platform/application/reporting/aggregate_service.py
"""Aggregate-report builder over exact cluster generations (Part 11.8).

Consumes `FindingClusterRepository` taxonomy + assignments verbatim: the
service never regroups findings. Rows are sorted deterministically
(member count descending, cluster key ascending) so identical generations
reproduce identical report bytes. Member lists are capped per cluster with
an explicit `truncated` flag rather than silently dropped.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from investigation_agent_platform.domain.common.provenance import ProvenanceRecord
from investigation_agent_platform.domain.reporting.aggregate import (
    AGGREGATE_REPORT_CONTRACT_VERSION,
    HTML_AGGREGATE_RENDERER_ID,
    HTML_AGGREGATE_RENDERER_VERSION,
    MAX_MEMBERS_PER_CLUSTER,
    MAX_REPORT_CLUSTERS,
    AggregateReport,
    AggregateReportRow,
)

REPORT_BUILDER_PLUGIN_ID = "aggregate-report-builder"
REPORT_BUILDER_VERSION = "1.0"


def _digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class InvestigationAggregateReportService:
    """Builds bound aggregate reports from cluster generations."""

    def __init__(self, cluster_repo: Any, finding_repo: Any | None = None) -> None:
        self._cluster_repo = cluster_repo
        self._finding_repo = finding_repo

    async def build(
        self,
        tenant_id: str,
        job_id: UUID | None = None,
    ) -> AggregateReport:
        """Build the report for the tenant's current taxonomy generation."""
        taxonomy = await self._cluster_repo.load_taxonomy(tenant_id)
        revision = await self._cluster_repo.current_revision(tenant_id)
        revision = max(1, revision)
        if not taxonomy:
            return AggregateReport(
                tenant_id=tenant_id,
                taxonomy_revision=revision,
                empty=True,
                input_digests={"taxonomy": _digest({"revision": revision, "keys": []})},
                provenance=self._provenance(tenant_id, job_id, revision, {}),
            )
        rows: list[AggregateReportRow] = []
        assignment_count = 0
        unassigned_count = 0
        for cluster in taxonomy[:MAX_REPORT_CLUSTERS]:
            assignments, total = await self._cluster_repo.cluster_assignments(
                tenant_id, cluster.id, limit=MAX_MEMBERS_PER_CLUSTER, offset=0
            )
            member_ids = [assignment.finding_id for assignment in assignments]
            assignment_count += total
            rows.append(
                AggregateReportRow(
                    cluster_key=cluster.cluster_key,
                    label=cluster.label,
                    description=cluster.description,
                    member_count=total,
                    member_finding_ids=member_ids,
                    truncated=total > len(member_ids),
                )
            )
        rows.sort(key=lambda row: (-row.member_count, row.cluster_key))
        unassigned_count = await self._count_unassigned(tenant_id)
        digests = {
            "taxonomy": _digest(
                {
                    "revision": revision,
                    "keys": sorted(cluster.cluster_key for cluster in taxonomy),
                }
            ),
            "assignments": _digest(
                {
                    "revision": revision,
                    "counts": {row.cluster_key: row.member_count for row in rows},
                }
            ),
        }
        return AggregateReport(
            tenant_id=tenant_id,
            taxonomy_revision=revision,
            assignment_count=assignment_count,
            unassigned_count=unassigned_count,
            rows=rows,
            input_digests=digests,
            provenance=self._provenance(tenant_id, job_id, revision, digests),
        )

    async def _count_unassigned(self, tenant_id: str) -> int:
        """Findings with no live assignment (bounded sample; exact below 2000)."""
        if self._finding_repo is None:
            return 0
        findings, _ = await self._finding_repo.list_tenant_findings(tenant_id, limit=2000)
        if not findings:
            return 0
        assigned = await self._cluster_repo.assigned_finding_ids(
            tenant_id, [finding.id for finding in findings]
        )
        return sum(1 for finding in findings if finding.id not in assigned)

    def _provenance(
        self,
        tenant_id: str,
        job_id: UUID | None,
        revision: int,
        digests: dict[str, str],
    ) -> ProvenanceRecord:
        _ = tenant_id
        return ProvenanceRecord(
            plugin_id=REPORT_BUILDER_PLUGIN_ID,
            plugin_version=REPORT_BUILDER_VERSION,
            schema_version=AGGREGATE_REPORT_CONTRACT_VERSION,
            input_digests=digests,
            parent_job_id=job_id,
            metadata={
                "taxonomy_revision": str(revision),
                "renderer_id": HTML_AGGREGATE_RENDERER_ID,
                "renderer_version": HTML_AGGREGATE_RENDERER_VERSION,
            },
        )
