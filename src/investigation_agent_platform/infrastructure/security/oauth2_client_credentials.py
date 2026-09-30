# src/investigation_agent_platform/infrastructure/security/oauth2_client_credentials.py
"""RFC 6749 OAuth2 client-credentials credential provider (Part 11.4).

Generic replacement for organization-specific SAT libraries: any
 standards-compliant token endpoint works. Tokens are cached per
``(tenant_id, application_id, provider_id)`` under per-key asyncio locks
(single-flight refresh), refreshed proactively at 80% of TTL, and
invalidated on terminal (401/403) responses. The HTTP layer is stdlib
``urllib`` offloaded via ``asyncio.to_thread`` — no new dependencies.

Never logs tokens, client secrets, or full auth responses. Failures carry
provider id + host + failure class only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr

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
from investigation_agent_platform.infrastructure.security.endpoint_guard import (
    EndpointPolicy,
    validate_endpoint,
)
from investigation_agent_platform.ports.security.outbound_credentials import AccessToken

logger = logging.getLogger(__name__)

_REFRESH_FRACTION = 0.8
_MIN_TTL_SECONDS = 60
_MAX_TOKEN_RESPONSE_BYTES = 65_536


class OAuth2ProviderConfig(BaseModel):
    """Static configuration for one OAuth2 credential provider (Part 11.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str = Field(..., min_length=1, max_length=128)
    token_endpoint: str = Field(..., min_length=1, max_length=512)
    client_id: str = Field(..., min_length=1, max_length=256)
    client_secret_env: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Z][A-Z0-9_]*$")
    scope: str = Field(default="", max_length=512)
    audience: str | None = Field(default=None, max_length=512)
    allowed_hosts: frozenset[str] = Field(default_factory=frozenset, max_length=100)
    timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)


class _CachedToken:
    __slots__ = ("refresh_at", "token")

    def __init__(self, token: AccessToken, refresh_at: float) -> None:
        self.token = token
        self.refresh_at = refresh_at


def oauth2_manifest(provider_id: str) -> PluginManifest:
    """Plugin manifest for registering an OAuth2 adapter (Part 11.4)."""
    return PluginManifest(
        plugin_id=provider_id,
        plugin_kind=PluginKind.CREDENTIAL_PROVIDER,
        contract_version="1.0",
        implementation_version="1.0",
        config_schema_version="1.0",
        capabilities=frozenset({"get_token"}),
        config_schema={
            "type": "object",
            "properties": {
                "token_endpoint": {"type": "string"},
                "client_id": {"type": "string"},
            },
            "required": ["token_endpoint", "client_id"],
        },
    )


