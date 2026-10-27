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

    # Phase 4.2: code-grounding proof-chain counters. Label vocabularies are
    # closed by construction (action names, outcome statuses, miss reasons,
    # claim kinds, limitation codes, booleans) — high-cardinality values
    # (file paths, line ranges, revision SHAs) never appear as labels; they
    # belong in structured event fields. All methods are fire-and-forget:
    # adapters must never raise into an investigation.

    def record_code_action(
        self, context: SpanContext, action: str, outcome: str, miss_reason: str
    ) -> None: ...

    def record_symbol_empty(self, context: SpanContext, tool: str) -> None: ...

    def record_verification(
        self, context: SpanContext, claim_kind: str, status: str, limitation: str
    ) -> None: ...

    def record_conclusion_limitation(self, context: SpanContext, kind: str) -> None: ...

    def record_replay_mismatch(self, context: SpanContext) -> None: ...

    def record_code_packets(
        self, context: SpanContext, included: int, truncated: int, dropped: int
    ) -> None: ...

    def record_location_answer(self, context: SpanContext, has_untested_rival: bool) -> None: ...

    def record_revision_check(self, context: SpanContext, operation: str, match: bool) -> None: ...

    def record_claim(self, context: SpanContext, source: str) -> None: ...

    # Phase 1 (claim binding): closed-vocabulary counters. Label sets are
    # frozen — claim kinds (DEFINITION|CALL_EDGE|BEHAVIOR|ABSENCE|HISTORY|
    # CAUSAL|unspecified), partition dispositions (claim_local|
    # bank_fallback|evaporated|unmapped|no_citations), binding versions
    # (cbv1|cbv2|cbv3), subject outcomes (matched_exact|matched_token_set|
    # mismatched_subject|missing_subject|missing_row), newest-rule states
    # (satisfied|violated|not_applicable), version sources (pinned|
    # unmarked). Phase 3 adds repair bases (snippet_containment|
    # overlap_and_containment) and repair outcomes (applied|shadow).
    # High-cardinality values (paths, ranges, SHAs, subjects,
    # statements) never appear as labels. All methods are fire-and-forget:
    # adapters must never raise into an investigation.

    def record_claim_binding(
        self, context: SpanContext, claim_kind: str, disposition: str, binding_version: str
    ) -> None: ...

    def record_claim_repair(
        self,
        context: SpanContext,
        claim_kind: str,
        basis: str,
        outcome: str,
        binding_version: str,
    ) -> None: ...

    def record_history_subject(
        self, context: SpanContext, outcome: str, newest_rule: str
    ) -> None: ...

    def record_binding_version(self, context: SpanContext, version: str, source: str) -> None: ...
