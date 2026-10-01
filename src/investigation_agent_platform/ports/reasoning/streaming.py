# src/investigation_agent_platform/ports/reasoning/streaming.py
"""Streaming LLM gateway + chat context builder ports (Part 11.10).

Streaming is a separate capability contract from completion: the structural
`LLMGateway` requirement is untouched. Streams emit provider text chunks;
the application layer assembles, validates, and accounts for them.
"""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class StreamTextChunk(BaseModel):
    """One provider text delta (Part 11.10)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(default="", max_length=16000)
    done: bool = Field(default=False)
    finish_reason: str | None = Field(default=None, max_length=64)


class ChatContext(BaseModel):
    """Retrieved context for one chat turn (Part 11.10)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    system_prompt: str = Field(default="", max_length=8000)
    snippets: list[str] = Field(default_factory=list, max_length=20)


@runtime_checkable
class StreamingLLMGateway(Protocol):
    """Provider text streaming (Part 11.10).

    Implementations must close the provider stream when the consumer stops
    iterating (disconnect/cancel/timeout). No retries after the first chunk
    has been yielded.
    """

    async def stream(
        self,
        tenant_id: str,
        system_prompt: str,
        messages: list[dict[str, str]],
    ) -> AsyncIterator[StreamTextChunk]:
        """Yield text deltas; closing iteration closes the provider stream."""
        ...


@runtime_checkable
class ChatContextBuilderPort(Protocol):
    """Builds per-turn chat context from existing knowledge retrieval."""

    async def build_context(self, tenant_id: str, system_prompt: str, query: str) -> ChatContext:
        """Assemble system prompt + bounded snippets for one turn."""
        ...
