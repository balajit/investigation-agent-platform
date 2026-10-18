# src/investigation_agent_platform/application/reference/__init__.py
"""Reference-document application services (Part 11.6)."""

from investigation_agent_platform.application.reference.job_kinds import (
    REFERENCE_DOCS_INDEX_WORKFLOW,
    REFERENCE_DOCS_JOB_CONTRACT_VERSION,
    REFERENCE_DOCS_REINDEX_JOB_KIND,
    reference_docs_reindex_descriptor,
    reference_docs_reindex_manifest,
    register_reference_plugins,
)
from investigation_agent_platform.application.reference.reference_service import (
    ReferenceDocumentService,
    source_id_for,
)

__all__ = [
    "REFERENCE_DOCS_INDEX_WORKFLOW",
    "REFERENCE_DOCS_JOB_CONTRACT_VERSION",
    "REFERENCE_DOCS_REINDEX_JOB_KIND",
    "ReferenceDocumentService",
    "reference_docs_reindex_descriptor",
    "reference_docs_reindex_manifest",
    "register_reference_plugins",
    "source_id_for",
]
