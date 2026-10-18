# src/investigation_agent_platform/application/intake/job_kinds.py
"""Bulk-intake background-job descriptor + registration (Part 11.9)."""

from __future__ import annotations

from typing import Any

from investigation_agent_platform.domain.common.background_job import (
    BackgroundJobKindDescriptor,
)
from investigation_agent_platform.domain.common.extension import (
    PluginKind,
    PluginManifest,
)

BATCH_INTAKE_JOB_KIND = "batch-intake"
BATCH_INTAKE_WORKFLOW = "BulkIntakeWorkflow"
BATCH_INTAKE_JOB_CONTRACT_VERSION = "1.0"

_BATCH_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["tenant_id", "job_id"],
    "properties": {
        "tenant_id": {"type": "string"},
        "job_id": {"type": "string", "format": "uuid"},
        "start_index": {"type": "integer", "minimum": 0},
        "attempt": {"type": "integer", "minimum": 0},
    },
}

_BATCH_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "total": {"type": "integer"},
        "succeeded": {"type": "integer"},
        "failed": {"type": "integer"},
        "awaiting_input": {"type": "integer"},
        "canceled": {"type": "integer"},
    },
}


def batch_intake_descriptor() -> BackgroundJobKindDescriptor:
    """Descriptor for the batch-intake kind (investigation queue)."""
    return BackgroundJobKindDescriptor(
        kind=BATCH_INTAKE_JOB_KIND,
        contract_version=BATCH_INTAKE_JOB_CONTRACT_VERSION,
        input_schema=dict(_BATCH_INPUT_SCHEMA),
        result_schema=dict(_BATCH_RESULT_SCHEMA),
        task_queue="investigation-tasks",
        timeout_seconds=7200,
        max_attempts=3,
        cancellable=True,
        retention_class="batch",
        quota_class="batch",
        required_capability="batch-intake",
    )


def batch_intake_job_manifest() -> PluginManifest:
    """Manifest publishing the batch-intake job kind."""
    return PluginManifest(
        plugin_id=f"job-kind:{BATCH_INTAKE_JOB_KIND}",
        plugin_kind=PluginKind.BACKGROUND_JOB_KIND,
        contract_version=BATCH_INTAKE_JOB_CONTRACT_VERSION,
        implementation_version="1.0",
        config_schema_version="1.0",
        capabilities=frozenset({"batch-intake"}),
        config_schema={"type": "object"},
    )


def register_intake_plugins(registries: Any) -> None:
    """Register the batch-intake job kind (idempotent)."""
    manifest = batch_intake_job_manifest()
    if not any(
        existing.plugin_id == manifest.plugin_id
        for existing in registries.background_job_kinds.manifests()
    ):
        registries.background_job_kinds.register(manifest, batch_intake_descriptor())