class OAuth2ClientCredentialsProvider:
    """RFC 6749 client-credentials grant with scoped caching (Part 11.4)."""

    def __init__(
        self,
        config: OAuth2ProviderConfig,
        *,
        allowed_env_names: frozenset[str] | None = None,
        observability: Any | None = None,
        audit_sink: Callable[[AuditEvent], Any] | None = None,
    ) -> None:
        self._config = config
        self._allowed_env_names = (
            allowed_env_names
            if allowed_env_names is not None
            else frozenset({config.client_secret_env})
        )
        self._observability = observability
        self._audit_sink = audit_sink
        self._cache: dict[str, _CachedToken] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        endpoint_policy = EndpointPolicy(
            allowed_hosts=config.allowed_hosts,
            require_https=True,
            timeout_seconds=config.timeout_seconds,
        )
        self._endpoint = validate_endpoint(
            config.token_endpoint, endpoint_policy, purpose="oauth2-token"
        )

    def _cache_key(self, scope: CapabilityScope) -> str:
        return f"{scope.tenant_id}:{scope.application_id or '-'}:{self._config.provider_id}"

    async def _key_lock(self, key: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    def _client_secret(self) -> str:
        name = self._config.client_secret_env
        if name not in self._allowed_env_names:
            raise CredentialProviderError(
                "OAuth2 client secret is not in the allowlisted secret names",
                details={"provider_id": self._config.provider_id},
            )
        value = os.environ.get(name, "")
        if not value:
            raise CredentialProviderError(
                "OAuth2 client secret is not set",
                details={"provider_id": self._config.provider_id},
            )
        return value

    async def get_token(self, scope: CapabilityScope, provider_id: str) -> AccessToken:
        if provider_id != self._config.provider_id:
            raise CredentialProviderError(
                "Unknown provider id for this OAuth2 adapter",
                details={"provider_id": provider_id},
            )
        key = self._cache_key(scope)
        cached = self._cache.get(key)
        now = time.monotonic()
        if cached is not None and now < cached.refresh_at and cached.token.is_usable():
            record_credential_refresh(
                self._observability, provider_id, scope.tenant_id, refreshed=False
            )
            return cached.token
        lock = await self._key_lock(key)
        async with lock:
            cached = self._cache.get(key)
            now = time.monotonic()
            if cached is not None and now < cached.refresh_at and cached.token.is_usable():
                record_credential_refresh(
                    self._observability, provider_id, scope.tenant_id, refreshed=False
                )
                return cached.token
            token = await self._fetch_token(scope)
            ttl = max(0, (token.expires_at - datetime.now(UTC)).total_seconds())
            self._cache[key] = _CachedToken(token, now + ttl * _REFRESH_FRACTION)
            record_credential_refresh(
                self._observability, provider_id, scope.tenant_id, refreshed=True
            )
            self._audit(
                scope,
                "credential.refresh",
                {"provider_id": provider_id, "ttl_seconds": int(ttl)},
            )
            return token

    def invalidate(self, scope: CapabilityScope) -> None:
        """Drop the cached token (called on downstream 401/403)."""
        self._cache.pop(self._cache_key(scope), None)

    async def _fetch_token(self, scope: CapabilityScope) -> AccessToken:
        form: dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": self._config.client_id,
            "client_secret": self._client_secret(),
        }
        if self._config.scope:
            form["scope"] = self._config.scope
        if self._config.audience:
            form["audience"] = self._config.audience
        body = urllib.parse.urlencode(form).encode("ascii")
        request = urllib.request.Request(
            self._endpoint,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            status, payload = await asyncio.to_thread(
                _post_token_request,
                request,
                self._config.timeout_seconds,
                _MAX_TOKEN_RESPONSE_BYTES,
            )
        except CredentialProviderError:
            raise
        except Exception as exc:
            self._audit(scope, "credential.refresh_failed", {"failure": type(exc).__name__})
            raise CredentialProviderError(
                "OAuth2 token request failed",
                details={
                    "provider_id": self._config.provider_id,
                    "failure": type(exc).__name__,
                },
            ) from exc
        if status in (401, 403):
            self.invalidate(scope)
            self._audit(scope, "credential.refresh_failed", {"failure": f"http-{status}"})
            raise CredentialProviderError(
                "OAuth2 token endpoint refused credentials",
                details={
                    "provider_id": self._config.provider_id,
                    "failure": f"http-{status}",
                },
            )
        if status != 200:
            self._audit(scope, "credential.refresh_failed", {"failure": f"http-{status}"})
            raise CredentialProviderError(
                "OAuth2 token endpoint returned an error",
                details={
                    "provider_id": self._config.provider_id,
                    "failure": f"http-{status}",
                },
            )
        return self._parse_token_response(scope, payload)

    def _parse_token_response(self, scope: CapabilityScope, payload: dict[str, Any]) -> AccessToken:
        _ = scope
        raw_token = payload.get("access_token")
        if not isinstance(raw_token, str) or not raw_token:
            raise CredentialProviderError(
                "OAuth2 token response carries no access token",
                details={"provider_id": self._config.provider_id},
            )
        token_type = payload.get("token_type", "Bearer")
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            raise CredentialProviderError(
                "OAuth2 token response is not a bearer token",
                details={"provider_id": self._config.provider_id},
            )
        try:
            ttl = int(payload.get("expires_in", 300))
        except (TypeError, ValueError) as exc:
            raise CredentialProviderError(
                "OAuth2 token response has an invalid expiry",
                details={"provider_id": self._config.provider_id},
            ) from exc
        if ttl < _MIN_TTL_SECONDS:
            raise CredentialProviderError(
                "OAuth2 token TTL is below the usable minimum",
                details={"provider_id": self._config.provider_id},
            )
        return AccessToken(
            token=SecretStr(raw_token),
            token_type="Bearer",
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl),
            provider_id=self._config.provider_id,
        )

    def _audit(self, scope: CapabilityScope, action: str, detail: dict[str, Any]) -> None:
        if self._audit_sink is None:
            return
        try:
            self._audit_sink(
                AuditEvent(
                    tenant_id=scope.tenant_id,
                    application_id=scope.application_id,
                    actor="credential-provider",
                    action=action,
                    resource_type="outbound-credential",
                    resource_id=self._config.provider_id,
                    detail=detail,
                )
            )
        except Exception as exc:
            logger.warning("Audit sink failed; continuing", extra={"error": str(exc)})


def _post_token_request(
    request: urllib.request.Request, timeout_seconds: float, max_bytes: int
) -> tuple[int, dict[str, Any]]:
    """Blocking token POST; runs in a worker thread. Returns (status, payload)."""
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status = int(response.status or 0)
            raw = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        return int(exc.code or 0), {}
    if len(raw) > max_bytes:
        raise CredentialProviderError(
            "OAuth2 token response exceeds size limit",
            details={},
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CredentialProviderError(
            "OAuth2 token response is not valid JSON",
            details={},
        ) from exc
    if not isinstance(payload, dict):
        raise CredentialProviderError(
            "OAuth2 token response is not an object",
            details={},
        )
    return status, payload
