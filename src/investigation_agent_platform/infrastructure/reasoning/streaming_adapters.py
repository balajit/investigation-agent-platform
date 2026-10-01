# src/investigation_agent_platform/infrastructure/reasoning/streaming_adapters.py
"""Streaming LLM gateway implementations (Part 11.10).

The OpenAI adapter wraps `AsyncOpenAI` (or `AsyncAzureOpenAI` for the azure
provider) chat completions with `stream=True`. The provider stream is closed
as soon as the consumer stops iterating — disconnect, cancel, and timeout
all funnel through generator teardown. No retries happen after the first
chunk: a mid-stream provider failure surfaces as a stream error, never as
retried/duplicated text.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from investigation_agent_platform.domain.common.extension import (
    PluginKind,
    PluginManifest,
)
from investigation_agent_platform.infrastructure.configuration.config import LLMConfig
from investigation_agent_platform.ports.reasoning.streaming import StreamTextChunk

logger = logging.getLogger(__name__)

OPENAI_STREAM_PLUGIN_ID = "llm-streaming:openai"
STREAM_CONTRACT_VERSION = "1.0"


def streaming_manifest() -> PluginManifest:
    """Manifest for the OpenAI streaming adapter (registry wiring)."""
    return PluginManifest(
        plugin_id=OPENAI_STREAM_PLUGIN_ID,
        plugin_kind=PluginKind.LLM_STREAMING,
        contract_version=STREAM_CONTRACT_VERSION,
        implementation_version="1.0",
        config_schema_version="1.0",
        capabilities=frozenset({"stream"}),
        config_schema={"type": "object"},
    )


def count_text_tokens(model_name: str, text: str) -> int:
    """Best-effort token count for cost accounting (tiktoken, char fallback)."""
    try:
        import tiktoken  # type: ignore[import-not-found]

        try:
            encoding = tiktoken.encoding_for_model(model_name)
        except KeyError:
            encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))
    except Exception:
        return max(0, len(text) // 4)


class OpenAIStreamingGateway:
    """Streaming gateway over the OpenAI / Azure OpenAI chat API."""

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._client: Any | None = None

    @property
    def _is_azure(self) -> bool:
        return (self._config.provider or "openai").lower() == "azure"

    def _wire_model_name(self) -> str:
        if self._is_azure and self._config.azure_deployment:
            return self._config.azure_deployment
        return self._config.model_name

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import openai  # type: ignore[import-not-found]

                if self._is_azure:
                    endpoint = (self._config.azure_endpoint or "").rstrip("/")
                    if not endpoint or not self._config.azure_deployment:
                        raise RuntimeError(
                            "Azure streaming requires IAP_AZURE_OPENAI_ENDPOINT "
                            "and IAP_AZURE_OPENAI_DEPLOYMENT"
                        )
                    self._client = openai.AsyncAzureOpenAI(
                        api_key=self._config.api_key.get_secret_value(),
                        azure_endpoint=endpoint,
                        api_version=self._config.azure_api_version or "2024-02-01",
                    )
                else:
                    self._client = openai.AsyncOpenAI(
                        api_key=self._config.api_key.get_secret_value()
                    )
            except ImportError as exc:
                raise RuntimeError("openai package not installed") from exc
        return self._client

    async def stream(
        self,
        tenant_id: str,
        system_prompt: str,
        messages: list[dict[str, str]],
    ) -> AsyncIterator[StreamTextChunk]:
        """Yield text deltas; generator teardown closes the provider stream."""
        _ = tenant_id
        client: Any = self._get_client()
        wire_messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            *({"role": m["role"], "content": m["content"]} for m in messages),
        ]
        raw = await client.chat.completions.create(
            model=self._wire_model_name(),
            messages=wire_messages,
            temperature=self._config.temperature,
            max_tokens=self._config.max_tokens,
            stream=True,
        )
        emitted = False
        try:
            async for event in raw:
                for choice in event.choices or []:
                    delta = getattr(choice, "delta", None)
                    text = getattr(delta, "content", None) if delta is not None else None
                    if text:
                        emitted = True
                        yield StreamTextChunk(text=text)
                    finish = getattr(choice, "finish_reason", None)
                    if finish:
                        yield StreamTextChunk(done=True, finish_reason=str(finish))
                        return
        finally:
            # Generator teardown (consumer disconnect/cancel) closes the
            # underlying HTTP stream; nothing is retried after emission.
            close = getattr(raw, "close", None)
            if callable(close):
                try:
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
                except Exception as exc:
                    logger.warning("Streaming close failed", extra={"error": str(exc)})
            if not emitted:
                logger.warning("Streaming completed without emitting text")
