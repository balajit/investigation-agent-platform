# src/investigation_agent_platform/infrastructure/security/static_token_provider.py
"""Static-token credential provider for development and tests (Part 11.4).

Resolves one allowlisted environment variable into a long-lived AccessToken.
Production use requires an explicit operator decision: the token never
refreshes, so rotation means process restart. The allowlist default is empty
(fail-closed) — nothing resolves until configured.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import SecretStr

from investigation_agent_platform.domain.common.exceptions import CredentialProviderError
from investigation_agent_platform.domain.common.extension import (
    CapabilityScope,
    PluginKind,
    PluginManifest,
)
from investigation_agent_platform.domain.common.lifecycle import AuditEvent
from investigation_agent_platform.infrastructure.observability.job_telemetry import (
    record_credential_refresh,
)
from investigation_agent_platform.ports.security.outbound_credentials import AccessToken

logger = logging.getLogger(__name__)

_STATIC_TOKEN_TTL_SECONDS = 3600


class StaticTokenProvider:
    """Allowlisted-env static token source (Part 11.4)."""

    def __init__(
        self,
        provider_id: str,
        secret_env: str,
        *,
        allowed_env_names: frozenset[str] | None = None,
        observability: Any | None = None,
        audit_sink: Callable[[AuditEvent], Any] | None = None,
    ) -> None:
        if not provider_id:
            raise CredentialProviderError(
                "Static token provider requires a provider id", details={}
            )
        self._provider_id = provider_id
        self._secret_env = secret_env
        self._allowed_env_names = (
            allowed_env_names if allowed_env_names is not None else frozenset()
        )
        self._observability = observability
        self._audit_sink = audit_sink

    async def get_token(self, scope: CapabilityScope, provider_id: str) -> AccessToken:
        if provider_id != self._provider_id:
            raise CredentialProviderError(
                "Unknown provider id for this static adapter",
                details={"provider_id": provider_id},
            )
        if self._secret_env not in self._allowed_env_names:
            raise CredentialProviderError(
                "Static token secret is not in the allowlisted secret names",
                details={"provider_id": self._provider_id},
            )
        value = os.environ.get(self._secret_env, "")
        if not value:
            raise CredentialProviderError(
                "Static token secret is not set",
                details={"provider_id": self._provider_id},
            )
        record_credential_refresh(
            self._observability, provider_id, scope.tenant_id, refreshed=False
        )
        if self._audit_sink is not None:
            try:
                self._audit_sink(
                    AuditEvent(
                        tenant_id=scope.tenant_id,
                        application_id=scope.application_id,
                        actor="credential-provider",
                        action="credential.resolve",
                        resource_type="outbound-credential",
                        resource_id=self._provider_id,
                        detail={"mode": "static"},
                    )
                )
            except Exception as exc:
                logger.warning("Audit sink failed; continuing", extra={"error": str(exc)})
        return AccessToken(
            token=SecretStr(value),
            token_type="Bearer",
            expires_at=datetime.now(UTC) + timedelta(seconds=_STATIC_TOKEN_TTL_SECONDS),
            provider_id=self._provider_id,
        )

    def invalidate(self, scope: CapabilityScope) -> None:
        """No-op: static tokens have no cache entry to drop."""
        _ = scope


def static_manifest(provider_id: str) -> PluginManifest:
    """Plugin manifest for registering a static adapter (Part 11.4)."""
    return PluginManifest(
        plugin_id=provider_id,
        plugin_kind=PluginKind.CREDENTIAL_PROVIDER,
        contract_version="1.0",
        implementation_version="1.0",
        config_schema_version="1.0",
        capabilities=frozenset({"get_token"}),
        config_schema={
            "type": "object",
            "properties": {"secret_env": {"type": "string"}},
            "required": ["secret_env"],
        },
    )
