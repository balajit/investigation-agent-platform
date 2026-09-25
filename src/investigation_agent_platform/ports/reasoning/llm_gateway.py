# src/investigation_agent_platform/ports/reasoning/llm_gateway.py
"""Provider-agnostic LLM gateway port."""

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from investigation_agent_platform.ports.observability.telemetry import LLMCallMetadata


class LLMGatewayRequest(BaseModel):
    """Normalized LLM completion request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str
    system_prompt: str | None = None
    response_schema: dict[str, Any] | None = None
    max_tokens: int | None = None
    temperature: float | None = None


class LLMGatewayResponse(BaseModel):
    """Normalized LLM completion response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str
    parsed: dict[str, Any] | None = None
    metadata: LLMCallMetadata


@runtime_checkable
class LLMGateway(Protocol):
    """Provider-agnostic gateway (OpenAI | Anthropic)."""

    async def complete(
        self, tenant_id: str, request: LLMGatewayRequest
    ) -> LLMGatewayResponse:
        ...
