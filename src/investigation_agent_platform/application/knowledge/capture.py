# src/investigation_agent_platform/application/knowledge/capture.py
"""Knowledge capture: concluded investigation state -> envelopes (Part 6 D4, Slice 0).

Runs AFTER conclusion. Distills interpreted facts from evidence, findings,
and effective-config snapshots into write-once envelopes. Capture failures
never fail the investigation (log + metric; the work is already done).
Mem0/Graphiti projection hooks are no-ops until Slices 1–2 land.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from investigation_agent_platform.domain.evidence.models import Evidence
from investigation_agent_platform.domain.knowledge.models import (
    ArtifactVisibility,
    KnowledgeArtifact,
    RefreshPolicy,
    sanitize_statement,
)

logger = logging.getLogger("iap.application")

# Kinds captured from conclusion-linked evidence. Static attribution is
# shareable across tenants on a merged code issue; everything else stays
# tenant-private by construction (see the domain validator).
SHARED_KINDS = frozenset({"error_signature", "domain_attribution", "code_reference"})


class KnowledgeCaptureService:
    """Distills envelopes from concluded state and projects them to stores."""

    def __init__(
        self,
        artifact_repo: Any,
        evidence_repo: Any = None,
        knowledge_store: Any = None,
        temporal_port: Any = None,
        clock_now: Any = None,
        investigation_repo: Any = None,
        evidence_summary_ttl_days: int = 90,
        max_episodes_per_investigation: int = 50,
        observability: Any = None,
    ) -> None:
        self.artifact_repo = artifact_repo
        self.evidence_repo = evidence_repo
        self.knowledge_store = knowledge_store
        self.temporal_port = temporal_port
        self._clock_now = clock_now
        self.investigation_repo = investigation_repo
        self.evidence_summary_ttl_days = evidence_summary_ttl_days
        self.max_episodes_per_investigation = max_episodes_per_investigation
        self.observability = observability
        self._group_locks: dict[str, asyncio.Lock] = {}

    def _now(self) -> datetime:
        return self._clock_now() if self._clock_now else datetime.now(UTC)

    async def capture_for_investigation(
        self, tenant_id: str, investigation_id: UUID
    ) -> list[KnowledgeArtifact]:
        now = self._now()
        try:
            artifacts = await self._distill(tenant_id, investigation_id, now)
        except Exception as exc:
            # Capture must never fail the (already concluded) investigation.
            logger.exception(
                "Knowledge capture failed; investigation unaffected",
                extra={"context": {"tenant_id": tenant_id, "error": str(exc)}},
            )
            return []
        stored: list[KnowledgeArtifact] = []
        for artifact in artifacts:
            try:
                await self.artifact_repo.save(tenant_id, artifact)
                stored.append(artifact)
            except Exception as exc:
                logger.warning(
                    "Artifact store failed; continuing with remainder",
                    extra={"context": {"tenant_id": tenant_id, "error": str(exc)}},
                )
                continue
        # D7 budget: envelopes are always stored; projection to Mem0/Graphiti
        # degrades to envelopes-only once the per-investigation episode cap
        # is hit. Prior spend is read before this batch so retries and
        # concurrent captures share one ceiling.
        prior_spend = await self._prior_spend(tenant_id, investigation_id, len(stored))
        budget_remaining = max(self.max_episodes_per_investigation - prior_spend, 0)
        projected = 0
        skipped = 0
        for artifact in stored:
            if budget_remaining <= 0:
                skipped += 1
                continue
            await self._project(tenant_id, artifact)
            budget_remaining -= 1
            projected += 1
        self._emit_telemetry(tenant_id, investigation_id, projected, skipped)
        if skipped:
            logger.warning(
                "Episode budget exceeded; degraded to envelopes-only",
                extra={
                    "context": {
                        "tenant_id": tenant_id,
                        "projected": projected,
                        "skipped": skipped,
                    }
                },
            )
        return stored

    async def _prior_spend(self, tenant_id: str, investigation_id: UUID, batch_size: int) -> int:
        """Artifacts already stored for this investigation before this batch."""
        try:
            total = await self.artifact_repo.list_for_investigation(tenant_id, investigation_id)
            return max(len(total) - batch_size, 0)
        except Exception:
            return 0

    def _emit_telemetry(
        self, tenant_id: str, investigation_id: UUID, projected: int, skipped: int
    ) -> None:
        obs = self.observability
        if obs is None:
            return
        try:
            obs.record_metric(
                "knowledge.episodes_projected",
                float(projected),
                {"tenant_id": tenant_id, "investigation_id": str(investigation_id)},
            )
            obs.record_metric(
                "knowledge.episodes_skipped_budget",
                float(skipped),
                {"tenant_id": tenant_id, "investigation_id": str(investigation_id)},
            )
        except Exception as exc:
            logger.warning(
                "Capture telemetry failed",
                extra={"context": {"tenant_id": tenant_id, "error": str(exc)}},
            )

    def _lock_for(self, group_id: str) -> asyncio.Lock:
        lock = self._group_locks.get(group_id)
        if lock is None:
            lock = asyncio.Lock()
            self._group_locks[group_id] = lock
        return lock

    async def _distill(
        self, tenant_id: str, investigation_id: UUID, now: datetime
    ) -> list[KnowledgeArtifact]:
        """Build envelopes from concluded evidence.

        Slice 0 distills two artifact classes without any LLM:
        - error_signature (SHARED): normalized error identity + code refs.
        - evidence_summary (TENANT): per-evidence interpreted summaries.
        LLM-based interpretation distillation lands with the Mem0 slice.
        """
        if self.evidence_repo is None:
            return []
        items: list[Evidence] = await self.evidence_repo.find_by_investigation_id(
            tenant_id, investigation_id
        )
        # SHARED artifacts require the investigation's code-issue fingerprint;
        # without one they downgrade to TENANT (fail closed, never share keyless).
        fingerprint: str | None = None
        if self.investigation_repo is not None:
            investigation = await self.investigation_repo.get_by_id(tenant_id, investigation_id)
            fingerprint = getattr(investigation, "code_issue_fingerprint", None)
        artifacts: list[KnowledgeArtifact] = []
        seen_signatures: set[str] = set()
        for item in items:
            signature = f"{item.provider}:{item.evidence_type.value}:{item.source}"
            if signature not in seen_signatures:
                seen_signatures.add(signature)
                artifacts.append(
                    KnowledgeArtifact(
                        tenant_id=tenant_id,
                        application_id=getattr(item, "application_id", "unknown"),
                        investigation_id=investigation_id,
                        kind="error_signature",
                        statement=sanitize_statement(
                            f"Observed {item.evidence_type.value} from {item.source}: "
                            f"{item.summary[:500]}"
                        ),
                        confidence=float(item.confidence),
                        refresh_policy=RefreshPolicy.IMMUTABLE,
                        valid_from=now,
                        source_evidence_ids=[item.evidence_id],
                        visibility=(
                            ArtifactVisibility.SHARED_CODE_ISSUE
                            if fingerprint
                            else ArtifactVisibility.TENANT
                        ),
                        code_issue_fingerprint=fingerprint,
                    )
                )
            artifacts.append(
                KnowledgeArtifact(
                    tenant_id=tenant_id,
                    application_id=getattr(item, "application_id", "unknown"),
                    investigation_id=investigation_id,
                    kind="evidence_summary",
                    statement=sanitize_statement(item.summary[:2000]),
                    confidence=float(item.confidence),
                    refresh_policy=RefreshPolicy.TTL,
                    valid_from=now,
                    valid_to=now + timedelta(days=self.evidence_summary_ttl_days),
                    source_evidence_ids=[item.evidence_id],
                    visibility=ArtifactVisibility.TENANT,
                )
            )
        return artifacts

    async def _project(self, tenant_id: str, artifact: KnowledgeArtifact) -> None:
        if self.knowledge_store is not None:
            try:
                await self.knowledge_store.project_artifact(tenant_id, artifact)
            except Exception as exc:
                logger.warning(
                    "Mem0 projection failed; envelope retained",
                    extra={"context": {"tenant_id": tenant_id, "error": str(exc)}},
                )
        if self.temporal_port is not None:
            try:
                from investigation_agent_platform.ports.knowledge.ports import (
                    investigation_group_id,
                )

                group_id = investigation_group_id(artifact.investigation_id)
                # D7 per-group serialization: concurrent captures for the same
                # group never interleave project_episode calls (Graphiti
                # entity-resolution races under concurrent same-group writes).
                async with self._lock_for(group_id):
                    await self.temporal_port.project_episode(tenant_id, group_id, artifact)
            except Exception as exc:
                logger.warning(
                    "Graphiti projection failed; envelope retained",
                    extra={"context": {"tenant_id": tenant_id, "error": str(exc)}},
                )
