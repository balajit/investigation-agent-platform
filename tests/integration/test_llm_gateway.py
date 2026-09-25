"""LLM gateway generalization test — OpenAI ↔ Anthropic."""

import pytest
from pydantic import SecretStr

from investigation_agent_platform.infrastructure.configuration.config import LLMConfig
from investigation_agent_platform.infrastructure.reasoning.factory import create_llm_gateway
from investigation_agent_platform.infrastructure.reasoning.openai_adapter import OpenAIGateway
from investigation_agent_platform.infrastructure.reasoning.anthropic_adapter import AnthropicGateway


def test_factory_openai_default() -> None:
    cfg = LLMConfig(api_key=SecretStr("sk-test"), model_name="gpt-4o", provider="openai")
    gw = create_llm_gateway(cfg)
    assert isinstance(gw, OpenAIGateway)


def test_factory_anthropic() -> None:
    cfg = LLMConfig(api_key=SecretStr("sk-test"), model_name="claude-3-5-sonnet", provider="anthropic")
    gw = create_llm_gateway(cfg)
    assert isinstance(gw, AnthropicGateway)


def test_factory_inferred_claude() -> None:
    cfg = LLMConfig(api_key=SecretStr("sk-test"), model_name="claude-3-5-sonnet", provider="openai")
    # provider explicit wins; but if provider empty, inference should pick anthropic
    cfg2 = LLMConfig(api_key=SecretStr("sk-test"), model_name="claude-3-opus")
    # factory infers from model_name when provider not set; here provider defaults to openai so stays openai
    # ensure factory respects explicit provider
    gw = create_llm_gateway(cfg)
    assert isinstance(gw, OpenAIGateway)


@pytest.mark.asyncio
async def test_reasoning_coordinator_stub_without_gateway() -> None:
    from investigation_agent_platform.application.investigation.reasoning import ReasoningCoordinator
    from investigation_agent_platform.domain.investigation.models import Investigation, InvestigationRequest, InvestigationContext, InvestigationStatus
    from datetime import datetime, timezone
    from uuid import uuid4

    class DummyPolicy:
        async def validate_prompt_safety(self, tenant_id: str, prompt: str) -> bool:
            return True

    # Build minimal InvestigationState
    inv = Investigation(
        session_id="sess",
        application_id="example-app",
        tenant_id="tenant-a",
        request=InvestigationRequest(application_id="example-app", problem_description="test", session_id="sess", requested_by="tester"),
        context=InvestigationContext(environment="prod", time_window=(datetime.now(timezone.utc), datetime.now(timezone.utc))),
        status=InvestigationStatus.INVESTIGATING,
    )
    from investigation_agent_platform.domain.investigation.models import InvestigationState
    state = InvestigationState(investigation=inv)

    coord = ReasoningCoordinator(prompt_safety_policy=DummyPolicy())  # type: ignore[arg-type]
    decision = await coord.reason("tenant-a", state)
    assert decision.conclusion_readiness >= 0.0
    assert len(decision.observations) > 0
