# src/investigation_agent_platform/ports/knowledge/embeddings.py
"""Text embedding provider port (Part 11.7).

Converts finding text into fixed-dimension vectors for candidate rerank.
Optional: the clustering service runs lexical-only when no embedder is
configured. Embedding spaces must never be compared across
(embedding_model, embedding_version) — enforced by callers filtering on both.
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class EmbeddingResult(BaseModel):
    """One embedded text with its immutable space identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    vector: list[float] = Field(default_factory=list, max_length=4096)
    embedding_model: str = Field(..., min_length=1, max_length=128)
    embedding_version: str = Field(default="1.0", min_length=1, max_length=32)


@runtime_checkable
class EmbedderPort(Protocol):
    """Text → vector embedding provider (Part 11.7)."""

    async def embed(self, texts: list[str]) -> list[EmbeddingResult]:
        """Embed texts in order; raises on provider failure (no silent zeros)."""
        ...
