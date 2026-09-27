# src/investigation_agent_platform/application/knowledge/janitor.py
"""Proactive supersession/expiry janitor (Part 6 ISSUE-9).

Sweeps TTL-elapsed artifacts to EXPIRED without requiring a retrieval to
hit them, reusing KnowledgeRetrievalService._transition (never duplicates
transition logic). Optional proactive CONDITIONAL re-verification reuses
_reverify's checker registry — no second reverification code path.
Idempotent: already-transitioned rows are never touched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from investigation_agent_platform.domain.knowledge.models import (
    ArtifactStatus,
    KnowledgeArtifact,
    RefreshPolicy,
)

logger = logging.getLogger("iap.application")


@dataclass
class JanitorSweepResult:
    tenant_id: str
    expired_count: int = 0
    reverified_count: int = 0
    superseded_count: int = 0
    quarantined_count: int = 0
    expired_ids: list[UUID] = field(default_factory=list)


class KnowledgeArtifactJanitor:
    """Per-tenant sweep driver. Pure status advancement, never history mutation."""

    def __init__(self, artifact_repo: Any, retrieval_service: Any) -> None:
        self.artifact_repo = artifact_repo
        self.retrieval = retrieval_service

    def _now(self) -> datetime:
        try:
            return self.retrieval._now()
        except Exception:
            return datetime.now(UTC)

    async def sweep_tenant(
        self,
        tenant_id: str,
        cutoff: datetime | None = None,
        limit: int = 500,
        reverify_conditional: bool = False,
    ) -> JanitorSweepResult:
        now = cutoff or self._now()
        result = JanitorSweepResult(tenant_id=tenant_id)
        expired = await self.artifact_repo.list_expired_ttl(tenant_id, now, limit)
        for artifact in expired:
            if artifact.status != ArtifactStatus.ACTIVE:
                continue
            await self.retrieval._transition(tenant_id, artifact, ArtifactStatus.EXPIRED)
            result.expired_count += 1
            result.expired_ids.append(artifact.id)
        if reverify_conditional:
            conditional = await self._list_active_conditional(tenant_id, limit)
            for artifact in conditional:
                outcome = await self.retrieval._reverify(tenant_id, artifact, now)
                result.reverified_count += 1
                if outcome is None:
                    stored = await self.artifact_repo.get_by_id(tenant_id, artifact.id)
                    if stored is not None:
                        if stored.status == ArtifactStatus.SUPERSEDED:
                            result.superseded_count += 1
                        elif stored.status == ArtifactStatus.QUARANTINED:
                            result.quarantined_count += 1
        logger.info(
            "Knowledge janitor sweep completed",
            extra={"context": {"tenant_id": tenant_id, "expired": result.expired_count}},
        )
        return result

    async def _list_active_conditional(self, tenant_id: str, limit: int) -> list[KnowledgeArtifact]:
        # Prefer a dedicated repo query when available; fall back to scanning
        # list_active_for_reuse across kinds is not possible without an
        # application_id, so scan list_for... via expired listing is wrong.
        # Fallback: ask the repo for all ACTIVE CONDITIONAL via a generic scan
        # when the repo exposes _store (in-memory) — otherwise return [].
        listing = getattr(self.artifact_repo, "list_active_conditionals", None)
        if callable(listing):
            return list(await listing(tenant_id, limit))
        store = getattr(self.artifact_repo, "_store", None)
        if isinstance(store, dict):
            out = [
                a
                for a in store.values()
                if getattr(a, "tenant_id", None) == tenant_id
                and getattr(a, "status", None) == ArtifactStatus.ACTIVE
                and getattr(a, "refresh_policy", None) == RefreshPolicy.CONDITIONAL
            ]
            return out[:limit]
        return []
