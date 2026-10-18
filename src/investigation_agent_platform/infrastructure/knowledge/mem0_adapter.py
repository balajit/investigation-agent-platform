# src/investigation_agent_platform/infrastructure/knowledge/mem0_adapter.py
"""Mem0 OSS adapter implementing KnowledgeStorePort (Part 6 Slice 1).

Preferences, persona, micro-facts, and interpretations are projected into
Mem0 (pgvector on the application Postgres); envelopes in Postgres remain
the source of truth and the index is rebuildable at any time.

Tenant isolation (verified against mem0ai 2.2.1 behavior):
- OSS has no `app_id`/org scoping, so the adapter namespaces `user_id` /
  `agent_id` per tenant AND stamps mandatory `tenant_id`,
  `investigation_id`, `artifact_id` metadata on every write.
- Every read injects the tenant metadata filter server-side here; filters
  are never accepted from callers.
- Mem0 OSS is ADD-only with no recency weight: supersession/expiry lives in
  the envelope layer, never assumed from the index.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from investigation_agent_platform.domain.knowledge.models import KnowledgeArtifact

logger = logging.getLogger("iap.application")

# Envelope kinds projected to Mem0. Runtime/mechanical kinds stay
# Postgres-only; the index holds recallable knowledge, not raw observations.
PROJECTED_KINDS = frozenset({"preference", "micro_fact", "interpretation"})

# Mechanical kinds stored verbatim (infer=False): no LLM extraction call,
# high-volume safe. Everything else goes through extraction.
VERBATIM_KINDS = frozenset({"micro_fact"})


def tenant_agent_id(tenant_id: str) -> str:
    """Tenant-namespaced agent persona scope. Never trust caller-supplied IDs."""
    return f"t_{tenant_id}__investigator"


def tenant_user_id(tenant_id: str, analyst_id: str) -> str:
    return f"t_{tenant_id}__analyst_{analyst_id}"


def _require_tenant(tenant_id: str) -> str:
    tenant_id = (tenant_id or "").strip()
    if not tenant_id or tenant_id == "anonymous":
        raise ValueError("Mem0 operations require an authenticated tenant_id")
    return tenant_id


class Mem0KnowledgeStore:
    """KnowledgeStorePort over Mem0 OSS + pgvector."""

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: str = "",
        embedder_model: str = "text-embedding-3-small",
        embedding_dims: int = 1536,
        collection_name: str = "iap_memories",
        pgvector_url: str = "",
        llm_provider: str = "openai",
        azure_endpoint: str = "",
        azure_api_version: str = "2025-01-01-preview",
        azure_deployment: str = "",
        azure_embedding_deployment: str = "",
        azure_embedding_endpoint: str = "",
        azure_embedding_api_version: str = "",
        azure_embedding_api_key: str = "",
    ) -> None:
        self._model = model
        self._api_key = api_key
        self._embedder_model = embedder_model
        self._embedding_dims = embedding_dims
        self._collection_name = collection_name
        self._pgvector_url = pgvector_url
        self._llm_provider = llm_provider
        self._azure_endpoint = azure_endpoint
        self._azure_api_version = azure_api_version
        self._azure_deployment = azure_deployment
        self._azure_embedding_deployment = azure_embedding_deployment
        self._azure_embedding_endpoint = azure_embedding_endpoint
        self._azure_embedding_api_version = azure_embedding_api_version
        self._azure_embedding_api_key = azure_embedding_api_key
        self._memory: Any | None = None

    def _client(self) -> Any:
        if self._memory is None:
            from mem0 import Memory  # type: ignore[import-untyped]

            if self._llm_provider == "azure_openai":
                if not self._azure_endpoint or not self._azure_deployment:
                    raise ValueError(
                        "Mem0 Azure provider requires azure_endpoint and azure_deployment"
                    )
                if not self._azure_embedding_deployment:
                    raise ValueError("Mem0 Azure provider requires azure_embedding_deployment")
                azure_kwargs = {
                    "api_key": self._api_key,
                    "azure_endpoint": self._azure_endpoint,
                    "api_version": self._azure_api_version,
                    "azure_deployment": self._azure_deployment,
                }
                llm_config: dict[str, Any] = {
                    "provider": "azure_openai",
                    "config": {
                        "model": self._azure_deployment,
                        "api_key": self._api_key,
                        "azure_kwargs": azure_kwargs,
                    },
                }
                embedder_config: dict[str, Any] = {
                    "provider": "azure_openai",
                    "config": {
                        "model": self._azure_embedding_deployment,
                        "api_key": self._azure_embedding_api_key or self._api_key,
                        "embedding_dims": self._embedding_dims,
                        "azure_kwargs": {
                            "api_key": self._azure_embedding_api_key or self._api_key,
                            "azure_endpoint": self._azure_embedding_endpoint
                            or self._azure_endpoint,
                            "api_version": self._azure_embedding_api_version
                            or self._azure_api_version,
                            "azure_deployment": self._azure_embedding_deployment,
                        },
                    },
                }
            elif self._llm_provider == "openai":
                llm_config = {
                    "provider": "openai",
                    "config": {"model": self._model},
                }
                embedder_config = {
                    "provider": "openai",
                    "config": {"model": self._embedder_model},
                }
                if self._api_key:
                    llm_config["config"]["api_key"] = self._api_key
                    embedder_config["config"]["api_key"] = self._api_key
            else:
                raise ValueError(
                    f"Unsupported mem0 llm_provider: {self._llm_provider!r} "
                    "(expected 'openai' or 'azure_openai')"
                )
            config: dict[str, Any] = {
                "llm": llm_config,
                "embedder": embedder_config,
                "vector_store": {
                    "provider": "pgvector",
                    "config": {
                        "collection_name": self._collection_name,
                        "embedding_model_dims": self._embedding_dims,
                        "connection_string": self._pgvector_url,
                    },
                },
            }
            self._memory = Memory.from_config(config)
        return self._memory

    async def recall_preferences(self, tenant_id: str, query: str, limit: int = 10) -> list[Any]:
        """Recall preference/micro-fact views for a tenant.

        Returns ArtifactViews for envelope kinds in PROJECTED_KINDS.
        Mem0 record IDs resolve back to envelopes; unknown IDs are dropped
        (index/envelope skew is expected and harmless).
        """
        from investigation_agent_platform.domain.knowledge.models import ArtifactView

        tenant_id = _require_tenant(tenant_id)
        client = self._client()
        # mem0 >=2.2 validates top-level user_id/agent_id/run_id in filters;
        # tenant isolation = namespaced user_id AND stamped tenant_id metadata.
        raw = await asyncio.to_thread(
            client.search,
            query,
            filters={
                "user_id": tenant_agent_id(tenant_id),
                "AND": [{"tenant_id": tenant_id}],
            },
            top_k=limit,
        )
        views: list[ArtifactView] = []
        for record in raw.get("results", []) if isinstance(raw, dict) else []:
            metadata = record.get("metadata", {}) or {}
            try:
                artifact_id = UUID(str(metadata.get("artifact_id", "")))
            except ValueError:
                continue
            try:
                verified_at = datetime.fromisoformat(str(metadata.get("verified_at", "")))
            except ValueError:
                verified_at = datetime.now(UTC)
            views.append(
                ArtifactView(
                    artifact_id=artifact_id,
                    statement=str(record.get("memory", ""))[:4096],
                    confidence=float(metadata.get("confidence", 0.5)),
                    verified_at=verified_at,
                    verification_source="mem0_recall",
                    code_refs=list(metadata.get("code_refs", []) or []),
                )
            )
        return views

    async def project_artifact(self, tenant_id: str, artifact: KnowledgeArtifact) -> str | None:
        """Project one envelope into Mem0. Returns the Mem0 record ID.

        Only PROJECTED_KINDS are indexed; anything else returns None
        (the envelope itself is always retained in Postgres).
        """
        tenant_id = _require_tenant(tenant_id)
        if artifact.kind not in PROJECTED_KINDS:
            return None
        client = self._client()
        metadata = {
            "tenant_id": tenant_id,
            "application_id": artifact.application_id,
            "investigation_id": str(artifact.investigation_id),
            "artifact_id": str(artifact.id),
            "kind": artifact.kind,
            "confidence": artifact.confidence,
            "verified_at": artifact.valid_from.isoformat(),
            "code_refs": list(artifact.code_refs),
        }
        result = await asyncio.to_thread(
            client.add,
            artifact.statement,
            user_id=tenant_agent_id(tenant_id),
            run_id=f"inv_{artifact.investigation_id}",
            metadata=metadata,
            infer=artifact.kind not in VERBATIM_KINDS,
        )
        results = result.get("results", []) if isinstance(result, dict) else []
        if not results:
            raise RuntimeError("Mem0 add() returned no records")
        record_id = str(results[0].get("id", ""))
        logger.info(
            "Projected artifact to Mem0",
            extra={"context": {"tenant_id": tenant_id, "artifact_id": str(artifact.id)}},
        )
        return record_id or None
