# src/investigation_agent_platform/application/reporting/__init__.py
"""Aggregate reporting application services (Part 11.8)."""

from investigation_agent_platform.application.reporting.aggregate_service import (
    InvestigationAggregateReportService,
)
from investigation_agent_platform.application.reporting.job_kinds import (
    AGGREGATE_REPORT_JOB_CONTRACT_VERSION,
    AGGREGATE_REPORT_JOB_KIND,
    AGGREGATE_REPORT_WORKFLOW,
    aggregate_report_descriptor,
    aggregate_report_job_manifest,
    register_reporting_plugins,
)

__all__ = [
    "AGGREGATE_REPORT_JOB_CONTRACT_VERSION",
    "AGGREGATE_REPORT_JOB_KIND",
    "AGGREGATE_REPORT_WORKFLOW",
    "InvestigationAggregateReportService",
    "aggregate_report_descriptor",
    "aggregate_report_job_manifest",
    "register_reporting_plugins",
]
