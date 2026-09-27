# src/investigation_agent_platform/infrastructure/reasoning/anthropic_adapter.py
"""Anthropic implementation of LLMGateway."""

import asyncio
import json
import logging
import time

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from investigation_agent_platform.infrastructure.configuration.config import LLMConfig
from investigation_agent_platform.infrastructure.reasoning.pricing import (
    estimate_cost as _estimate_cost_versioned,
)
from investigation_agent_platform.ports.observability.telemetry import LLMCallMetadata
from investigation_agent_platform.ports.reasoning.llm_gateway import (
    LLMGateway,
    LLMGatewayRequest,
    LLMGatewayResponse,
)

logger = logging.getLogger(__name__)


def _estimate_cost(model_name: str, prompt_tokens: int, completion_tokens: int) -> float:
    cost, _ = _estimate_cost_versioned(model_name, prompt_tokens, completion_tokens)
    return cost


_RESTRICTED_CLASSIFICATIONS = frozenset({"RESTRICTED", "SECRET", "TOP_SECRET"})


def _is_retryable_llm_error(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "http_status_code", None)
    if status is None:
        status = getattr(exc, "status", None)
    if status in (429, 503):
        return True
    if status in (401, 403):
        return False
    return bool(getattr(exc, "retryable", False))


def _enforce_envelope_policy(tenant_id: str, request: LLMGatewayRequest, provider: str) -> None:
    """Shared F-028 envelope boundary check for non-OpenAI adapters."""
    from investigation_agent_platform.domain.common.exceptions import (
        SecurityPolicyViolationException,
    )

    envelope = request.envelope
    assert envelope is not None
    if envelope.tenant_id != tenant_id:
        raise SecurityPolicyViolationException(
            "Prompt envelope tenant does not match request tenant",
            details={"tenant_id": tenant_id},
        )
    if (
        envelope.classification.upper() in _RESTRICTED_CLASSIFICATIONS
        and not envelope.authorization_reference
    ):
        raise SecurityPolicyViolationException(
            f"Data classification '{envelope.classification}' requires an explicit "
            "authorization reference before it may be sent to a model provider",
            details={"classification": envelope.classification},
        )
    if envelope.allowed_provider and envelope.allowed_provider.lower() not in (provider, "any"):
        raise SecurityPolicyViolationException(
            f"Envelope permits provider '{envelope.allowed_provider}' but gateway is '{provider}'",
            details={"allowed_provider": envelope.allowed_provider},
        )


class AnthropicGateway(LLMGateway):
    """Anthropic adapter — lazy client init."""

    def __init__(self, config: LLMConfig) -> None:
        self._config = config
        self._client: object | None = None

    def _get_client(self) -> object:
        if self._client is None:
            try:
                import anthropic  # type: ignore[import-not-found]

                self._client = anthropic.AsyncAnthropic(
                    api_key=self._config.api_key.get_secret_value()
                )
            except ImportError as exc:
                raise RuntimeError("anthropic package not installed") from exc
        return self._client

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(_is_retryable_llm_error),
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

        # F-028: same envelope policy boundary as the OpenAI adapter.
        if request.envelope is not None:
            _enforce_envelope_policy(tenant_id, request, provider="anthropic")
        else:
            logger.warning(
                "LLM call without classified prompt envelope; proceeding with default INTERNAL policy",
                extra={"tenant_id": tenant_id},
            )

        max_tokens = request.max_tokens or self._config.max_tokens or 4096
        kwargs: dict[str, object] = {
            "model": self._config.model_name,
            "max_tokens": max_tokens,
            "temperature": request.temperature
            if request.temperature is not None
            else self._config.temperature,
            "messages": [{"role": "user", "content": request.prompt}],
        }
        if request.system_prompt:
            kwargs["system"] = request.system_prompt
        if request.response_schema is not None:
            # F-027: enforce the schema at the provider layer via forced tool
            # use — the model must respond through the schema-bound tool rather
            # than free text parsed post-hoc.
            kwargs["tools"] = [
                {
                    "name": "investigation_decision",
                    "description": "Return the structured investigation decision.",
                    "input_schema": request.response_schema,
                }
            ]
            kwargs["tool_choice"] = {"type": "tool", "name": "investigation_decision"}

        resp: object = await self._call_with_retry(client, kwargs)

        content: str = ""
        prompt_tokens = 0
        completion_tokens = 0
        tool_input: dict[str, object] | None = None

        # Extract usage telemetry safely
        try:
            usage = getattr(resp, "usage", None)
            if usage:
                prompt_tokens = getattr(usage, "input_tokens", 0) or 0
                completion_tokens = getattr(usage, "output_tokens", 0) or 0
        except Exception:
            logger.warning(
                "Failed to parse Anthropic usage telemetry", extra={"tenant_id": tenant_id}
            )

        # Extract content & tool outputs safely
        try:
            blocks = getattr(resp, "content", [])
            for block in blocks:
                if getattr(block, "type", None) == "tool_use" and hasattr(block, "input"):
                    tool_input = dict(block.input) if isinstance(block.input, dict) else block.input
                    content = json.dumps(block.input)
                    break
            else:
                if blocks and hasattr(blocks[0], "text"):
                    content = blocks[0].text
        except Exception:
            logger.warning(
                "Failed to parse Anthropic response content", extra={"tenant_id": tenant_id}
            )

        parsed: dict[str, object] | None = None
        if request.response_schema is not None and content:
            if tool_input is not None:
                parsed = tool_input
            else:
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError:
                    pass

        latency_ms = (time.perf_counter() - start) * 1000
        estimated_cost, pricing_version = _estimate_cost_versioned(
            self._config.model_name, prompt_tokens, completion_tokens
        )
        metadata = LLMCallMetadata(
            model_name=self._config.model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            estimated_cost_usd=estimated_cost,
            latency_ms=latency_ms,
            pricing_version=pricing_version,
        )
        return LLMGatewayResponse(content=content, parsed=parsed, metadata=metadata)
