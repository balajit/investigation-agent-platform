# src/investigation_agent_platform/infrastructure/reasoning/openai_adapter.py
"""OpenAI implementation of LLMGateway."""

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

# Approximate per-token USD rates (per 1K tokens). Sorted for fallback.
_MODEL_RATES: dict[str, tuple[float, float]] = {
    "gpt-4o": (0.005, 0.015),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4-turbo": (0.01, 0.03),
    "gpt-3.5-turbo": (0.0005, 0.0015),
}


def _estimate_cost(model_name: str, prompt_tokens: int, completion_tokens: int) -> float:
    prompt_rate, completion_rate = _MODEL_RATES.get(model_name, (0.005, 0.015))
    return (prompt_tokens / 1000.0) * prompt_rate + (completion_tokens / 1000.0) * completion_rate


class OpenAIGateway:
    """OpenAI adapter — lazy client init so mypy/test does not require openai."""

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._client: object | None = None

    def _get_client(self) -> object:
        if self._client is None:
            try:
                import openai  # type: ignore[import-not-found]

                self._client = openai.AsyncOpenAI(api_key=self._config.api_key.get_secret_value())
            except ImportError as exc:
                raise RuntimeError("openai package not installed") from exc
        return self._client

    def _is_retryable_llm_error(self, exc: BaseException) -> bool:
        status = getattr(exc, "status_code", None)
        if status is None:
            status = getattr(exc, "http_status_code", None)
        if status is None:
            status = getattr(exc, "status", None)
        if status in (429, 503):
            return True
        # Fallback to retryable attribute but exclude auth errors (401, 403)
        if status in (401, 403):
            return False
        return bool(getattr(exc, "retryable", False))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(lambda e: getattr(e, "status_code", getattr(e, "http_status_code", None)) in (429, 503) or (getattr(e, "retryable", False) and getattr(e, "http_status_code", None) not in (401, 403))),
        reraise=True,
    )
    async def _call_with_retry(self, client: object, kwargs: dict[str, object]) -> object:
        return await asyncio.wait_for(
            client.chat.completions.create(**kwargs),  # type: ignore[attr-defined]
            timeout=30,
        )

    async def complete(self, tenant_id: str, request: LLMGatewayRequest) -> LLMGatewayResponse:
        start = time.perf_counter()
        client: object = self._get_client()

        # Use structured output if schema provided
        kwargs: dict[str, object] = {
            "model": self._config.model_name,
            "messages": [
                {"role": "system", "content": request.system_prompt or "You are an investigation assistant."},
                {"role": "user", "content": request.prompt},
            ],
            "temperature": request.temperature if request.temperature is not None else self._config.temperature,
            "max_tokens": request.max_tokens or self._config.max_tokens,
        }
        if request.response_schema is not None:
            # OpenAI structured output via json_object
            kwargs["response_format"] = {"type": "json_object"}

        # Dynamic dispatch — keep mypy happy with Any
        resp: object = await self._call_with_retry(client, kwargs)

        # Extract content + usage
        content: str = ""
        prompt_tokens = 0
        completion_tokens = 0
        try:
            choice = resp.choices[0]  # type: ignore[attr-defined]
            content = choice.message.content or ""
            usage = getattr(resp, "usage", None)
            if usage:
                prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        except Exception:
            logger.warning("Failed to parse OpenAI response", extra={"tenant_id": tenant_id})

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
