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


def test_registry_accepts_muse_spark_with_policy() -> None:
    from investigation_agent_platform.infrastructure.reasoning.factory import get_model_policy

    policy = get_model_policy("muse-spark-1.3")
    assert policy.provider == "muse"
    assert policy.model_name == "muse-spark-1.3"
    assert policy.max_context_tokens == 128000
    assert policy.max_cost_usd_per_call == 1.0


def test_factory_constructs_muse_spark_on_openai_transport() -> None:
    cfg = LLMConfig(
        api_key=SecretStr("sk-test"),
        model_name="muse-spark-1.3",
        provider="openai",
        openai_base_url="https://gateway.example.com/v1",
    )
    gw = create_llm_gateway(cfg)
    assert isinstance(gw, OpenAIGateway)


def test_factory_constructs_muse_direct_path() -> None:
    cfg = LLMConfig(
        api_key=SecretStr("sk-test"),
        model_name="muse-spark-1.3",
        provider="muse",
        openai_base_url="https://gateway.example.com/v1",
    )
    gw = create_llm_gateway(cfg)
    assert isinstance(gw, OpenAIGateway)


def test_factory_rejects_muse_model_without_endpoint() -> None:
    from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError

    for provider, pattern in (
        ("muse", "missing settings"),
        ("openai", "explicit"),
    ):
        cfg = LLMConfig(
            api_key=SecretStr("sk-test"), model_name="muse-spark-1.3", provider=provider
        )
        with pytest.raises(PlatformConfigurationError, match=pattern):
            create_llm_gateway(cfg)


def test_factory_rejects_muse_provider_with_openai_model() -> None:
    from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError

    cfg = LLMConfig(api_key=SecretStr("sk-test"), model_name="gpt-4o-mini", provider="muse")
    with pytest.raises(PlatformConfigurationError, match="cross-wire"):
        create_llm_gateway(cfg)


def test_factory_rejects_muse_spark_cross_wired_to_anthropic() -> None:
    from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError

    cfg = LLMConfig(api_key=SecretStr("sk-test"), model_name="muse-spark-1.3", provider="anthropic")
    with pytest.raises(PlatformConfigurationError):
        create_llm_gateway(cfg)


def test_custom_endpoint_reaches_sdk_client() -> None:
    from unittest.mock import patch

    cfg = LLMConfig(
        api_key=SecretStr("sk-test"),
        model_name="muse-spark-1.3",
        provider="openai",
        openai_base_url="https://gateway.example.com/v1/",
    )
    gw = create_llm_gateway(cfg)
    assert isinstance(gw, OpenAIGateway)
    with patch("openai.AsyncOpenAI", autospec=True) as mock_client:
        gw._get_client()
        _, kwargs = mock_client.call_args
        assert kwargs["base_url"] == "https://gateway.example.com/v1"
        assert kwargs["api_key"] == "sk-test"


def test_custom_endpoint_reaches_streaming_client() -> None:
    from unittest.mock import patch

    from investigation_agent_platform.infrastructure.reasoning.streaming_adapters import (
        OpenAIStreamingGateway,
    )

    cfg = LLMConfig(
        api_key=SecretStr("sk-test"),
        model_name="muse-spark-1.3",
        provider="openai",
        openai_base_url="https://gateway.example.com/v1",
    )
    gw = OpenAIStreamingGateway(cfg)
    with patch("openai.AsyncOpenAI", autospec=True) as mock_client:
        gw._get_client()
        _, kwargs = mock_client.call_args
        assert kwargs["base_url"] == "https://gateway.example.com/v1"


def test_default_endpoint_sends_no_base_url() -> None:
    from unittest.mock import patch

    cfg = LLMConfig(api_key=SecretStr("sk-test"), model_name="gpt-4o-mini", provider="openai")
    gw = create_llm_gateway(cfg)
    with patch("openai.AsyncOpenAI", autospec=True) as mock_client:
        gw._get_client()
        _, kwargs = mock_client.call_args
        assert "base_url" not in kwargs


