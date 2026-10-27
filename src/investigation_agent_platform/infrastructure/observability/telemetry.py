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
        # Phase 4.2: code-grounding proof-chain counters. All labels are
        # closed vocabularies — never file paths, line ranges, or SHAs.
        self._code_action_counter = self._meter.create_counter(
            "code.action.executions", unit="1", description="Code actions by outcome"
        )
        self._symbol_empty_counter = self._meter.create_counter(
            "code.symbol.empty", unit="1", description="Symbol lookups banking nothing"
        )
        self._verification_counter = self._meter.create_counter(
            "verification.decisions", unit="1", description="Verification by kind/status"
        )
        self._conclusion_limitation_counter = self._meter.create_counter(
            "conclusion.limitations", unit="1", description="Conclusion limitations by kind"
        )
        self._replay_mismatch_counter = self._meter.create_counter(
            "replay.outcome.mismatch",
            unit="1",
            description="Idempotency replays with unknown outcome shape",
        )
        self._code_packet_counter = self._meter.create_counter(
            "prompt.code_packets", unit="1", description="Code packets by disposition"
        )
        self._location_answer_counter = self._meter.create_counter(
            "verification.location_answers",
            unit="1",
            description="Location answers by rival state",
        )
        self._revision_check_counter = self._meter.create_counter(
            "code.revision.checks", unit="1", description="Revision pin checks by match"
        )
        self._claim_counter = self._meter.create_counter(
            "reasoning.claims", unit="1", description="Decision claims by source"
        )
        # Phase 1 (claim binding): closed-vocabulary counters. Labels are
        # coerced to the frozen sets below — never file paths, SHAs,
        # subjects, or statements.
        self._claim_binding_counter = self._meter.create_counter(
            "claim.binding.decisions",
            unit="1",
            description="Per-claim binding dispositions by kind",
        )
        self._history_subject_counter = self._meter.create_counter(
            "history.subject.checks",
            unit="1",
            description="HISTORY subject comparisons by outcome",
        )
        self._binding_version_counter = self._meter.create_counter(
            "claim.binding.version",
            unit="1",
            description="Claim binding version resolutions by source",
        )
        # Phase 3 (claim binding): closed-vocabulary repair counter.
        self._claim_repair_counter = self._meter.create_counter(
            "claim.binding.repairs",
            unit="1",
            description="Misattribution repairs by kind, basis, and outcome",
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

    @staticmethod
    def _str(value: Any) -> str:
        text = str(value or "").strip()
        return text[:128] if text else "none"

    def record_code_action(
        self, context: SpanContext, action: str, outcome: str, miss_reason: str
    ) -> None:
        if not self._enabled:
            return
        self._code_action_counter.add(
            1,
            {
                "action": self._str(action) or "unknown",
                "outcome": self._str(outcome) or "unknown",
                "miss_reason": self._str(miss_reason),
            },
        )

    def record_symbol_empty(self, context: SpanContext, tool: str) -> None:
        if not self._enabled:
            return
        self._symbol_empty_counter.add(1, {"tool": self._str(tool) or "unknown"})

    def record_verification(
        self, context: SpanContext, claim_kind: str, status: str, limitation: str
    ) -> None:
        if not self._enabled:
            return
        self._verification_counter.add(
            1,
            {
                "claim_kind": self._str(claim_kind),
                "status": self._str(status) or "unknown",
                "limitation": self._str(limitation),
            },
        )

    def record_conclusion_limitation(self, context: SpanContext, kind: str) -> None:
        if not self._enabled:
            return
        self._conclusion_limitation_counter.add(1, {"kind": self._str(kind)})

    def record_replay_mismatch(self, context: SpanContext) -> None:
        if not self._enabled:
            return
        self._replay_mismatch_counter.add(1, {})

    def record_code_packets(
        self, context: SpanContext, included: int, truncated: int, dropped: int
    ) -> None:
        if not self._enabled:
            return
        for disposition, count in (
            ("included", included),
            ("truncated", truncated),
            ("dropped", dropped),
        ):
            try:
                total = int(count or 0)
            except (TypeError, ValueError):
                continue
            if total > 0:
                self._code_packet_counter.add(total, {"disposition": disposition})

    def record_location_answer(self, context: SpanContext, has_untested_rival: bool) -> None:
        if not self._enabled:
            return
        self._location_answer_counter.add(
            1, {"has_untested_rival": "true" if has_untested_rival else "false"}
        )

    def record_revision_check(self, context: SpanContext, operation: str, match: bool) -> None:
        if not self._enabled:
            return
        self._revision_check_counter.add(
            1,
            {
                "operation": self._str(operation) or "unknown",
                "match": "true" if match else "false",
            },
        )

    def record_claim(self, context: SpanContext, source: str) -> None:
        if not self._enabled:
            return
        if source not in ("model", "auto"):
            return
        self._claim_counter.add(1, {"source": source})

    _CLAIM_KINDS = frozenset(
        {
            "DEFINITION",
            "CALL_EDGE",
            "BEHAVIOR",
            "ABSENCE",
            "HISTORY",
            "CAUSAL",
            "UNTYPED",
            "unspecified",
        }
    )
    _BINDING_DISPOSITIONS = frozenset(
        {"claim_local", "bank_fallback", "evaporated", "unmapped", "no_citations"}
    )
    _BINDING_VERSIONS = frozenset({"cbv1", "cbv2", "cbv3"})
    _REPAIR_BASES = frozenset({"snippet_containment", "overlap_and_containment"})
    _REPAIR_OUTCOMES = frozenset({"applied", "shadow"})
    _SUBJECT_OUTCOMES = frozenset(
        {
            "matched_exact",
            "matched_token_set",
            "mismatched_subject",
            "missing_subject",
            "missing_row",
        }
    )
    _NEWEST_RULES = frozenset({"satisfied", "violated", "not_applicable"})
    _VERSION_SOURCES = frozenset({"pinned", "unmarked"})

    @staticmethod
    def _closed(value: Any, allowed: frozenset[str], fallback: str) -> str:
        text = str(value or "").strip()
        return text if text in allowed else fallback

    def record_claim_binding(
        self, context: SpanContext, claim_kind: str, disposition: str, binding_version: str
    ) -> None:
        if not self._enabled:
            return
        self._claim_binding_counter.add(
            1,
            {
                "claim_kind": self._closed(claim_kind, self._CLAIM_KINDS, "unspecified"),
                "disposition": self._closed(
                    disposition, self._BINDING_DISPOSITIONS, "no_citations"
                ),
                "binding_version": self._closed(binding_version, self._BINDING_VERSIONS, "cbv1"),
            },
        )

    def record_history_subject(self, context: SpanContext, outcome: str, newest_rule: str) -> None:
        if not self._enabled:
            return
        self._history_subject_counter.add(
            1,
            {
                "outcome": self._closed(outcome, self._SUBJECT_OUTCOMES, "mismatched_subject"),
                "newest_rule": self._closed(newest_rule, self._NEWEST_RULES, "not_applicable"),
            },
        )

    def record_binding_version(self, context: SpanContext, version: str, source: str) -> None:
        if not self._enabled:
            return
        self._binding_version_counter.add(
            1,
            {
                "version": self._closed(version, self._BINDING_VERSIONS, "cbv1"),
                "source": self._closed(source, self._VERSION_SOURCES, "unmarked"),
            },
        )

    def record_claim_repair(
        self,
        context: SpanContext,
        claim_kind: str,
        basis: str,
        outcome: str,
        binding_version: str,
    ) -> None:
        if not self._enabled:
            return
        self._claim_repair_counter.add(
            1,
            {
                "claim_kind": self._closed(claim_kind, self._CLAIM_KINDS, "unspecified"),
                "basis": self._closed(basis, self._REPAIR_BASES, "snippet_containment"),
                "outcome": self._closed(outcome, self._REPAIR_OUTCOMES, "shadow"),
                "binding_version": self._closed(binding_version, self._BINDING_VERSIONS, "cbv1"),
            },
        )


__all__ = ["ObservabilityPort", "OpenTelemetryObservabilityAdapter"]
