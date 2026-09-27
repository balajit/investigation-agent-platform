# src/investigation_agent_platform/infrastructure/knowledge/graphiti_adapter.py
"""Graphiti self-hosted adapter implementing TemporalKnowledgePort (Part 6 Slice 2).

Temporal/state knowledge (flag evaluations, effective config snapshots,
policy outcomes, interpretations) is projected as episodes into a shared
Neo4j instance, namespaced by server-derived opaque `group_id`s. Envelopes
in Postgres remain the source of truth; the graph is a disposable temporal
index, rebuildable at any time.

Tenant isolation (verified against graphiti-core 0.30.2 behavior):
- `group_id` is namespacing, not authorization (official docs warn
  accordingly). All group IDs are derived server-side here; callers never
  supply them.
- Every public method re-validates the tenant and scopes reads to the
  given group(s) only. Cross-group fan-out is explicit and audited, never
  implicit.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from investigation_agent_platform.domain.knowledge.models import KnowledgeArtifact

logger = logging.getLogger("iap.application")

# Envelope kinds projected as Graphiti episodes. Preference/micro-fact kinds
# live in Mem0; dual-writing them here would split the source of truth.
PROJECTED_KINDS = frozenset(
    {
        "interpretation",
        "policy_outcome",
        "error_signature",
        "flag_evaluation",
        "effective_config",
        "domain_attribution",
    }
)

_GROUP_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _require_tenant(tenant_id: str) -> str:
    tenant_id = (tenant_id or "").strip()
    if not tenant_id or tenant_id == "anonymous":
        raise ValueError("Graphiti operations require an authenticated tenant_id")
    return tenant_id


def _require_group(group_id: str) -> str:
    if not group_id or not _GROUP_ID_RE.match(group_id):
        raise ValueError(f"Refusing unscoped graph namespace: {group_id!r}")
    return group_id


class GraphitiTemporalKnowledge:
    """TemporalKnowledgePort over self-hosted graphiti-core + Neo4j."""

    def __init__(
        self,
        neo4j_uri: str = "",
        neo4j_user: str = "",
        neo4j_password: str = "",
        neo4j_database: str = "neo4j",
        model: str = "gpt-4o-mini",
        api_key: str = "",
        embedder_model: str = "text-embedding-3-small",
        semaphore_limit: int = 5,
    ) -> None:
        self._neo4j_uri = neo4j_uri
        self._neo4j_user = neo4j_user
        self._neo4j_password = neo4j_password
        self._neo4j_database = neo4j_database
        self._model = model
        self._api_key = api_key
        self._embedder_model = embedder_model
        self._semaphore_limit = semaphore_limit
        self._client: Any | None = None
        self._indices_initialized = False

    def _client_or_raise(self) -> Any:
        if self._client is None:
            from graphiti_core import Graphiti  # type: ignore[import-untyped]
            from graphiti_core.embedder.openai import OpenAIEmbedderConfig
            from graphiti_core.llm_client import OpenAIClient
            from graphiti_core.llm_client.config import LLMConfig

            if not self._neo4j_uri:
                raise ValueError("Graphiti requires a Neo4j URI (IAP_KNOWLEDGE_GRAPHITI_NEO4J_URI)")
            llm_config: dict[str, Any] = {"model": self._model}
            embedder_config: dict[str, Any] = {"embedding_model": self._embedder_model}
            if self._api_key:
                llm_config["api_key"] = self._api_key
                embedder_config["api_key"] = self._api_key
            self._client = Graphiti(
                uri=self._neo4j_uri,
                user=self._neo4j_user or None,
                password=self._neo4j_password or None,
                llm_client=OpenAIClient(config=LLMConfig(**llm_config)),
                embedder=OpenAIEmbedder(
                    config=OpenAIEmbedderConfig(**embedder_config),
                ),
                max_coroutines=self._semaphore_limit,
            )
        return self._client

    async def ensure_indices(self) -> None:
        """Create constraints/indexes once (idempotent; safe to call repeatedly)."""
        if self._indices_initialized:
            return
        client = self._client_or_raise()
        await client.build_indices_and_constraints()
        self._indices_initialized = True

    async def search_temporal(
        self, tenant_id: str, group_id: str, query: str, limit: int = 10
    ) -> list[Any]:
        """Search currently-valid temporal facts within exactly one group.

        Post-filters to `invalid_at IS NULL` (currently-valid) because the
        simple search surface does not apply temporal filters itself; expired
        edges must never reach the reasoner silently.
        """
        from investigation_agent_platform.domain.knowledge.models import ArtifactView

        tenant_id = _require_tenant(tenant_id)
        _require_group(group_id)
        client = self._client_or_raise()
        edges = await client.search(query, group_ids=[group_id], num_results=limit)
        views: list[ArtifactView] = []
        now = datetime.now(UTC)
        for edge in edges:
            invalid_at = getattr(edge, "invalid_at", None)
            if invalid_at is not None:
                continue
            episodes = getattr(edge, "episodes", None) or []
            artifact_id = self._artifact_id_from_episodes(episodes)
            if artifact_id is None:
                continue
            views.append(
                ArtifactView(
                    artifact_id=artifact_id,
                    statement=str(getattr(edge, "fact", ""))[:4096],
                    confidence=0.5,
                    verified_at=now,
                    verification_source="graphiti_search",
                    code_refs=[],
                )
            )
        logger.info(
            "Graphiti temporal search completed",
            extra={"context": {"tenant_id": tenant_id, "group_id": group_id}},
        )
        return views

    async def project_episode(
        self, tenant_id: str, group_id: str, artifact: KnowledgeArtifact
    ) -> str | None:
        """Project one envelope as a JSON episode. Returns the episode UUID.

        Only PROJECTED_KINDS are indexed; anything else returns None (the
        envelope itself is always retained in Postgres). `reference_time` is
        the artifact's validity start — never ingest time — so temporal
        extraction resolves relative dates correctly.
        """
        tenant_id = _require_tenant(tenant_id)
        _require_group(group_id)
        if artifact.kind not in PROJECTED_KINDS:
            return None
        if artifact.tenant_id != tenant_id:
            raise ValueError("Artifact tenant mismatch: refusing cross-tenant projection")
        client = self._client_or_raise()
        body = json.dumps(
            {
                "statement": artifact.statement,
                "kind": artifact.kind,
                "confidence": artifact.confidence,
                "artifact_id": str(artifact.id),
                "tenant_id": tenant_id,
                "application_id": artifact.application_id,
                "investigation_id": str(artifact.investigation_id),
                "code_refs": list(artifact.code_refs),
                "source_evidence_ids": [str(e) for e in artifact.source_evidence_ids],
            },
            default=str,
        )
        result = await client.add_episode(
            name=f"{artifact.kind}:{artifact.id}",
            episode_body=body,
            source_description=f"investigation {artifact.investigation_id} knowledge capture",
            reference_time=artifact.valid_from,
            group_id=group_id,
        )
        episode = result.episode
        episode_uuid = str(getattr(episode, "uuid", ""))
        logger.info(
            "Projected artifact episode to Graphiti",
            extra={"context": {"tenant_id": tenant_id, "artifact_id": str(artifact.id)}},
        )
        return episode_uuid or None

    @staticmethod
    def _artifact_id_from_episodes(episodes: list[Any]) -> UUID | None:
        # Episode names are f"{kind}:{artifact_id}" (format we control);
        # JSON bodies additionally carry an explicit artifact_id key.
        for episode in episodes:
            name = str(getattr(episode, "name", "") or "")
            if ":" in name:
                try:
                    return UUID(name.rsplit(":", 1)[-1].strip())
                except ValueError:
                    pass
            content = str(getattr(episode, "content", "") or "")
            if content:
                try:
                    payload = json.loads(content)
                except ValueError:
                    continue
                if isinstance(payload, dict) and payload.get("artifact_id"):
                    try:
                        return UUID(str(payload["artifact_id"]))
                    except ValueError:
                        continue
        return None
