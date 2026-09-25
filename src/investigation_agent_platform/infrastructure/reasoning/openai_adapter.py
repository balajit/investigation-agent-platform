# src/investigation_agent_platform/infrastructure/reasoning/openai_adapter.py
"""OpenAI implementation of LLMGateway."""

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
    # Back-compat shim — new code should use pricing.estimate_cost directly to
    # also capture the pricing version. Kept for existing callers/tests.
    cost, _ = _estimate_cost_versioned(model_name, prompt_tokens, completion_tokens)
    return cost


def _is_unsupported_format_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "response_format" in message or "json_schema" in message


# Data classifications that must never leave the platform boundary toward an
# external model provider without an explicit per-call authorization reference.
_RESTRICTED_CLASSIFICATIONS = frozenset({"RESTRICTED", "SECRET", "TOP_SECRET"})


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

    def _enforce_envelope_policy(self, tenant_id: str, request: LLMGatewayRequest) -> None:
        """Refuse provider calls whose data-policy envelope forbids them (F-028)."""
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
        provider = (self._config.provider or "openai").lower()
        if envelope.allowed_provider and envelope.allowed_provider.lower() not in (provider, "any"):
            raise SecurityPolicyViolationException(
                f"Envelope permits provider '{envelope.allowed_provider}' but gateway is '{provider}'",
                details={"allowed_provider": envelope.allowed_provider},
            )

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
        retry=retry_if_exception(
            lambda e: (
                getattr(e, "status_code", getattr(e, "http_status_code", None)) in (429, 503)
                or (
                    getattr(e, "retryable", False)
                    and getattr(e, "http_status_code", None) not in (401, 403)
                )
            )
        ),
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

        # F-028: enforce the prompt data-policy boundary before any provider call.
        if request.envelope is not None:
            self._enforce_envelope_policy(tenant_id, request)
        else:
            logger.warning(
                "LLM call without classified prompt envelope; proceeding with default INTERNAL policy",
                extra={"tenant_id": tenant_id},
            )

        # Use structured output if schema provided
        kwargs: dict[str, object] = {
            "model": self._config.model_name,
            "messages": [
                {
                    "role": "system",
                    "content": request.system_prompt or "You are an investigation assistant.",
                },
                {"role": "user", "content": request.prompt},
            ],
            "temperature": request.temperature
            if request.temperature is not None
            else self._config.temperature,
            "max_tokens": request.max_tokens or self._config.max_tokens,
        }
        if request.response_schema is not None:
            # F-027: enforce the supplied JSON schema at the provider layer via
            # strict structured outputs — not a generic json_object mode with
            # only post-hoc Pydantic validation. Falls back to json_object only
            # for API versions that reject the json_schema response format.
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "investigation_decision",
                    "strict": True,
                    "schema": request.response_schema,
                },
            }

        # Dynamic dispatch — keep mypy happy with Any
        try:
            resp: object = await self._call_with_retry(client, kwargs)
        except Exception as exc:
            # Older API versions reject json_schema response_format: retry once
            # with generic json_object mode rather than failing the call, but
            # keep strict Pydantic validation downstream as the second boundary.
            if request.response_schema is not None and _is_unsupported_format_error(exc):
                logger.warning(
                    "Provider rejected strict json_schema mode; falling back to json_object",
                    extra={"tenant_id": tenant_id},
                )
                kwargs["response_format"] = {"type": "json_object"}
                resp = await self._call_with_retry(client, kwargs)
            else:
                raise

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
