# src/investigation_agent_platform/infrastructure/observability/telemetry.py
"""Infrastructure telemetry implementation powering OpenTelemetry instrumentation."""

import logging
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from investigation_agent_platform.ports.observability.telemetry import (
    LLMCallMetadata,
    ObservabilityPort,
    SpanContext,
    ToolExecutionMetadata,
)

logger = logging.getLogger(__name__)


class OpenTelemetryObservabilityAdapter(ObservabilityPort):
    """Production OpenTelemetry instrumentation adapter implementing the outbound ObservabilityPort."""

    def __init__(self, service_name: str, otlp_endpoint: str, enabled: bool = True) -> None:
        self._service_name = service_name
        self._enabled = enabled

        if self._enabled:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

                resource = Resource.create(attributes={"service.name": service_name})
                provider = TracerProvider(resource=resource)
                processor = BatchSpanProcessor(
                    OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
                )
                provider.add_span_processor(processor)
                trace.set_tracer_provider(provider)
            except ImportError:
                logger.warning("OTLP exporter not installed; tracing disabled")

        self._tracer = trace.get_tracer(service_name)
        self._meter = metrics.get_meter(service_name)
        self._tool_counter = self._meter.create_counter(
            "agent.tool.executions", unit="1", description="Count of agent tool calls"
        )
        self._tool_histogram = self._meter.create_histogram(
            "agent.tool.duration", unit="ms", description="Latency of tool calls"
        )
        self._llm_token_counter = self._meter.create_counter(
            "agent.llm.tokens", unit="1", description="LLM token usage"
        )
        self._evidence_counter = self._meter.create_counter(
            "evidence.retrieval", unit="1", description="Evidence retrieval count"
        )
        self._llm_cost_histogram = self._meter.create_histogram(
            "agent.llm.cost", unit="usd", description="Estimated LLM cost"
        )

    def record_tool_execution(
        self,
        tool_name: str,
        duration_ms: float,
        success: bool,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        if not self._enabled:
            return
        attrs: dict[str, Any] = {
            "tool.name": tool_name,
            "tool.success": success,
            **(attributes or {}),
        }
        self._tool_counter.add(1, attrs)
        self._tool_histogram.record(duration_ms, attrs)

    def record_llm_call(self, context: SpanContext, metadata: LLMCallMetadata) -> None:
        if not self._enabled:
            return
        attrs = {"llm.model": metadata.model_name}
        self._llm_token_counter.add(metadata.prompt_tokens, {**attrs, "token.type": "prompt"})
        self._llm_token_counter.add(
            metadata.completion_tokens, {**attrs, "token.type": "completion"}
        )
        if metadata.estimated_cost_usd:
            self._llm_cost_histogram.record(metadata.estimated_cost_usd, attrs)

    # Compatibility shims for older callers
    def record_tool_call(self, context: SpanContext, metadata: ToolExecutionMetadata) -> None:
        self.record_tool_execution(metadata.tool_name, metadata.execution_time_ms, metadata.success)

    def start_span(
        self, name: str, context: SpanContext, attributes: dict[str, Any] | None = None
    ) -> Any:
        return self._tracer.start_as_current_span(name)

    def record_evidence_retrieval(
        self,
        context: SpanContext,
        provider_type: str,
        query_type: str,
        result_count: int,
        duration_ms: float,
    ) -> None:
        if not self._enabled:
            return
        self._evidence_counter.add(
            result_count, {"provider": provider_type, "query_type": query_type}
        )

    def record_metric(self, metric_name: str, value: float, tags: dict[str, str]) -> None:
        if not self._enabled:
            return
        self._meter.create_counter(metric_name, unit="1").add(value, tags)


__all__ = ["ObservabilityPort", "OpenTelemetryObservabilityAdapter"]
