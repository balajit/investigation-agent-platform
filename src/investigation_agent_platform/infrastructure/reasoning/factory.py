# src/investigation_agent_platform/infrastructure/reasoning/factory.py
"""Factory for provider-agnostic LLM gateway."""

import logging

from investigation_agent_platform.infrastructure.configuration.config import LLMConfig
from investigation_agent_platform.ports.reasoning.llm_gateway import LLMGateway

logger = logging.getLogger(__name__)


def create_llm_gateway(config: LLMConfig) -> LLMGateway:
    """Create gateway based on LLMConfig.provider.

    Supported: 'openai' (default for gpt-*) and 'anthropic' (claude-*).
    Env var IAP_LLM_PROVIDER overrides inference; otherwise inferred from model_name.
    """
    provider = (config.provider or "").lower() if hasattr(config, "provider") else ""
    if not provider:
        model = config.model_name.lower()
        if model.startswith("claude"):
            provider = "anthropic"
        else:
            provider = "openai"

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
