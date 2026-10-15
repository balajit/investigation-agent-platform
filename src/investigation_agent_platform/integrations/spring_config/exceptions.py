# src/investigation_agent_platform/integrations/spring_config/exceptions.py
"""Typed Spring Config Server integration errors (appl-profile 1.0 §10.1)."""


class ConfigClientError(RuntimeError):
    """Base error for Config Server integration."""

    code = "SPRING_ERROR"


class ConfigAuthenticationError(ConfigClientError):
    """Upstream credential rejected (token endpoint or config server 401)."""

    code = "SPRING_AUTH"


class ConfigAuthorizationError(ConfigClientError):
    """Scope/allowlist denial (config server 403 or local allowlist)."""

    code = "SPRING_FORBIDDEN"


class ConfigNotFoundError(ConfigClientError):
    """Unknown Spring application/profile/label (config server 404)."""

    code = "SPRING_NOT_FOUND"


class ConfigRateLimitError(ConfigClientError):
    """Upstream throttling (429). Carries optional retry-after seconds."""

    code = "SPRING_THROTTLED"

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ConfigServerError(ConfigClientError):
    """Upstream 5xx."""

    code = "SPRING_UNAVAILABLE"


class ConfigTransportError(ConfigClientError):
    """Network-level failure (DNS, connect, timeout)."""

    code = "SPRING_UNAVAILABLE"
