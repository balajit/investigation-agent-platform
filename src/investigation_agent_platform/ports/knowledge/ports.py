# src/investigation_agent_platform/ports/knowledge/ports.py
"""Knowledge layer port protocols (Part 6 Slice 0).

Backends (Mem0/Graphiti adapters in later slices) sit behind
KnowledgeStorePort/TemporalKnowledgePort; the envelope repository and
session/index stores are Postgres-backed from Slice 0.
"""

from __future__ import annotations

import re
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from investigation_agent_platform.domain.knowledge.models import (
    ArtifactView,
    InvestigationSession,
    KnowledgeArtifact,
)


def investigation_group_id(investigation_id: UUID) -> str:
    """Opaque server-derived group for one investigation's episodes.

    Hyphens stripped: group IDs must match `^[a-zA-Z0-9_-]+$` (Graphiti
    validation) and stay stable across backends. Callers never supply
    group IDs directly.
    """
    return f"inv_{str(investigation_id).replace('-', '_')}"


def baseline_group_id(tenant_id: str, application_id: str) -> str:
    """Opaque server-derived group for tenant-application baseline knowledge."""

    def _segment(value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]", "_", value)[:64].strip("_") or "unknown"

    return f"base_{_segment(tenant_id)}_{_segment(application_id)}"


@runtime_checkable
class ArtifactRepository(Protocol):
    """Tenant-scoped envelope persistence.

    Implementations MUST enforce tenant isolation: reads/writes are scoped
    to the caller's tenant, except that `SHARED_CODE_ISSUE` artifacts are
    readable by any tenant holding a session on the same
    `code_issue_fingerprint` (enforced via RLS `OR visibility` policy in
    SQL, via explicit filter in memory).
    """

    async def save(self, tenant_id: str, artifact: KnowledgeArtifact) -> None: ...

    async def get_by_id(self, tenant_id: str, artifact_id: UUID) -> KnowledgeArtifact | None: ...

    async def list_for_investigation(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[KnowledgeArtifact]: ...

    async def list_active_for_reuse(
        self, tenant_id: str, application_id: str, kinds: list[str] | None = None
    ) -> list[KnowledgeArtifact]: ...

    async def list_shared_for_fingerprint(
        self, tenant_id: str, code_issue_fingerprint: str
    ) -> list[KnowledgeArtifact]:
        """SHARED artifacts for a fingerprint, readable cross-tenant.

        Implementations MUST return only `visibility == SHARED_CODE_ISSUE`
        rows. Callers MUST additionally verify session membership before use.
        """
        ...


@runtime_checkable
class SessionRepository(Protocol):
    """Tenant-scoped session detail persistence (RLS on tenant_id)."""

    async def append(self, tenant_id: str, session: InvestigationSession) -> None: ...

    async def list_own_sessions(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[InvestigationSession]: ...

    async def count_own_sessions(self, tenant_id: str, investigation_id: UUID) -> int: ...


@runtime_checkable
class CodeIssueIndex(Protocol):
    """Tenant-free coordination index: fingerprint -> sessions.

    Contents (fingerprint, session number, opaque investigation UUID,
    timestamp) identify no tenant by construction, so this store is
    intentionally NOT tenant-scoped. Service-layer only: never expose
    directly to callers; intake and redacted-placeholder assembly only.
    """

    async def record_session(
        self,
        code_issue_fingerprint: str,
        session_number: int,
        investigation_id: UUID,
        occurred_at: Any,
    ) -> None: ...

    async def sessions_for_fingerprint(
        self, code_issue_fingerprint: str
    ) -> list[tuple[int, UUID, Any]]:
        """Return (session_number, investigation_id, occurred_at) ascending."""
        ...

    async def latest_investigation(
        self, code_issue_fingerprint: str, open_only: bool = False
    ) -> UUID | None:
        """Most recent investigation for the fingerprint.

        `open_only` filtering is applied by the caller (which can read
        statuses); the index itself is status-agnostic.
        """
        ...


@runtime_checkable
class KnowledgeStorePort(Protocol):
    """Mem0-backed preference/micro-fact projection (Slice 1+; no-op until then)."""

    async def recall_preferences(
        self, tenant_id: str, query: str, limit: int = 10
    ) -> list[ArtifactView]: ...

    async def project_artifact(self, tenant_id: str, artifact: KnowledgeArtifact) -> str | None:
        """Project one envelope; returns backend ref or None when disabled."""
        ...


@runtime_checkable
class TemporalKnowledgePort(Protocol):
    """Graphiti-backed temporal knowledge projection (Slice 2+; no-op until then)."""

    async def search_temporal(
        self, tenant_id: str, group_id: str, query: str, limit: int = 10
    ) -> list[ArtifactView]: ...

    async def project_episode(
        self, tenant_id: str, group_id: str, artifact: KnowledgeArtifact
    ) -> str | None:
        """Project one envelope as an episode; returns backend ref or None."""
        ...


@runtime_checkable
class KnowledgeCapturePort(Protocol):
    """Distillation service: concluded state -> envelopes (+ projections)."""

    async def capture_for_investigation(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[KnowledgeArtifact]: ...


@runtime_checkable
class KnowledgeRetrievalPort(Protocol):
    """Validity-gated retrieval: envelopes -> reasoner-ready views."""

    async def retrieve_for_reasoning(
        self, tenant_id: str, application_id: str, investigation_id: UUID
    ) -> Any:
        """Return KnowledgeContext; never includes stale artifacts silently."""
        ...
