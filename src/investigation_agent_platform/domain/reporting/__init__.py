# src/investigation_agent_platform/domain/reporting/__init__.py
"""Aggregate-report domain (Part 11.8)."""

from investigation_agent_platform.domain.reporting.aggregate import (
    AGGREGATE_REPORT_CONTRACT_VERSION,
    HTML_AGGREGATE_RENDERER_ID,
    HTML_AGGREGATE_RENDERER_VERSION,
    MAX_MEMBERS_PER_CLUSTER,
    MAX_REPORT_CLUSTERS,
    REPORT_HISTORY_RETENTION_DAYS,
    AggregateReport,
    AggregateReportRow,
)

__all__ = [
    "AGGREGATE_REPORT_CONTRACT_VERSION",
    "HTML_AGGREGATE_RENDERER_ID",
    "HTML_AGGREGATE_RENDERER_VERSION",
    "MAX_MEMBERS_PER_CLUSTER",
    "MAX_REPORT_CLUSTERS",
    "REPORT_HISTORY_RETENTION_DAYS",
    "AggregateReport",
    "AggregateReportRow",
]
