# src/investigation_agent_platform/api/v1/schemas/evidence.py
"""Response models for the evidence router."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.evidence.models import Evidence


class EvidenceDetail(BaseModel):
    """Single-evidence wrapper (`GET …/evidence/{id}` returns `{evidence}`)."""

    model_config = ConfigDict(extra="forbid")

    evidence: Evidence


class SharedEvidenceLink(BaseModel):
    """One shared canonical row: who else banked interest in it (G6)."""

    model_config = ConfigDict(extra="forbid")

    shared: bool = Field(default=True)
    investigations: list[str] = Field(default_factory=list)


class EvidenceSharingResponse(BaseModel):
    """`GET …/evidence/sharing` returns key → link for shared rows only."""

    model_config = ConfigDict(extra="forbid")

    links: dict[str, SharedEvidenceLink] = Field(default_factory=dict)