def test_non_http_endpoint_fails_closed() -> None:
    from unittest.mock import patch

    cfg = LLMConfig(
        api_key=SecretStr("sk-test"),
        model_name="muse-spark-1.3",
        provider="openai",
        openai_base_url="ftp://gateway.example.com/v1",
    )
    gw = create_llm_gateway(cfg)
    with patch("openai.AsyncOpenAI", autospec=True):
        with pytest.raises(RuntimeError, match="http"):
            gw._get_client()


def test_llm_config_reads_namespaced_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from investigation_agent_platform.infrastructure.configuration.config import (
        _llm_config_from_env,
    )

    monkeypatch.setenv("IAP_LLM_PROVIDER", "openai")
    monkeypatch.setenv("IAP_OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("IAP_OPENAI_MODEL", "muse-spark-1.3")
    monkeypatch.setenv("IAP_OPENAI_ENDPOINT", "https://gateway.example.com/v1")
    cfg = _llm_config_from_env()
    assert cfg.openai_base_url == "https://gateway.example.com/v1"
    assert cfg.model_name == "muse-spark-1.3"


def test_llm_config_reads_muse_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    from investigation_agent_platform.infrastructure.configuration.config import (
        _llm_config_from_env,
    )

    monkeypatch.setenv("IAP_LLM_PROVIDER", "muse")
    monkeypatch.setenv("IAP_MUSE_API_KEY", "sk-muse")
    monkeypatch.setenv("IAP_MUSE_MODEL", "muse-spark-1.3")
    monkeypatch.setenv("IAP_MUSE_ENDPOINT", "https://muse.example.com/v1")
    cfg = _llm_config_from_env()
    assert cfg.api_key.get_secret_value() == "sk-muse"
    assert cfg.openai_base_url == "https://muse.example.com/v1"


def test_llm_config_ignores_legacy_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError
    from investigation_agent_platform.infrastructure.configuration.config import (
        _llm_config_from_env,
    )

    # Legacy names are dead: a legacy-only environment halts instead of wiring.
    monkeypatch.setenv("IAP_LLM_PROVIDER", "muse")
    monkeypatch.setenv("IAP_LLM_MODEL", "muse-spark-1.3")
    monkeypatch.setenv("IAP_LLM_API_KEY", "sk-legacy")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "sk-legacy")
    monkeypatch.setenv("ACCOUNT_LLM_PROVIDER", "muse")
    for var in (
        "IAP_MUSE_API_KEY",
        "IAP_MUSE_MODEL",
        "IAP_MUSE_ENDPOINT",
    ):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(PlatformConfigurationError):
        _llm_config_from_env()


def test_llm_config_muse_without_endpoint_halts_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError
    from investigation_agent_platform.infrastructure.configuration.config import (
        _llm_config_from_env,
    )

    monkeypatch.setenv("IAP_MUSE_API_KEY", "sk-test")
    monkeypatch.setenv("IAP_MUSE_MODEL", "muse-spark-1.3")
    monkeypatch.setenv("IAP_LLM_PROVIDER", "muse")
    monkeypatch.delenv("IAP_MUSE_ENDPOINT", raising=False)
    with pytest.raises(PlatformConfigurationError, match="muse"):
        _llm_config_from_env()


def test_pricing_covers_muse_spark() -> None:
    from investigation_agent_platform.infrastructure.reasoning.pricing import estimate_cost

    cost, _ = estimate_cost("muse-spark-1.3", 1000, 1000)
    assert cost == pytest.approx(0.00015 + 0.0006)


def test_registry_accepts_gpt_41_with_policy() -> None:
    from investigation_agent_platform.infrastructure.reasoning.factory import get_model_policy

    policy = get_model_policy("gpt-4.1")
    assert policy.provider == "openai"
    assert policy.max_context_tokens == 128000
    assert policy.max_cost_usd_per_call == 5.0


