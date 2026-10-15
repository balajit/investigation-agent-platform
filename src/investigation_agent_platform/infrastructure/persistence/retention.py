# src/investigation_agent_platform/infrastructure/persistence/retention.py
"""Core-table retention purge driver (TO_DO-023).

Mirrors the existing janitor patterns (``KnowledgeArtifactJanitor.sweep_tenant``
per-tenant sweep with cutoff + limit, ``TopologySnapshotRetentionWorkflow``
per-repository isolation so one failing repository never blocks the rest of
the pass). Scheduling (Temporal Schedule invoking this driver) is intentionally
left to the workflow layer — this module never touches the store directly
except through the repository ports, so it is fully unit-testable without a
database.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.api.errors import sanitize_extra
from investigation_agent_platform.domain.common.lifecycle import (
    CORE_RETENTION_POLICIES,
    CoreRetentionClass,
    retention_cutoff,
)

logger = logging.getLogger(__name__)


class CoreRetentionPurgeResult(BaseModel):
    """Counts from one tenant's core-retention purge pass."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., min_length=1, max_length=128)
    investigations_purged: int = Field(default=0, ge=0)
    evidence_purged: int = Field(default=0, ge=0)
    timeline_purged: int = Field(default=0, ge=0)


class CoreRetentionPurge:
    """Per-tenant purge driver over the core investigation tables.

    Only terminal investigations (COMPLETED/FAILED/CANCELLED) older than the
    ``investigation`` retention cutoff are hard-deleted; child rows
    (evidence, timeline, transitions, facts, findings, conclusions) cascade
    via ``ondelete="CASCADE"`` FKs. Orphan evidence/timeline rows (NULL
    ``investigation_id``) older than their cutoffs are collected standalone.
    Transitions are never purged standalone: ``investigation_id`` is
    non-nullable and cascades, so they disappear with their investigation.
    Open, paused, or awaiting-input investigations are pinned regardless of
    age — ``purge_expired`` on the investigation repository enforces this.
    """

    def __init__(
        self,
        investigation_repo: Any,
        evidence_repo: Any,
        timeline_repo: Any,
    ) -> None:
        self._investigations = investigation_repo
        self._evidence = evidence_repo
        self._timeline = timeline_repo

    async def purge_tenant(
        self,
        tenant_id: str,
        now: datetime | None = None,
        limit: int = 500,
    ) -> CoreRetentionPurgeResult:
        """One tenant's scheduled retention pass. Idempotent; per-repository
        failures are recorded as zero counts for that repository (with an
        exception log) so one failing repository never blocks the rest."""
        at = now or datetime.now(UTC)
        logger.info(
            "core retention purge started",
            extra=sanitize_extra(
                {
                    "tenant_id": tenant_id,
                    "event": "core_retention.purge_tenant",
                    "outcome": "started",
                }
            ),
        )
        investigations_purged = await self._purge_one(
            tenant_id=tenant_id,
            repo=self._investigations,
            cutoff=retention_cutoff(
                CORE_RETENTION_POLICIES[CoreRetentionClass.INVESTIGATION.value], at
            ),
            limit=limit,
        )
        evidence_purged = await self._purge_one(
            tenant_id=tenant_id,
            repo=self._evidence,
            cutoff=retention_cutoff(
                CORE_RETENTION_POLICIES[CoreRetentionClass.EVIDENCE.value], at
            ),
            limit=limit,
        )
        timeline_purged = await self._purge_one(
            tenant_id=tenant_id,
            repo=self._timeline,
            cutoff=retention_cutoff(
                CORE_RETENTION_POLICIES[CoreRetentionClass.TIMELINE.value], at
            ),
            limit=limit,
        )
        result = CoreRetentionPurgeResult(
            tenant_id=tenant_id,
            investigations_purged=investigations_purged,
            evidence_purged=evidence_purged,
            timeline_purged=timeline_purged,
        )
        logger.info(
            "core retention purge completed",
            extra=sanitize_extra(
                {
                    "tenant_id": tenant_id,
                    "event": "core_retention.purge_tenant",
                    "outcome": "purged",
                    "investigations_purged": investigations_purged,
                    "evidence_purged": evidence_purged,
                    "timeline_purged": timeline_purged,
                }
            ),
        )
        return result

    async def _purge_one(
        self, tenant_id: str, repo: Any, cutoff: datetime, limit: int
    ) -> int:
        try:
            return int(await repo.purge_expired(tenant_id, cutoff, limit))
        except Exception as exc:
            logger.exception(
                "core retention repository purge failed",
                extra=sanitize_extra(
                    {
                        "tenant_id": tenant_id,
                        "event": "core_retention.purge_repository",
                        "outcome": "failed",
                        "repository": type(repo).__name__,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                ),
            )
            return 0
