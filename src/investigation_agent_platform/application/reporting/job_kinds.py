# src/investigation_agent_platform/application/reporting/job_kinds.py
"""Aggregate-report background-job descriptor + plugin registration (Part 11.8)."""

from __future__ import annotations

from typing import Any

from investigation_agent_platform.domain.common.background_job import (
    BackgroundJobKindDescriptor,
)
from investigation_agent_platform.domain.common.extension import (
    PluginKind,
    PluginManifest,
)

AGGREGATE_REPORT_JOB_KIND = "aggregate_report"
AGGREGATE_REPORT_WORKFLOW = "AggregateReportWorkflow"
AGGREGATE_REPORT_JOB_CONTRACT_VERSION = "1.0"

_REPORT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["tenant_id", "job_id"],
    "properties": {
        "tenant_id": {"type": "string"},
        "job_id": {"type": "string", "format": "uuid"},
        "legal_hold": {"type": "boolean"},
    },
}

_REPORT_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "taxonomy_revision": {"type": "integer"},
        "assignment_count": {"type": "integer"},
        "artifact_keys": {"type": "array", "items": {"type": "string"}},
    },
}


def aggregate_report_descriptor() -> BackgroundJobKindDescriptor:
    """Descriptor for the aggregate_report kind (analytics queue)."""
    return BackgroundJobKindDescriptor(
        kind=AGGREGATE_REPORT_JOB_KIND,
        contract_version=AGGREGATE_REPORT_JOB_CONTRACT_VERSION,
        input_schema=dict(_REPORT_INPUT_SCHEMA),
        result_schema=dict(_REPORT_RESULT_SCHEMA),
        task_queue="analytics-tasks",
        timeout_seconds=3600,
        max_attempts=3,
        cancellable=True,
        retention_class="reports",
        quota_class="analytics",
        required_capability="aggregate-reports",
    )


def aggregate_report_job_manifest() -> PluginManifest:
    """Manifest publishing the aggregate_report job kind."""
    return PluginManifest(
        plugin_id=f"job-kind:{AGGREGATE_REPORT_JOB_KIND}",
        plugin_kind=PluginKind.BACKGROUND_JOB_KIND,
        contract_version=AGGREGATE_REPORT_JOB_CONTRACT_VERSION,
        implementation_version="1.0",
        config_schema_version="1.0",
        capabilities=frozenset({"aggregate-reports"}),
        config_schema={"type": "object"},
    )


def register_reporting_plugins(registries: Any) -> None:
    """Register the HTML renderer + aggregate_report job kind (idempotent)."""
    from investigation_agent_platform.infrastructure.reporting.html_renderer import (
        HtmlAggregateReportRenderer,
        renderer_manifest,
    )

    if len(registries.report_renderers) == 0 or not any(
        manifest.plugin_id == renderer_manifest().plugin_id
        for manifest in registries.report_renderers.manifests()
    ):
        registries.report_renderers.register(renderer_manifest(), HtmlAggregateReportRenderer())
    manifest = aggregate_report_job_manifest()
    if not any(
        existing.plugin_id == manifest.plugin_id
        for existing in registries.background_job_kinds.manifests()
    ):
        registries.background_job_kinds.register(manifest, aggregate_report_descriptor())
