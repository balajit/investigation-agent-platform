# src/investigation_agent_platform/api/v1/schemas/knowledge.py
"""Response models for the knowledge router."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from investigation_agent_platform.domain.knowledge.models import KnowledgeArtifact


class KnowledgeSessionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_number: int
    occurred_at: str
    status: str
    log_refs: list[str]
    trace_refs: list[str]


class OtherTenantSessionView(BaseModel):
    """Redacted placeholder: counts and numbers truthful, identity never leaves."""

    model_config = ConfigDict(extra="forbid")

    session_number: int
    occurred_at: str
    tenant: str


class KnowledgeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    investigation_id: str
    code_issue_fingerprint: str | None
    artifacts: list[KnowledgeArtifact]
    sessions: list[KnowledgeSessionView]
    other_tenant_sessions: list[OtherTenantSessionView]
