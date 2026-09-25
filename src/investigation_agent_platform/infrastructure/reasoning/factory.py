# src/investigation_agent_platform/infrastructure/reasoning/factory.py
"""Factory for provider-agnostic LLM gateway with model policy enforcement (F-030)."""

import logging

from pydantic import BaseModel, ConfigDict

from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError
from investigation_agent_platform.infrastructure.configuration.config import LLMConfig
from investigation_agent_platform.ports.reasoning.llm_gateway import LLMGateway

logger = logging.getLogger(__name__)


class ModelPolicy(BaseModel):
    """Immutable policy record for one deployable model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model_name: str
    max_context_tokens: int = 128000
    max_cost_usd_per_call: float = 5.0
    allowed_classifications: tuple[str, ...] = ("PUBLIC", "INTERNAL")


# Tenant-independent model registry: only listed models may be instantiated.
# Unknown model names fail closed here instead of being passed blindly to a provider SDK.
MODEL_REGISTRY: dict[str, ModelPolicy] = {
    "gpt-4o": ModelPolicy(
        provider="openai", model_name="gpt-4o", max_context_tokens=128000, max_cost_usd_per_call=5.0
    ),
    "gpt-4o-mini": ModelPolicy(
        provider="openai",
        model_name="gpt-4o-mini",
        max_context_tokens=128000,
        max_cost_usd_per_call=1.0,
    ),
    "gpt-4-turbo": ModelPolicy(
        provider="openai",
        model_name="gpt-4-turbo",
        max_context_tokens=128000,
        max_cost_usd_per_call=5.0,
    ),
    "claude-3-5-sonnet": ModelPolicy(
        provider="anthropic",
        model_name="claude-3-5-sonnet",
        max_context_tokens=200000,
        max_cost_usd_per_call=5.0,
    ),
    "claude-3-opus": ModelPolicy(
        provider="anthropic",
        model_name="claude-3-opus",
        max_context_tokens=200000,
        max_cost_usd_per_call=10.0,
    ),
    "claude-3-haiku": ModelPolicy(
        provider="anthropic",
        model_name="claude-3-haiku",
        max_context_tokens=200000,
        max_cost_usd_per_call=1.0,
    ),
    "claude-sonnet-4": ModelPolicy(
        provider="anthropic",
        model_name="claude-sonnet-4",
        max_context_tokens=200000,
        max_cost_usd_per_call=5.0,
    ),
}


def get_model_policy(model_name: str) -> ModelPolicy:
    """Resolve the policy record for a model name, failing closed on unknown models."""
    policy = MODEL_REGISTRY.get(model_name.lower())
    if policy is None:
        raise PlatformConfigurationError(
            f"Model '{model_name}' is not in the approved model registry; "
            "refusing to instantiate an unreviewed model"
        )
    return policy


def create_llm_gateway(config: LLMConfig) -> LLMGateway:
    """Create gateway based on LLMConfig.provider.

    Supported: 'openai' (default for gpt-*) and 'anthropic' (claude-*).
    Env var IAP_LLM_PROVIDER overrides inference; otherwise inferred from model_name.

    F-030: the requested model must exist in MODEL_REGISTRY and its registered
    provider must agree with the configured/inferred provider — otherwise
    construction fails closed instead of instantiating an unreviewed model.
    """
    provider = (config.provider or "").lower() if hasattr(config, "provider") else ""
    if not provider:
        model = config.model_name.lower()
        if model.startswith("claude"):
            provider = "anthropic"
        else:
            provider = "openai"

    policy = get_model_policy(config.model_name)
    if policy.provider != provider:
        raise PlatformConfigurationError(
            f"Model '{config.model_name}' is registered for provider '{policy.provider}' "
            f"but '{provider}' was requested; refusing to cross-wire providers"
        )

    if provider == "anthropic":
        from investigation_agent_platform.infrastructure.reasoning.anthropic_adapter import (
            AnthropicGateway,
        )

        logger.info("Creating Anthropic LLM gateway", extra={"model": config.model_name})
        return AnthropicGateway(config)
    else:
        from investigation_agent_platform.infrastructure.reasoning.openai_adapter import (
            OpenAIGateway,
        )

        logger.info("Creating OpenAI LLM gateway", extra={"model": config.model_name})
        return OpenAIGateway(config)
