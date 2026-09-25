# src/investigation_agent_platform/infrastructure/reasoning/anthropic_adapter.py
"""Anthropic implementation of LLMGateway."""

import asyncio
import json
import logging
import time

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from investigation_agent_platform.infrastructure.configuration.config import LLMConfig
from investigation_agent_platform.ports.observability.telemetry import LLMCallMetadata
from investigation_agent_platform.ports.reasoning.llm_gateway import (
    LLMGatewayRequest,
    LLMGatewayResponse,
)

logger = logging.getLogger(__name__)

_MODEL_RATES: dict[str, tuple[float, float]] = {
    "claude-3-5-sonnet": (0.003, 0.015),
    "claude-3-opus": (0.015, 0.075),
    "claude-3-haiku": (0.00025, 0.00125),
    "claude-sonnet-4": (0.003, 0.015),
}


def _estimate_cost(model_name: str, prompt_tokens: int, completion_tokens: int) -> float:
    prompt_rate, completion_rate = _MODEL_RATES.get(model_name, (0.003, 0.015))
    return (prompt_tokens / 1000.0) * prompt_rate + (completion_tokens / 1000.0) * completion_rate


class AnthropicGateway:
    """Anthropic adapter — lazy client init."""

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._client: object | None = None

    def _get_client(self) -> object:
        if self._client is None:
            try:
                import anthropic  # type: ignore[import-not-found]

                self._client = anthropic.AsyncAnthropic(api_key=self._config.api_key.get_secret_value())
            except ImportError as exc:
                raise RuntimeError("anthropic package not installed") from exc
        return self._client

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(lambda e: getattr(e, "status_code", getattr(e, "http_status_code", None)) in (429, 503) or (getattr(e, "retryable", False) and getattr(e, "http_status_code", None) not in (401, 403))),
        reraise=True,
    )
    async def _call_with_retry(self, client: object, kwargs: dict[str, object]) -> object:
        return await asyncio.wait_for(
            client.messages.create(**kwargs),  # type: ignore[attr-defined]
            timeout=30,
        )

    async def complete(self, tenant_id: str, request: LLMGatewayRequest) -> LLMGatewayResponse:
        start = time.perf_counter()
        client: object = self._get_client()

        kwargs: dict[str, object] = {
            "model": self._config.model_name,
            "max_tokens": request.max_tokens or self._config.max_tokens,
            "temperature": request.temperature if request.temperature is not None else self._config.temperature,
            "messages": [{"role": "user", "content": request.prompt}],
        }
        if request.system_prompt:
            kwargs["system"] = request.system_prompt

        resp: object = await self._call_with_retry(client, kwargs)

        content: str = ""
        prompt_tokens = 0
        completion_tokens = 0
        try:
            blocks = getattr(resp, "content", [])
            if blocks and hasattr(blocks[0], "text"):
                content = blocks[0].text
            usage = getattr(resp, "usage", None)
            if usage:
                prompt_tokens = getattr(usage, "input_tokens", 0) or 0
                completion_tokens = getattr(usage, "output_tokens", 0) or 0
        except Exception:
            logger.warning("Failed to parse Anthropic response", extra={"tenant_id": tenant_id})

        parsed: dict[str, object] | None = None
        if request.response_schema is not None and content:
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                pass

        latency_ms = (time.perf_counter() - start) * 1000
        estimated_cost = _estimate_cost(self._config.model_name, prompt_tokens, completion_tokens)
        metadata = LLMCallMetadata(
            model_name=self._config.model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            estimated_cost_usd=estimated_cost,
            latency_ms=latency_ms,
        )
        return LLMGatewayResponse(content=content, parsed=parsed, metadata=metadata)
