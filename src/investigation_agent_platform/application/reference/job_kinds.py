# src/investigation_agent_platform/application/reference/job_kinds.py
"""Reference-docs reindex background-job descriptor + registration (Part 11.6)."""

from __future__ import annotations

from typing import Any

from investigation_agent_platform.domain.common.background_job import (
    BackgroundJobKindDescriptor,
)
from investigation_agent_platform.domain.common.extension import (
    PluginKind,
    PluginManifest,
)

REFERENCE_DOCS_REINDEX_JOB_KIND = "reference_docs_reindex"
REFERENCE_DOCS_INDEX_WORKFLOW = "ReferenceDocsIndexWorkflow"
REFERENCE_DOCS_JOB_CONTRACT_VERSION = "1.0"

_REINDEX_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["tenant_id", "job_id", "source_id"],
    "properties": {
        "tenant_id": {"type": "string"},
        "job_id": {"type": "string", "format": "uuid"},
        "source_id": {"type": "string"},
        "application_id": {"type": "string"},
    },
}

_REINDEX_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "generation": {"type": "integer"},
        "files_seen": {"type": "integer"},
        "chunks_written": {"type": "integer"},
        "tombstoned": {"type": "integer"},
    },
}


def reference_docs_reindex_descriptor() -> BackgroundJobKindDescriptor:
    """Descriptor for the reference_docs_reindex kind (indexing queue)."""
    return BackgroundJobKindDescriptor(
        kind=REFERENCE_DOCS_REINDEX_JOB_KIND,
        contract_version=REFERENCE_DOCS_JOB_CONTRACT_VERSION,
        input_schema=dict(_REINDEX_INPUT_SCHEMA),
        result_schema=dict(_REINDEX_RESULT_SCHEMA),
        task_queue="indexing-tasks",
        timeout_seconds=3600,
        max_attempts=3,
        cancellable=True,
        retention_class="default",
        quota_class="indexing",
        required_capability="reference-docs",
    )


def reference_docs_reindex_manifest() -> PluginManifest:
    """Manifest publishing the reference_docs_reindex job kind."""
    return PluginManifest(
        plugin_id=f"job-kind:{REFERENCE_DOCS_REINDEX_JOB_KIND}",
        plugin_kind=PluginKind.BACKGROUND_JOB_KIND,
        contract_version=REFERENCE_DOCS_JOB_CONTRACT_VERSION,
        implementation_version="1.0",
        config_schema_version="1.0",
        capabilities=frozenset({"reference-docs"}),
        config_schema={"type": "object"},
    )


def register_reference_plugins(registries: Any) -> None:
    """Register the reference_docs_reindex job kind (idempotent)."""
    manifest = reference_docs_reindex_manifest()
    if not any(
        existing.plugin_id == manifest.plugin_id
        for existing in registries.background_job_kinds.manifests()
    ):
        registries.background_job_kinds.register(manifest, reference_docs_reindex_descriptor())
