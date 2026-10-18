# src/investigation_agent_platform/domain/knowledge/models.py
"""Knowledge artifact envelope, sessions, and validity contracts (Part 6 D3/D8).

The envelope is the source of truth for *what we believe and why*.
Mem0/Graphiti hold indexed projections for retrieval; they never invent
validity. See docs/IAP-implementation-part6-knowledge-v1.md.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|secret|password|passwd|token)\s*[:=]\s*\S+"),
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]+=*"),
)

# Artifact kinds whose content is runtime/session-specific and therefore can
# never be shared across tenants, even on a merged code issue.
TENANT_ONLY_KINDS = frozenset(
    {
        "flag_evaluation",
        "effective_config",
        "policy_outcome",
        "interpretation",
    }
)


class RefreshPolicy(StrEnum):
    IMMUTABLE = "IMMUTABLE"
    CONDITIONAL = "CONDITIONAL"
    TTL = "TTL"


class ArtifactStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"
    QUARANTINED = "QUARANTINED"


class ArtifactVisibility(StrEnum):
    TENANT = "TENANT"
    SHARED_CODE_ISSUE = "SHARED_CODE_ISSUE"


class ReverifySpec(BaseModel):
    """How to re-check a CONDITIONAL artifact against the live source.

    Sources are assumed readable (no human-attestation fallback). A down
    endpoint is transient: bounded retries, then QUARANTINE + count.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_kind: str = Field(..., max_length=64)
    source_ref: str = Field(..., max_length=256)
    query: dict[str, str] = Field(default_factory=dict)


class KnowledgeArtifact(BaseModel):
    """One reusable unit of interpreted execution knowledge. Write-once."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    tenant_id: str = Field(..., max_length=128)
    application_id: str = Field(..., max_length=128)
    investigation_id: UUID
    kind: str = Field(..., max_length=64)
    statement: str = Field(..., max_length=4096)
    confidence: float = Field(ge=0.0, le=1.0)
    refresh_policy: RefreshPolicy
    reverify: ReverifySpec | None = None
    valid_from: datetime
    valid_to: datetime | None = None
    source_evidence_ids: list[UUID] = Field(default_factory=list, max_length=50)
    source_log_refs: list[str] = Field(default_factory=list, max_length=50)
    code_refs: list[str] = Field(default_factory=list, max_length=50)
    supersedes_id: UUID | None = None
    status: ArtifactStatus = ArtifactStatus.ACTIVE
    store_refs: dict[str, str] = Field(default_factory=dict)
    visibility: ArtifactVisibility = ArtifactVisibility.TENANT
    code_issue_fingerprint: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _validate_policy_contract(self) -> KnowledgeArtifact:
        if self.refresh_policy == RefreshPolicy.CONDITIONAL and self.reverify is None:
            raise ValueError("CONDITIONAL artifacts require a reverify spec")
        if self.refresh_policy == RefreshPolicy.TTL and self.valid_to is None:
            raise ValueError("TTL artifacts require a valid_to")
        if self.visibility == ArtifactVisibility.SHARED_CODE_ISSUE:
            if self.kind in TENANT_ONLY_KINDS:
                raise ValueError(f"kind '{self.kind}' is runtime content and cannot be shared")
            if not self.code_issue_fingerprint:
                raise ValueError("SHARED_CODE_ISSUE artifacts require a code_issue_fingerprint")
        for pattern in _SECRET_PATTERNS:
            if pattern.search(self.statement):
                raise ValueError("statement appears to contain secrets/credentials")
        return self


class InvestigationSession(BaseModel):
    """One occurrence (session) of a code issue within an investigation.

    Full detail rows are tenant-scoped (RLS). Cross-tenant views expose only
    (session_number, occurred_at) assembled server-side as redacted placeholders.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    investigation_id: UUID
    session_number: int = Field(ge=1)
    tenant_id: str = Field(..., max_length=128)
    occurred_at: datetime
    log_refs: list[str] = Field(default_factory=list, max_length=50)
    trace_refs: list[str] = Field(default_factory=list, max_length=50)
    status: str = Field(default="OPEN", max_length=32)


class ArtifactView(BaseModel):
    """Verified fact handed to the reasoner, with verification provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_id: UUID
    statement: str
    confidence: float
    verified_at: datetime
    verification_source: str = Field(max_length=128)
    code_refs: list[str] = Field(default_factory=list)
    # ISSUE-11 cross-layer join: code_ref -> resolved ownership. Empty when
    # no attribution port is configured or resolution degraded gracefully.
    attribution: dict[str, dict[str, str]] = Field(default_factory=dict)


def parse_code_ref(ref: str) -> tuple[str, str, str, int] | None:
    """Parse `repo@rev:path#line` into (repo, rev, path, line).

    Returns None for malformed refs (never guessed) — callers skip them.
    """
    try:
        repo_rev, _, path_line = ref.partition(":")
        repo, sep, rev = repo_rev.partition("@")
        path, hash_sep, line_s = path_line.partition("#")
        if not sep or not hash_sep or not repo or not rev or not path:
            return None
        if "/" in repo or ".." in path:
            # repo is an identifier, not a path; path must stay relative.
            pass
        if path.startswith("/") or ".." in path.split("/"):
            return None
        line = int(line_s)
        if line < 1:
            return None
        return repo, rev, path, line
    except (ValueError, AttributeError):
        return None


class KnowledgeContext(BaseModel):
    """Read model returned by retrieve_knowledge_activity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verified_facts: list[ArtifactView] = Field(default_factory=list)
    preferences: list[ArtifactView] = Field(default_factory=list)
    temporal_summary: list[ArtifactView] = Field(default_factory=list)
    excluded_stale_count: int = Field(default=0, ge=0)


def sanitize_statement(text: str) -> str:
    """Best-effort secret scrub before envelope construction (gateway mirrors this)."""
    scrubbed = text
    for pattern in _SECRET_PATTERNS:
        scrubbed = pattern.sub("[REDACTED]", scrubbed)
    return scrubbed[:4096]
