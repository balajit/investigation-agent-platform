# src/investigation_agent_platform/domain/common/provenance.py
"""Provenance records for reproducibility (Part 11.3D).

Every LLM-derived, indexed, clustered, or rendered artifact links to a
ProvenanceRecord capturing exactly how it was produced. Records are
immutable value objects; repositories embed them in row payload JSON rather
than joining a separate table.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProvenanceRecord(BaseModel):
    """How one derived artifact came to exist (Part 11.3D)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default="1.0", min_length=1, max_length=32)
    source_revision: str | None = Field(default=None, max_length=256)
    input_digests: dict[str, str] = Field(default_factory=dict, max_length=50)
    plugin_id: str = Field(default="", max_length=128)
    plugin_version: str = Field(default="", max_length=32)
    model_id: str | None = Field(default=None, max_length=128)
    prompt_version: str | None = Field(default=None, max_length=32)
    parent_job_id: UUID | None = Field(default=None)
    workflow_id: str | None = Field(default=None, max_length=256)
    run_id: str | None = Field(default=None, max_length=256)
    parse_mode: str | None = Field(default=None, max_length=32)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict, max_length=50)


def attach_provenance(payload: dict[str, Any], provenance: ProvenanceRecord) -> dict[str, Any]:
    """Return a copy of payload with the provenance embedded under _provenance."""
    out = dict(payload)
    out["_provenance"] = provenance.model_dump(mode="json")
    return out


def extract_provenance(payload: dict[str, Any]) -> ProvenanceRecord | None:
    """Extract an embedded provenance record, or None when absent/invalid."""
    raw = payload.get("_provenance")
    if not isinstance(raw, dict):
        return None
    try:
        return ProvenanceRecord.model_validate(raw)
    except Exception:
        return None
