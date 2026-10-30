# src/investigation_agent_platform/application/knowledge/retrieve.py
"""Validity-gated retrieval: envelopes -> reasoner-ready views (Part 6 D4, Slice 0).

Only ACTIVE artifacts whose validity holds *right now* reach the reasoner.
CONDITIONAL artifacts re-verify inline via their reverify spec against a
registered live-source checker: match -> include as verified; mismatch ->
supersede + exclude; checker missing/down (bounded retries exhausted) ->
QUARANTINE + exclude + count. Nothing stale is ever included silently.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from investigation_agent_platform.domain.knowledge.models import (
    ArtifactStatus,
    ArtifactView,
    KnowledgeArtifact,
    KnowledgeContext,
    RefreshPolicy,
)
from investigation_agent_platform.infrastructure.observability.tracing import traced

logger = logging.getLogger("iap.application")


def _knowledge_extra(context: Any) -> dict[str, Any]:
    """Context composition counts (never fact text)."""
    try:
        extra: dict[str, Any] = {}
        for key in ("verified_facts", "preferences", "temporal_summary"):
            value = getattr(context, key, None)
            if isinstance(value, list):
                extra[f"iap.{key}_count"] = len(value)
        excluded = getattr(context, "excluded_stale_count", None)
        if isinstance(excluded, int):
            extra["iap.excluded_stale_count"] = excluded
        return extra
    except Exception:
        return {}


# Checker: (tenant_id, reverify_spec) -> True if the live source still agrees.
ReverifyChecker = Callable[[str, Any], Awaitable[bool]]

# R3 context-cache envelope version. Bump when the cached shape changes;
# older envelopes fail soft to a miss, never to a crash.
CACHE_FORMAT_VERSION = 1
CACHE_KEY_PREFIX = "iap:knctx"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def cache_enabled() -> bool:
    """R3 kill-switch (env `IAP_KNOWLEDGE_CACHE_ENABLED`, default false)."""
    return os.environ.get("IAP_KNOWLEDGE_CACHE_ENABLED", "false").lower() == "true"


def cache_ttl_seconds() -> int:
    """R3 TTL (env `IAP_KNOWLEDGE_CACHE_TTL_SECONDS`, default 120; 0 disables store)."""
    return _env_int("IAP_KNOWLEDGE_CACHE_TTL_SECONDS", 120)


def cache_key(
    tenant_id: str,
    application_id: str,
    investigation_id: UUID,
    kinds: list[str] | None,
    code_issue_fingerprints: list[str] | None,
) -> str:
    """Tenant-first cache key. Fingerprints cover kinds + membership proof —
    different proofs MUST NOT alias (cross-tenant SHARED reads differ)."""
    digest = hashlib.sha256(
        "\x1f".join(
            [
                application_id,
                *sorted(kinds or []),
                *sorted(code_issue_fingerprints or []),
            ]
        ).encode("utf-8")
    ).hexdigest()[:32]
    return f"{CACHE_KEY_PREFIX}:{tenant_id}:{investigation_id}:{digest}"


class KnowledgeRetrievalService:
    """Gate envelopes into a KnowledgeContext the reasoner can trust."""

    def __init__(
        self,
        artifact_repo: Any,
        checkers: dict[str, ReverifyChecker] | None = None,
        max_reverify_attempts: int = 3,
        clock_now: Any = None,
        knowledge_store: Any = None,
        temporal_port: Any = None,
        attribution_port: Any = None,
        profile_repo: Any = None,
    ) -> None:
        self.artifact_repo = artifact_repo
        self.checkers: dict[str, ReverifyChecker] = dict(checkers or {})
        self.max_reverify_attempts = max_reverify_attempts
        self._clock_now = clock_now
        # Part 6 Slice 1: optional Mem0 projection for preference recall.
        # None = preferences stay empty; envelope facts unaffected.
        self.knowledge_store = knowledge_store
        # Part 6 Slice 2: optional Graphiti projection for temporal recall.
        # None = temporal_summary stays empty; envelope facts unaffected.
        self.temporal_port = temporal_port
        # ISSUE-11: optional Layer 3 cross-layer join. None = byte-for-byte
        # legacy behavior (refs stay opaque strings, no ownership attached).
        self.attribution_port = attribution_port
        self.profile_repo = profile_repo
        # R3: optional context cache (port backend, policy in this layer).
        # None = every call runs the full pipeline (legacy behavior).
        self.context_cache: Any | None = None
        self.context_cache_ttl_seconds: int = 0

    def _now(self) -> datetime:
        return self._clock_now() if self._clock_now else datetime.now(UTC)

    async def _confidence_floor(self, tenant_id: str, application_id: str) -> float | None:
        """Profile min_confidence when explicitly authored, else None (no filter)."""
        if self.profile_repo is None:
            return None
        try:
            profile = await self.profile_repo.get_by_application_id(tenant_id, application_id)
        except Exception:
            return None
        knowledge = getattr(profile, "knowledge", None) if profile else None
        if knowledge is None or "min_confidence" not in knowledge.model_fields_set:
            return None
        floor = knowledge.min_confidence
        return floor if isinstance(floor, (int, float)) and 0.0 <= floor <= 1.0 else None

    def register_checker(self, source_kind: str, checker: ReverifyChecker) -> None:
        self.checkers[source_kind] = checker

    def attach_context_cache(self, cache: Any | None, ttl_seconds: int = 0) -> None:
        """R3: attach a `KnowledgeContextCachePort` backend. None detaches."""
        self.context_cache = cache
        self.context_cache_ttl_seconds = ttl_seconds

    async def _cache_lookup(
        self,
        tenant_id: str,
        application_id: str,
        investigation_id: UUID,
        kinds: list[str] | None,
        code_issue_fingerprints: list[str] | None,
    ) -> KnowledgeContext | None:
        """Return the cached context on hit, else None. Fail-open: any cache
        error (or envelope mismatch) is a miss, never a failure."""
        if self.context_cache is None:
            return None
        try:
            raw = await self.context_cache.get(
                cache_key(
                    tenant_id, application_id, investigation_id, kinds, code_issue_fingerprints
                )
            )
        except Exception as exc:
            logger.warning("Context cache lookup failed open: %s", exc)
            return None
        if not raw:
            return None
        try:
            import json as _json

            envelope = _json.loads(raw)
            if (
                not isinstance(envelope, dict)
                or envelope.get("format_version") != CACHE_FORMAT_VERSION
            ):
                return None
            return KnowledgeContext.model_validate(envelope["context"])
        except Exception as exc:
            logger.warning("Context cache envelope rejected (miss): %s", exc)
            return None

    async def _cache_store(
        self,
        tenant_id: str,
        application_id: str,
        investigation_id: UUID,
        kinds: list[str] | None,
        code_issue_fingerprints: list[str] | None,
        context: KnowledgeContext,
    ) -> None:
        """Store the built context. Fail-open; never stores when TTL disables."""
        if self.context_cache is None or self.context_cache_ttl_seconds <= 0:
            return
        try:
            import json as _json

            envelope = _json.dumps(
                {"format_version": CACHE_FORMAT_VERSION, "context": context.model_dump(mode="json")}
            )
            await self.context_cache.set(
                cache_key(
                    tenant_id, application_id, investigation_id, kinds, code_issue_fingerprints
                ),
                envelope,
                self.context_cache_ttl_seconds,
            )
        except Exception as exc:
            logger.warning("Context cache store failed open: %s", exc)

    @traced("knowledge", component="retrieval", extra_from=_knowledge_extra)
    async def retrieve_for_reasoning(
        self,
        tenant_id: str,
        application_id: str,
        investigation_id: UUID,
        kinds: list[str] | None = None,
        code_issue_fingerprints: list[str] | None = None,
    ) -> KnowledgeContext:
        """Build the reasoner context.

        `code_issue_fingerprints` is the membership proof for cross-tenant
        SHARED reads: fingerprints of code issues the caller holds a session
        on (resolved by the caller from its own tenant-scoped session rows).
        Empty/absent means no sharing — fail closed.
        """
        logger.info(
            "retrieve_for_reasoning started",
            extra={
                "tenant_id": tenant_id,
                "investigation_id": str(investigation_id),
                "application_id": application_id,
                "kinds_count": len(kinds or []),
                "fingerprint_proofs": len(code_issue_fingerprints or []),
                "event": "retrieve_for_reasoning started",
            },
        )
        # R3: cached contexts skip the envelope-gate transitions for the TTL
        # window (janitor + TTL bound this; conclusions always re-validate
        # citations from live rows, never from cached context).
        cached = await self._cache_lookup(
            tenant_id, application_id, investigation_id, kinds, code_issue_fingerprints
        )
        if cached is not None:
            logger.info(
                "retrieve_for_reasoning cache hit",
                extra={
                    "tenant_id": tenant_id,
                    "investigation_id": str(investigation_id),
                    "verified_facts": len(cached.verified_facts),
                    "event": "retrieve_for_reasoning cache hit",
                },
            )
            return cached
        now = self._now()
        own = await self.artifact_repo.list_active_for_reuse(tenant_id, application_id, kinds)
        shared = await self._shared_for_caller(tenant_id, code_issue_fingerprints or [], kinds)
        min_confidence = await self._confidence_floor(tenant_id, application_id)
        seen: set[UUID] = set()
        verified: list[ArtifactView] = []
        excluded_stale = 0
        for artifact in (*own, *shared):
            if artifact.id in seen:
                continue
            seen.add(artifact.id)
            # Profile KnowledgeConfig.min_confidence — explicit-only, so
            # default profiles keep unfiltered reuse behavior.
            if min_confidence is not None and artifact.confidence < min_confidence:
                excluded_stale += 1
                continue
            view = await self._gate_one(tenant_id, artifact, now)
            if view is None:
                excluded_stale += 1
            else:
                view = await self._join_attribution(
                    tenant_id, application_id, investigation_id, view
                )
                verified.append(view)
        preferences = await self._recall_preferences(tenant_id, application_id)
        temporal_summary = await self._recall_temporal(tenant_id, application_id, investigation_id)
        context = KnowledgeContext(
            verified_facts=verified,
            preferences=preferences,
            temporal_summary=temporal_summary,
            excluded_stale_count=excluded_stale,
        )
        logger.info(
            "retrieve_for_reasoning completed",
            extra={
                "tenant_id": tenant_id,
                "investigation_id": str(investigation_id),
                "verified_facts": len(verified),
                "preferences": len(preferences),
                "temporal_summary": len(temporal_summary),
                "excluded_stale_count": excluded_stale,
                "event": "retrieve_for_reasoning completed",
            },
        )
        await self._cache_store(
            tenant_id, application_id, investigation_id, kinds, code_issue_fingerprints, context
        )
        return context

    @traced("knowledge", component="retrieval")
    async def _recall_preferences(self, tenant_id: str, application_id: str) -> list[ArtifactView]:
        """Best-effort preference recall. Never fails retrieval."""
        if self.knowledge_store is None:
            return []
        try:
            views = await self.knowledge_store.recall_preferences(
                tenant_id, f"analyst preferences for {application_id}", limit=10
            )
            views = list(views)
            logger.info(
                "preference recall completed",
                extra={
                    "tenant_id": tenant_id,
                    "preferences": len(views),
                    "event": "preference recall completed",
                },
            )
            return views
        except Exception as exc:
            logger.warning(
                "Preference recall failed; continuing without preferences",
                extra={"context": {"tenant_id": tenant_id, "error": str(exc)}},
            )
            return []

    @traced("knowledge", component="retrieval")
    async def _recall_temporal(
        self, tenant_id: str, application_id: str, investigation_id: UUID
    ) -> list[ArtifactView]:
        """Best-effort temporal recall across investigation + baseline groups.

        Group IDs are server-derived here (never caller-supplied). Only
        views whose artifact resolves to a known envelope are returned —
        the adapter already drops unjoinable records; this layer additionally
        caps the merged list. Never fails retrieval.
        """
        if self.temporal_port is None:
            return []
        try:
            from investigation_agent_platform.ports.knowledge.ports import (
                baseline_group_id,
                investigation_group_id,
            )

            query = f"incident timeline and state for {application_id}"
            seen: set[UUID] = set()
            merged: list[ArtifactView] = []
            for group in (
                investigation_group_id(investigation_id),
                baseline_group_id(tenant_id, application_id),
            ):
                views = await self.temporal_port.search_temporal(tenant_id, group, query, limit=10)
                for view in views:
                    if view.artifact_id in seen:
                        continue
                    seen.add(view.artifact_id)
                    merged.append(view)
            merged = merged[:20]
            logger.info(
                "temporal recall completed",
                extra={
                    "tenant_id": tenant_id,
                    "temporal_views": len(merged),
                    "event": "temporal recall completed",
                },
            )
            return merged
        except Exception as exc:
            logger.warning(
                "Temporal recall failed; continuing without temporal summary",
                extra={"context": {"tenant_id": tenant_id, "error": str(exc)}},
            )
            return []

    async def _shared_for_caller(
        self, tenant_id: str, fingerprints: list[str], kinds: list[str] | None
    ) -> list[KnowledgeArtifact]:
        # Cross-tenant SHARED reads are membership-gated: only fingerprints
        # the caller provably holds a session on (resolved by the caller from
        # its own tenant-scoped session rows and passed in explicitly).
        out: list[KnowledgeArtifact] = []
        for fingerprint in fingerprints:
            rows = await self.artifact_repo.list_shared_for_fingerprint(tenant_id, fingerprint)
            out.extend(r for r in rows if kinds is None or r.kind in kinds)
        logger.info(
            "shared envelopes resolved",
            extra={
                "tenant_id": tenant_id,
                "proofs": len(fingerprints),
                "shared_artifacts": len(out),
                "event": "shared envelopes resolved",
            },
        )
        return out

    async def _gate_one(
        self, tenant_id: str, artifact: KnowledgeArtifact, now: datetime
    ) -> ArtifactView | None:
        if artifact.status != ArtifactStatus.ACTIVE:
            return None
        if artifact.valid_to is not None and artifact.valid_to <= now:
            await self._transition(tenant_id, artifact, ArtifactStatus.EXPIRED)
            return None
        if artifact.refresh_policy == RefreshPolicy.IMMUTABLE:
            return self._view(artifact, now, "envelope_validity")
        if artifact.refresh_policy == RefreshPolicy.TTL:
            # TTL without explicit valid_to is treated as already elapsed:
            # fail closed rather than assuming freshness. A future valid_to
            # means the window still holds: include as verified.
            if artifact.valid_to is None:
                await self._transition(tenant_id, artifact, ArtifactStatus.EXPIRED)
                return None
            return self._view(artifact, now, "envelope_ttl_validity")
        return await self._reverify(tenant_id, artifact, now)

    async def _reverify(
        self, tenant_id: str, artifact: KnowledgeArtifact, now: datetime
    ) -> ArtifactView | None:
        spec = artifact.reverify
        checker = self.checkers.get(spec.source_kind) if spec else None
        if checker is None:
            # No registered live-source checker: sources are assumed readable
            # per stakeholder decision, so a missing checker is a wiring gap
            # to quarantine loudly — never a silent pass.
            logger.warning(
                "No reverify checker registered; quarantining artifact",
                extra={
                    "context": {
                        "tenant_id": tenant_id,
                        "artifact_id": str(artifact.id),
                        "source_kind": spec.source_kind if spec else None,
                    }
                },
            )
            await self._transition(tenant_id, artifact, ArtifactStatus.QUARANTINED)
            return None
        assert spec is not None
        last_error: str | None = None
        for _ in range(self.max_reverify_attempts):
            try:
                if await checker(tenant_id, spec):
                    return self._view(
                        artifact, now, f"live_reverify:{spec.source_kind}:{spec.source_ref}"
                    )
                last_error = "live source disagrees"
                break
            except Exception as exc:
                last_error = str(exc)
                await asyncio.sleep(0)
        # Mismatch or exhausted retries: supersede so history shows what
        # changed, and exclude from reasoning.
        logger.warning(
            "Reverification failed; superseding artifact",
            extra={
                "context": {
                    "tenant_id": tenant_id,
                    "artifact_id": str(artifact.id),
                    "reason": last_error,
                }
            },
        )
        await self._transition(tenant_id, artifact, ArtifactStatus.SUPERSEDED)
        return None

    async def _transition(
        self, tenant_id: str, artifact: KnowledgeArtifact, status: ArtifactStatus
    ) -> None:
        transitioned = artifact.model_copy(update={"status": status})
        try:
            await self.artifact_repo.save(tenant_id, transitioned)
        except Exception as exc:
            logger.warning(
                "Artifact status transition failed",
                extra={"context": {"tenant_id": tenant_id, "error": str(exc)}},
            )

    @staticmethod
    def _view(artifact: KnowledgeArtifact, now: datetime, source: str) -> ArtifactView:
        return ArtifactView(
            artifact_id=artifact.id,
            statement=artifact.statement,
            confidence=artifact.confidence,
            verified_at=now,
            verification_source=source,
            code_refs=list(artifact.code_refs),
        )

    async def _join_attribution(
        self,
        tenant_id: str,
        application_id: str,
        investigation_id: UUID,
        view: ArtifactView,
    ) -> ArtifactView:
        """ISSUE-11: resolve code_refs against Layer 3. Never fails retrieval."""
        if self.attribution_port is None or not view.code_refs:
            return view
        from investigation_agent_platform.domain.knowledge.models import parse_code_ref

        joined: dict[str, dict[str, str]] = {}
        for ref in view.code_refs:
            parsed = parse_code_ref(ref)
            if parsed is None:
                continue
            repo, rev, path, line = parsed
            try:
                ownership = await self.attribution_port.resolve_source_location(
                    tenant_id,
                    application_id,
                    investigation_id,
                    repo,
                    rev,
                    path,
                    line,
                )
                joined[ref] = {
                    "domain_id": str(getattr(ownership, "domain_id", "") or ""),
                    "fallback_level": str(getattr(ownership, "fallback_level", "") or ""),
                }
            except Exception as exc:
                logger.warning(
                    "Code-ref join degraded; ref kept without ownership",
                    extra={"context": {"tenant_id": tenant_id, "ref": ref, "error": str(exc)}},
                )
                continue
        if not joined:
            return view
        return view.model_copy(update={"attribution": joined})