def test_factory_constructs_gpt_41_on_azure() -> None:
    cfg = LLMConfig(
        api_key=SecretStr("sk-test"),
        model_name="gpt-4.1",
        provider="azure",
        azure_endpoint="https://example.openai.azure.com",
        azure_deployment="gpt-4.1",
    )
    gw = create_llm_gateway(cfg)
    assert isinstance(gw, OpenAIGateway)
    assert gw._wire_model_name() == "gpt-4.1"


def test_azure_custom_deployment_bypasses_registry_but_keeps_base_policy() -> None:
    """Custom deployment names need no registry entry; policy/pricing stay on the base model."""
    from investigation_agent_platform.infrastructure.reasoning.factory import get_model_policy

    cfg = LLMConfig(
        api_key=SecretStr("sk-test"),
        model_name="gpt-4.1",
        provider="azure",
        azure_endpoint="https://example.openai.azure.com",
        azure_deployment="my-gpt41-v2",
    )
    gw = create_llm_gateway(cfg)
    assert isinstance(gw, OpenAIGateway)
    assert gw._wire_model_name() == "my-gpt41-v2"
    assert get_model_policy(cfg.model_name).max_cost_usd_per_call == 5.0


def test_pricing_covers_gpt_41() -> None:
    from investigation_agent_platform.infrastructure.reasoning.pricing import estimate_cost

    cost, _ = estimate_cost("gpt-4.1", 1000, 1000)
    assert cost == pytest.approx(0.005 + 0.015)


def test_missing_key_fails_closed_with_provider_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError
    from investigation_agent_platform.infrastructure.configuration.config import (
        _llm_config_from_env,
    )

    for provider in ("openai", "anthropic", "azure", "muse"):
        monkeypatch.setenv("IAP_LLM_PROVIDER", provider)
        for var in (
            "IAP_OPENAI_API_KEY",
            "IAP_ANTHROPIC_API_KEY",
            "IAP_AZURE_API_KEY",
            "IAP_MUSE_API_KEY",
        ):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(PlatformConfigurationError, match=f"IAP_{provider.upper()}_API_KEY"):
            _llm_config_from_env()


def test_factory_rejects_unknown_provider() -> None:
    from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError

    cfg = LLMConfig(api_key=SecretStr("sk-test"), model_name="gpt-4o", provider="azuree")
    with pytest.raises(PlatformConfigurationError, match="Unsupported LLM provider"):
        create_llm_gateway(cfg)


def test_factory_azure_missing_deployment_names_canonical_env() -> None:
    from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError

    cfg = LLMConfig(
        api_key=SecretStr("sk-test"),
        model_name="gpt-4.1",
        provider="azure",
        azure_endpoint="https://example.openai.azure.com",
    )
    with pytest.raises(PlatformConfigurationError, match="IAP_AZURE_DEPLOYMENT"):
        create_llm_gateway(cfg)


def test_llm_config_reads_azure_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    from investigation_agent_platform.infrastructure.configuration.config import (
        _llm_config_from_env,
    )

    monkeypatch.setenv("IAP_LLM_PROVIDER", "azure")
    monkeypatch.setenv("IAP_AZURE_API_KEY", "az-key")
    monkeypatch.setenv("IAP_AZURE_MODEL", "gpt-4.1")
    monkeypatch.setenv("IAP_AZURE_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("IAP_AZURE_DEPLOYMENT", "gpt-4.1")
    monkeypatch.setenv("IAP_AZURE_API_VERSION", "2025-01-01-preview")
    cfg = _llm_config_from_env()
    assert cfg.azure_endpoint == "https://example.openai.azure.com"
    assert cfg.azure_deployment == "gpt-4.1"
    assert cfg.azure_api_version == "2025-01-01-preview"


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
