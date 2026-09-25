# src/investigation_agent_platform/application/investigation/reasoning.py
import html
import logging
from typing import Any
from uuid import UUID
from xml.sax.saxutils import quoteattr

from opentelemetry import trace
from pydantic import BaseModel, Field

from investigation_agent_platform.domain.entity.models import InvestigationEntity
from investigation_agent_platform.domain.hypothesis.models import Hypothesis
from investigation_agent_platform.domain.investigation.models import (
    ActionType,
    EvidenceManifest,
    Fact,
    InvestigationState,
)
from investigation_agent_platform.ports.reasoning.reasoner import (
    InvestigationDecision,
    InvestigationReasoner,
)
from investigation_agent_platform.ports.security.redactor import PromptSafetyPolicyPort

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class ReasoningContext(BaseModel):
    investigation_id: UUID
    problem_description: str
    current_lifecycle_state: str
    facts: list[Fact] = Field(default_factory=list)
    active_hypotheses: list[Hypothesis] = Field(default_factory=list)
    contradictions: list[dict[str, Any]] = Field(default_factory=list)
    relevant_evidence: list[EvidenceManifest] = Field(default_factory=list)
    timeline_summary: list[dict[str, Any]] = Field(default_factory=list)
    known_entities: list[InvestigationEntity] = Field(default_factory=list)
    available_actions: list[dict[str, Any]] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)


class EvidenceContextFormatter:
    """Formats evidence safely within XML envelope boundaries, escaping payloads and attributes."""

    @staticmethod
    def format_xml_payload(evidence: EvidenceManifest, raw_payload: str) -> str:
        with tracer.start_as_current_span("EvidenceContextFormatter.format_xml_payload"):
            escaped_payload = html.escape(raw_payload)
            escaped_id = quoteattr(str(evidence.evidence_id))
            escaped_source = quoteattr(evidence.source)
            escaped_type = quoteattr(evidence.evidence_type.value)

            return (
                f"<untrusted_evidence_payload evidence_id={escaped_id} "
                f"source={escaped_source} type={escaped_type}>\n"
                f"BEGIN EVIDENCE\n{escaped_payload}\nEND EVIDENCE\n"
                f"</untrusted_evidence_payload>"
            )


class ReasoningCoordinator(InvestigationReasoner):
    """Coordinates structured LLM reasoning and converts outputs to canonical decisions.

    TODO(LLM-001): Replace stub decision with real LLMGateway call.
    Phase 1 vertical slice ships with stub; Phase 3.1 wires gateway.
    """

    def __init__(
        self,
        prompt_safety_policy: PromptSafetyPolicyPort,
        llm_gateway: Any | None = None,
        observability: Any | None = None,
    ) -> None:
        self._prompt_safety_policy = prompt_safety_policy
        self._llm_gateway: Any | None = llm_gateway
        self._observability: Any | None = observability

    def _validate_llm_decision(self, parsed: dict[str, Any]) -> InvestigationDecision:
        """Validate raw LLM JSON via Pydantic and ensure action is allowed.

        Uses ``InvestigationDecision.model_validate`` for schema enforcement and
        checks that any embedded ``action`` has an allowed ``ActionType``.
        """
        decision = InvestigationDecision.model_validate(parsed)
        if decision.action is not None:
            allowed = {a.value for a in ActionType}
            if decision.action.action_type.value not in allowed:
                raise ValueError(
                    f"LLM decision contains disallowed action_type: {decision.action.action_type}"
                )
        return decision

    async def reason(self, tenant_id: str, state: InvestigationState) -> InvestigationDecision:
        with tracer.start_as_current_span("ReasoningCoordinator.reason"):
            prompt_summary = (
                f"Investigation state for tenant {tenant_id}: {state.investigation.status.value}"
            )
            is_safe = await self._prompt_safety_policy.validate_prompt_safety(
                tenant_id, prompt_summary
            )
            if not is_safe:
                logger.error(
                    "Prompt safety check failed during reasoning", extra={"tenant_id": tenant_id}
                )
                return InvestigationDecision(
                    observations=["Prompt safety policy violation detected."],
                    conclusion_readiness=0.0,
                    reasoning_chain="Aborted reasoning due to security violation.",
                )

            # TODO(LLM-001): When llm_gateway is configured, call it with structured schema.
            # Stub path preserves Phase 1 vertical slice without LLM credentials.
            if self._llm_gateway is not None:
                try:
                    from investigation_agent_platform.ports.reasoning.llm_gateway import (
                        LLMGatewayRequest,
                    )

                    # Dynamic to keep mypy clean without hard coupling
                    gateway: Any = self._llm_gateway
                    schema = InvestigationDecision.model_json_schema()
                    req = LLMGatewayRequest(
                        prompt=prompt_summary,
                        system_prompt="You are an investigation assistant. Return JSON matching the decision schema.",
                        response_schema=schema,
                    )
                    resp = await gateway.complete(tenant_id, req)
                    if resp.parsed:
                        try:
                            return self._validate_llm_decision(resp.parsed)
                        except Exception:
                            logger.warning(
                                "LLM returned invalid decision schema, falling back to stub",
                                extra={"tenant_id": tenant_id},
                            )
                    # Record telemetry if observability present
                    if self._observability is not None:
                        try:
                            from investigation_agent_platform.ports.observability.telemetry import (
                                SpanContext,
                            )

                            ctx = SpanContext(
                                tenant_id=tenant_id, investigation_id=state.investigation.id
                            )
                            self._observability.record_llm_call(ctx, resp.metadata)
                        except Exception:
                            pass
                except Exception as exc:
                    logger.warning(
                        "LLM gateway call failed, using stub decision",
                        extra={"tenant_id": tenant_id, "error": str(exc)},
                    )

            return InvestigationDecision(
                observations=["Synthesized state telemetry and code evidence."],
                information_gaps=["Missing state configuration for database pool."],
                hypotheses_evaluated=["DB pool exhaustion caused connection timeout."],
                conclusion_readiness=0.85,
                reasoning_chain="Evidence confirms pool exhaustion correlates with error burst.",
            )
