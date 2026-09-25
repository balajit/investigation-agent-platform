"""LLM gateway generalization test — OpenAI ↔ Anthropic."""

from datetime import UTC

import pytest
from pydantic import SecretStr

from investigation_agent_platform.infrastructure.configuration.config import LLMConfig
from investigation_agent_platform.infrastructure.reasoning.anthropic_adapter import AnthropicGateway
from investigation_agent_platform.infrastructure.reasoning.factory import create_llm_gateway
from investigation_agent_platform.infrastructure.reasoning.openai_adapter import OpenAIGateway


def test_factory_openai_default() -> None:
    cfg = LLMConfig(api_key=SecretStr("sk-test"), model_name="gpt-4o", provider="openai")
    gw = create_llm_gateway(cfg)
    assert isinstance(gw, OpenAIGateway)


def test_factory_anthropic() -> None:
    cfg = LLMConfig(
        api_key=SecretStr("sk-test"), model_name="claude-3-5-sonnet", provider="anthropic"
    )
    gw = create_llm_gateway(cfg)
    assert isinstance(gw, AnthropicGateway)


def test_factory_inferred_claude() -> None:
    from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError

    cfg = LLMConfig(api_key=SecretStr("sk-test"), model_name="claude-3-5-sonnet", provider="openai")
    # F-030: cross-wiring a claude model to the openai provider fails closed
    # instead of silently instantiating a mismatched gateway.
    with pytest.raises(PlatformConfigurationError):
        create_llm_gateway(cfg)
    cfg2 = LLMConfig(api_key=SecretStr("sk-test"), model_name="claude-3-opus")
    # factory infers from model_name when provider not set; here provider defaults to openai so stays openai
    # ensure factory respects explicit provider
    with pytest.raises(PlatformConfigurationError):
        create_llm_gateway(cfg2)


def test_factory_unknown_model_fails_closed() -> None:
    from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError

    cfg = LLMConfig(api_key=SecretStr("sk-test"), model_name="gpt-99-unreviewed", provider="openai")
    with pytest.raises(PlatformConfigurationError):
        create_llm_gateway(cfg)


@pytest.mark.asyncio
async def test_reasoning_coordinator_stub_without_gateway() -> None:
    from datetime import datetime

    from investigation_agent_platform.application.investigation.reasoning import (
        ReasoningCoordinator,
    )
    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationContext,
        InvestigationRequest,
        InvestigationStatus,
    )

    class DummyPolicy:
        async def validate_prompt_safety(self, tenant_id: str, prompt: str) -> bool:
            return True

    # Build minimal InvestigationState
    inv = Investigation(
        session_id="sess",
        application_id="example-app",
        tenant_id="tenant-a",
        request=InvestigationRequest(
            application_id="example-app",
            problem_description="test",
            session_id="sess",
            requested_by="tester",
        ),
        context=InvestigationContext(
            environment="prod", time_window=(datetime.now(UTC), datetime.now(UTC))
        ),
        status=InvestigationStatus.INVESTIGATING,
    )
    from investigation_agent_platform.domain.investigation.models import InvestigationState

    state = InvestigationState(investigation=inv)

    coord = ReasoningCoordinator(prompt_safety_policy=DummyPolicy())  # type: ignore[arg-type]
    decision = await coord.reason("tenant-a", state)
    # No LLM gateway configured: must return an explicit non-conclusive result,
    # never a fabricated observation/conclusion (F-006/F-007).
    assert decision.conclusion_readiness == 0.0
    assert decision.metadata["status"] == "REASONING_UNAVAILABLE"
