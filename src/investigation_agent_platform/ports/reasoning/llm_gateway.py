# src/investigation_agent_platform/ports/reasoning/llm_gateway.py
"""Provider-agnostic LLM gateway port."""

from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.ports.observability.telemetry import LLMCallMetadata


class PromptDataEnvelope(BaseModel):
    """Classified prompt envelope (F-028): every LLM call carries its data
    policy boundary — tenant, investigation, referenced evidence IDs, data
    classification, permitted provider, and retention policy — so the gateway
    can refuse to send disallowed data classes to a model/provider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(..., max_length=128)
    investigation_id: UUID
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=500)
    classification: str = Field(default="INTERNAL", max_length=32)
    allowed_provider: str = Field(default="openai", max_length=32)
    retention_days: int = Field(default=30, ge=0, le=3650)
    authorization_reference: str = Field(default="", max_length=256)


class LLMGatewayRequest(BaseModel):
    """Normalized LLM completion request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str
    system_prompt: str | None = None
    response_schema: dict[str, Any] | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    # F-028: optional but enforced when present — adapters must refuse to
    # complete the call if the envelope's classification/provider policy
    # forbids it.
    envelope: PromptDataEnvelope | None = None


class LLMGatewayResponse(BaseModel):
    """Normalized LLM completion response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str
    parsed: dict[str, Any] | None = None
    metadata: LLMCallMetadata


@runtime_checkable
class LLMGateway(Protocol):
    """Provider-agnostic gateway (OpenAI | Anthropic | Azure OpenAI)."""

    async def complete(self, tenant_id: str, request: LLMGatewayRequest) -> LLMGatewayResponse: ...
