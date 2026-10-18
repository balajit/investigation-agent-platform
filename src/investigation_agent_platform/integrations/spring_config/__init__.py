# src/investigation_agent_platform/integrations/spring_config/__init__.py
"""Spring Config Server integration (appl-profile 1.0 §10)."""

from investigation_agent_platform.integrations.spring_config.client import (
    OAuth2ClientCredentials,
    SpringConfigClient,
)
from investigation_agent_platform.integrations.spring_config.derivation import (
    derive_profile,
    parse_jdbc_url,
)
from investigation_agent_platform.integrations.spring_config.exceptions import (
    ConfigAuthenticationError,
    ConfigAuthorizationError,
    ConfigClientError,
    ConfigNotFoundError,
    ConfigRateLimitError,
    ConfigServerError,
    ConfigTransportError,
)
from investigation_agent_platform.integrations.spring_config.models import (
    ConfigResult,
    SpringBinding,
)

__all__ = [
    "ConfigAuthenticationError",
    "ConfigAuthorizationError",
    "ConfigClientError",
    "ConfigNotFoundError",
    "ConfigRateLimitError",
    "ConfigResult",
    "ConfigServerError",
    "ConfigTransportError",
    "OAuth2ClientCredentials",
    "SpringBinding",
    "SpringConfigClient",
    "derive_profile",
    "parse_jdbc_url",
]
