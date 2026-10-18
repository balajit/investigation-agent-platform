# src/investigation_agent_platform/infrastructure/security/oauth_bridge.py
"""Generic OAuth2 token bridge for any interaction (P3-4).

Adapts the registry-based ``OutboundCredentialProvider`` (client-credentials
grant with scoped caching) to the simple per-tenant token callable that MCP
adapters and other outbound clients consume. Any interaction needing OAuth2 —
MCP servers, HTTP probes, future transports — resolves tokens through here
instead of implementing its own grant/caching.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class TenantScopedOAuthProvider:
    """Binds one registered OAuth provider to tenant-scoped token fetch."""

    def __init__(
        self,
        credential_provider: Any,
        provider_id: str,
        application_id: str | None = None,
    ) -> None:
        self._credential_provider = credential_provider
        self._provider_id = provider_id
        self._application_id = application_id

    async def get_token(self, tenant_id: str) -> str | None:
        """Return a bearer token for the tenant, or None when unavailable."""
        from investigation_agent_platform.domain.common.extension import CapabilityScope

        try:
            token = await self._credential_provider.get_token(
                CapabilityScope(tenant_id=tenant_id, application_id=self._application_id),
                self._provider_id,
            )
        except Exception as exc:
            logger.warning(
                "OAuth token acquisition failed",
                extra={"provider_id": self._provider_id, "error": str(exc)},
            )
            return None
        value = getattr(token, "access_token", None) or getattr(token, "token", None)
        return str(value) if value else None


def build_oauth_provider_from_env() -> tuple[str, Any] | None:
    """Construct (provider_id, OAuth2ClientCredentialsProvider) from env.

    Single-provider form (``IAP_OAUTH_PROVIDER_ID`` + ``IAP_OAUTH_TOKEN_ENDPOINT``
    + ``IAP_OAUTH_CLIENT_ID`` + ``IAP_OAUTH_CLIENT_SECRET_ENV`` naming the env
    var that holds the secret). Absent means OAuth unconfigured — callers
    treat None as "no OAuth", never an error.
    """
    import os

    from investigation_agent_platform.infrastructure.security.oauth2_client_credentials import (
        OAuth2ClientCredentialsProvider,
        OAuth2ProviderConfig,
    )

    provider_id = os.environ.get("IAP_OAUTH_PROVIDER_ID", "").strip()
    token_endpoint = os.environ.get("IAP_OAUTH_TOKEN_ENDPOINT", "").strip()
    client_id = os.environ.get("IAP_OAUTH_CLIENT_ID", "").strip()
    if not (provider_id and token_endpoint and client_id):
        return None
    config = OAuth2ProviderConfig(
        provider_id=provider_id,
        token_endpoint=token_endpoint,
        client_id=client_id,
        client_secret_env=os.environ.get("IAP_OAUTH_CLIENT_SECRET_ENV", "IAP_OAUTH_CLIENT_SECRET"),
        scope=os.environ.get("IAP_OAUTH_SCOPE", ""),
        allowed_hosts=frozenset(
            h.strip()
            for h in os.environ.get("IAP_OAUTH_ALLOWED_HOSTS", "").split(",")
            if h.strip()
        ),
    )
    return provider_id, OAuth2ClientCredentialsProvider(config=config)
