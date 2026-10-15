# src/investigation_agent_platform/api/v1/schemas/evidence.py
"""Response models for the evidence router."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from investigation_agent_platform.domain.evidence.models import Evidence


class EvidenceDetail(BaseModel):
    """Single-evidence wrapper (`GET …/evidence/{id}` returns `{evidence}`)."""

    model_config = ConfigDict(extra="forbid")

    evidence: Evidence
