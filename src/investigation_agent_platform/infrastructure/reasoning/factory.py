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
    "gpt-3.5-turbo": ModelPolicy(
        provider="openai",
        model_name="gpt-3.5-turbo",
        max_context_tokens=16385,
        max_cost_usd_per_call=1.0,
    ),
    "gpt-5.6-sol": ModelPolicy(
        provider="openai",
        model_name="gpt-5.6-sol",
        max_context_tokens=128000,
        max_cost_usd_per_call=5.0,
    ),
    "gpt-4.1": ModelPolicy(
        provider="openai",
        model_name="gpt-4.1",
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
    "muse-spark-1.3": ModelPolicy(
        provider="muse",
        model_name="muse-spark-1.3",
        max_context_tokens=128000,
        max_cost_usd_per_call=1.0,
    ),
}


def get_model_policy(model_name: str) -> ModelPolicy:
    """Resolve the policy record for a model name, failing closed on unknown models."""
    policy = MODEL_REGISTRY.get(model_name.lower())
    if policy is None:
        raise PlatformConfigurationError(
            f"Model '{model_name}' is not in the approved model registry; "
            "refusing to instantiate an unreviewed model (for Azure set "
            "IAP_AZURE_MODEL to the base model id and "
            "IAP_AZURE_DEPLOYMENT to your deployment name)"
        )
    return policy


SUPPORTED_PROVIDERS = ("openai", "anthropic", "azure", "muse")

# Cross-transport pairs: same model family, different wire. Everything else
# must match the registry provider exactly.
TRANSPORT_COMPAT = frozenset({("azure", "openai"), ("openai", "muse")})

# Required LLMConfig fields per provider; enforced uniformly below.
PROVIDER_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "azure": ("azure_endpoint", "azure_deployment"),
    "muse": ("openai_base_url",),
}

# Canonical env name per required field, for error messages.
REQUIREMENT_ENV = {
    "azure_endpoint": "IAP_AZURE_ENDPOINT",
    "azure_deployment": "IAP_AZURE_DEPLOYMENT",
    "openai_base_url": "IAP_MUSE_ENDPOINT",
}


def create_llm_gateway(config: LLMConfig) -> LLMGateway:
    """Create gateway based on LLMConfig.provider.

    Supported: 'openai' (default), 'anthropic' (claude-*), 'azure' (Azure
    OpenAI deployment of an OpenAI model), 'muse' (Meta-hosted Muse models
    over an OpenAI-compatible endpoint).

    F-030: the requested model must exist in MODEL_REGISTRY (see
    get_model_policy) and either match the provider or form a pair in
    TRANSPORT_COMPAT. Required per-provider settings come from
    PROVIDER_REQUIREMENTS. Any violation fails closed.
    """
    provider = (config.provider or "openai").lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise PlatformConfigurationError(
            f"Unsupported LLM provider '{config.provider}'; "
            f"expected one of {', '.join(SUPPORTED_PROVIDERS)}"
        )
    policy = get_model_policy(config.model_name)
    if policy.provider != provider and (provider, policy.provider) not in TRANSPORT_COMPAT:
        raise PlatformConfigurationError(
            f"Model '{config.model_name}' is registered for provider '{policy.provider}' "
            f"but '{provider}' was requested; refusing to cross-wire providers"
        )
    missing = [f for f in PROVIDER_REQUIREMENTS.get(provider, ()) if not getattr(config, f)]
    if missing:
        names = ", ".join(REQUIREMENT_ENV[f] for f in missing)
        raise PlatformConfigurationError(
            f"Provider '{provider}' requires missing settings ({names}); refusing to "
            "build a misconfigured gateway"
        )
    if policy.provider == "muse" and not config.openai_base_url:
        raise PlatformConfigurationError(
            f"Model '{config.model_name}' is Muse-hosted and requires an explicit "
            "Meta endpoint (IAP_MUSE_ENDPOINT); refusing to send Muse "
            "models to the default OpenAI endpoint"
        )
    from investigation_agent_platform.infrastructure.reasoning.openai_adapter import (
        OpenAIGateway,
    )

    if provider == "anthropic":
        from investigation_agent_platform.infrastructure.reasoning.anthropic_adapter import (
            AnthropicGateway,
        )

        gateway_cls: type[AnthropicGateway | OpenAIGateway] = AnthropicGateway
    else:
        gateway_cls = OpenAIGateway
    logger.info(
        "Creating %s LLM gateway",
        provider,
        extra={"model": config.model_name, "provider": provider},
    )
    return gateway_cls(config)
