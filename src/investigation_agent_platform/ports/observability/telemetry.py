# src/investigation_agent_platform/ports/observability/telemetry.py
"""Structured tracing and telemetry port protocol for agentic observability."""

from contextlib import AbstractContextManager
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SpanContext(BaseModel):
    """Execution context binding span to trace hierarchy and tenant boundaries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    investigation_id: UUID
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None


class LLMCallMetadata(BaseModel):
    """Telemetry data capturing LLM model invocation details."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_name: str
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    estimated_cost_usd: float = Field(ge=0.0)
    latency_ms: float = Field(ge=0.0)
    # F-029: pricing table version that produced estimated_cost_usd — estimates
    # are telemetry only, never hard spend limits.
    pricing_version: str = Field(default="2026-09-01")


class ToolExecutionMetadata(BaseModel):
    """Telemetry data capturing tool invocation metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str
    inputs: dict[str, Any]
    outputs: dict[str, Any] | None = None
    execution_time_ms: float = Field(ge=0.0)
    success: bool
    error_message: str | None = None


@runtime_checkable
class ObservabilityPort(Protocol):
    """Port for structured tracing, agentic LLM/tool instrumentation, and metric emission."""

    def start_span(
        self, name: str, context: SpanContext, attributes: dict[str, Any] | None = None
    ) -> AbstractContextManager[Any]: ...

    def record_llm_call(self, context: SpanContext, metadata: LLMCallMetadata) -> None: ...

    def record_tool_call(self, context: SpanContext, metadata: ToolExecutionMetadata) -> None: ...

    def record_evidence_retrieval(
        self,
        context: SpanContext,
        provider_type: str,
        query_type: str,
        result_count: int,
        duration_ms: float,
    ) -> None: ...

    def record_metric(self, metric_name: str, value: float, tags: dict[str, str]) -> None: ...
