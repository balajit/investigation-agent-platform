# src/investigation_agent_platform/infrastructure/evidence/runtime/tenant_telemetry.py
"""Per-tenant Elasticsearch telemetry endpoints (Phase A: config + load-time validation).

Each tenant's observability lives in the tenant's own Elasticsearch
deployment(s), declared in the tenant operator scope — never in the repo::

    <telemetry_dir>/<tenant_id>/telemetry.conf

One INI section per endpoint (section name = endpoint name)::

    [orders-es]
    url = https://elastic.orders.example.com:9200
    api_key = <secret>
    timeout = 30
    pattern = logs-orders-*
    services = svc-a, svc-b
    managed_by = manual

``services`` homes service names onto exactly one endpoint (configure-time
check, fail closed). ``managed_by=auto`` entries are discovery-written
defaults; the discovery writer never clobbers ``manual`` entries
(``merge_managed_defaults`` enforces that here so the rule lives next to
the model, not in a future writer).

Secrets follow the existing ``SecretStr``/redaction standard: values are
never logged; logs name only tenants, endpoint names, and URL hosts.

Precedence: the operator-scope file is the source of truth for endpoint
existence and service homing. ``IAP_ES_<TENANT>_<NAME>_{URL,USER,PASSWORD,
API_KEY,BEARER_TOKEN,VERIFY_CERTS,CA_CERTS,OAUTH_TOKEN_URL,OAUTH_CLIENT_ID,
OAUTH_CLIENT_SECRET,OAUTH_SCOPE,TIMEOUT,PATTERN}`` env vars override
individual connection fields of file-defined endpoints only — an override
naming an unknown endpoint fails closed rather than conjuring
infrastructure.

OAuth client-credentials (``oauth_*`` fields): the endpoint's client
carries no static credential; an ``OAuthTokenProvider`` fetches a bearer
token from the IdP, caches it for its validity period, and refreshes only
when the token is within ``OAUTH_REFRESH_SKEW_SECONDS`` (300s) of expiry —
never one token fetch per request. The cached token is injected per ES
request via ``client.options(bearer_auth=...)`` on the shared transport.
Token-fetch failures fail closed and loud (typed errors, ERROR logs naming
tenant/endpoint/token host — never secrets).
"""

from __future__ import annotations

import asyncio
import configparser
import fnmatch
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from investigation_agent_platform.domain.common.exceptions import (
    ExecutionError,
    PlatformConfigurationError,
    ProviderNotConfiguredError,
    ProviderUnavailableException,
)
from investigation_agent_platform.infrastructure.evidence.runtime.validation import (
    INDEX_PATTERN_CHARS,
)

logger = logging.getLogger(__name__)

TELEMETRY_FILE_NAME = "telemetry.conf"
ENV_PREFIX = "IAP_ES_"
ENV_OVERRIDABLE_FIELDS = (
    "URL",
    "USER",
    "PASSWORD",
    "API_KEY",
    "BEARER_TOKEN",
    "VERIFY_CERTS",
    "CA_CERTS",
    "OAUTH_TOKEN_URL",
    "OAUTH_CLIENT_ID",
    "OAUTH_CLIENT_SECRET",
    "OAUTH_SCOPE",
    "TIMEOUT",
    "PATTERN",
)

# OAuth client-credentials: access tokens are cached for the validity
# period the provider states in ``expires_in`` (RFC 6749 §5.1) and
# refreshed only inside the skew window below — never one token fetch per
# request. 300s means a token is treated as expired five minutes before
# the IdP says it is, so in-flight requests never race the expiry. A grant
# response without ``expires_in`` fails closed: the validity period is the
# provider's to state, never ours to assume.
OAUTH_REFRESH_SKEW_SECONDS = 300.0
OAUTH_GRANT_TYPE = "client_credentials"
OAUTH_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Fernet envelope for secret fields (password, api_key, bearer_token,
# oauth_client_secret), in two forms:
#   1. `fernet:<token>` — key from ``IAP_ES_FERNET_KEY`` (env only; the key
#      never lives in the file). Real secrecy: file holds ciphertext only.
#   2. `fernet:<cipher>:<token>` — two-part in-field form: the cipher (key)
#      travels with the ciphertext. This keeps the PLAINTEXT secret out of
#      the file and out of logs, but anyone who can read the file can
#      decrypt it — access-control-level protection, not real secrecy.
#      Provided for operator artifacts that already carry cipher +
#      ciphertext together (e.g. a separate `cipher` line folds into this
#      form; a bare `cipher` key is rejected as unknown).
# Plain and encrypted forms are one field, so they cannot conflict; across
# auth kinds the ``auth_kind`` precedence applies.
FERNET_ENV_VAR = "IAP_ES_FERNET_KEY"
FERNET_PREFIX = "fernet:"
VERIFY_TRUE = {"true", "yes", "1", "on"}
VERIFY_FALSE = {"false", "no", "0", "off"}


def _check_fernet_part(*, source: str, field_name: str, part_name: str, part: str) -> None:
    """Structural pre-checks on one envelope part (lengths/charset only).

    Reports exactly which part is malformed and how — counts and character
    kinds, never values — so operators can fix the assembly without
    exposing secrets. A well-formed part can still fail to decrypt (wrong
    key for the token), which surfaces separately below.
    """
    if len(part) > 8192:
        raise PlatformConfigurationError(
            f"{source}: {field_name} fernet: {part_name} is {len(part)} characters; "
            "check for a pasted file or multi-line value."
        )
    bad = sorted(
        set(part) - set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")
    )
    if bad:
        raise PlatformConfigurationError(
            f"{source}: {field_name} fernet: {part_name} contains characters "
            f"outside url-safe base64: {''.join(bad)} "
            "(check for b'...' wrappers, quotes, or whitespace)."
        )
    if part_name == "cipher" and len(part) != 44:
        raise PlatformConfigurationError(
            f"{source}: {field_name} fernet: cipher is {len(part)} characters; "
            "a Fernet key is 44 url-safe base64 characters (32 bytes). "
            "If the cipher comes from another scheme, name it so a matching "
            "envelope can be added."
        )


def decrypt_secret_value(*, field_name: str, value: str, source: str) -> str:
    """Resolve one secret field: plaintext passes through, ``fernet:`` decrypts.

    One-part form uses the env key; two-part form carries its own cipher.
    Fail closed when the envelope is present but undecryptable (missing
    key, bad key/cipher, corrupt token) or decrypts to an empty secret —
    a silently-unauthenticated client must never be constructed.
    """
    if not value.startswith(FERNET_PREFIX):
        return value
    rest = value[len(FERNET_PREFIX) :]
    if not rest:
        raise PlatformConfigurationError(f"{source}: {field_name} has an empty fernet: envelope.")
    if ":" in rest:
        # Two-part in-field form: fernet:<cipher>:<token>. Fernet keys and
        # tokens are base64url (never contain ':'), so the first colon is
        # an unambiguous separator.
        cipher, _, token = rest.partition(":")
        if not cipher or not token:
            raise PlatformConfigurationError(
                f"{source}: {field_name} fernet: envelope needs both cipher "
                "and token (fernet:<cipher>:<token>)."
            )
        key = cipher
    else:
        key = os.environ.get(FERNET_ENV_VAR, "")
        if not key:
            raise PlatformConfigurationError(
                f"{source}: {field_name} uses a fernet: envelope but "
                f"{FERNET_ENV_VAR} is not set; refusing to guess."
            )
        token = rest
    _check_fernet_part(source=source, field_name=field_name, part_name="cipher", part=key)
    _check_fernet_part(source=source, field_name=field_name, part_name="token", part=token)
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError:
        raise PlatformConfigurationError(
            f"{source}: {field_name} uses a fernet: envelope but the "
            "cryptography package is unavailable."
        ) from None
    try:
        secret = Fernet(key.encode("utf-8")).decrypt(token.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise PlatformConfigurationError(
            f"{source}: {field_name} fernet: envelope failed to decrypt "
            f"(wrong key or corrupt token): {exc}"
        ) from None
    if not secret:
        raise PlatformConfigurationError(
            f"{source}: {field_name} fernet: envelope decrypted to an empty secret."
        )
    return secret


NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class OAuthSettings(BaseModel):
    """OAuth client-credentials grant for one telemetry endpoint.

    The client carries no ES credential; ``OAuthTokenProvider`` exchanges
    these for a bearer token at ``token_url`` and caches it. ``scope`` is
    optional and passed through verbatim. Token URLs must be https —
    plain http is accepted only for loopback hosts (local stub IdPs in
    tests), never for real deployments.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    token_url: str = Field(..., min_length=1, max_length=1024)
    client_id: str = Field(..., min_length=1, max_length=256)
    client_secret: SecretStr = Field(default=SecretStr(""))
    scope: str | None = Field(default=None, max_length=512)

    @field_validator("token_url")
    @classmethod
    def validate_token_url(cls, v: str) -> str:
        parsed = urlparse(v)
        if parsed.scheme == "https" and parsed.hostname:
            return v
        if parsed.scheme == "http" and parsed.hostname in OAUTH_LOOPBACK_HOSTS:
            return v
        raise ValueError(
            "oauth token_url must be an absolute https URL "
            "(plain http is accepted only for loopback test IdPs)."
        )

    @field_validator("client_secret")
    @classmethod
    def validate_client_secret(cls, v: SecretStr) -> SecretStr:
        if not v.get_secret_value():
            raise ValueError("oauth client_secret must not be empty.")
        return v


class OAuthTokenProvider:
    """Cached client-credentials bearer tokens with skew-based refresh.

    ``token()`` returns the cached token while it remains valid beyond
    ``OAUTH_REFRESH_SKEW_SECONDS``; otherwise exactly one coroutine
    fetches a fresh token under a lock while concurrent callers wait —
    the IdP sees one grant per validity period, never one per request.
    Fetch failures raise typed errors (loud ERROR log, no secrets) and
    are never cached; an expired token is never served.
    """

    def __init__(
        self,
        settings: OAuthSettings,
        *,
        verify_certs: bool = True,
        ca_certs: str | None = None,
        fetch: Callable[[], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._settings = settings
        self._verify_certs = verify_certs
        self._ca_certs = ca_certs
        self._fetch = fetch or self._post_token_request
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    @property
    def _verify(self) -> bool | str:
        if self._ca_certs is not None:
            return self._ca_certs
        return self._verify_certs

    def _usable(self) -> str | None:
        if self._token and time.monotonic() < self._expires_at - OAUTH_REFRESH_SKEW_SECONDS:
            return self._token
        return None

    async def token(self) -> str:
        cached = self._usable()
        if cached is not None:
            return cached
        async with self._lock:
            cached = self._usable()
            if cached is not None:
                return cached
            payload = await self._fetch()
            access_token = payload.get("access_token", "")
            if not isinstance(access_token, str) or not access_token:
                logger.error(
                    "Tenant telemetry token endpoint returned no access token",
                    extra={"context": {"token_host": urlparse(self._settings.token_url).hostname}},
                )
                raise ExecutionError(
                    "Telemetry token endpoint returned no access token.",
                    retryable=False,
                )
            if "expires_in" not in payload:
                logger.error(
                    "Tenant telemetry token endpoint omitted expires_in",
                    extra={"context": {"token_host": urlparse(self._settings.token_url).hostname}},
                )
                raise ExecutionError(
                    "Telemetry token endpoint omitted expires_in; refusing "
                    "to assume a validity period.",
                    retryable=False,
                )
            ttl_raw = payload["expires_in"]
            try:
                ttl = float(ttl_raw)
            except (TypeError, ValueError):
                raise ExecutionError(
                    "Telemetry token endpoint returned a non-numeric expires_in.",
                    retryable=False,
                ) from None
            if ttl <= 0:
                raise ExecutionError(
                    "Telemetry token endpoint returned a non-positive expires_in.",
                    retryable=False,
                )
            self._token = access_token
            self._expires_at = time.monotonic() + ttl
            logger.info(
                "Tenant telemetry token refreshed",
                extra={
                    "context": {
                        "token_host": urlparse(self._settings.token_url).hostname,
                        "ttl_seconds": ttl,
                    }
                },
            )
            return access_token

    async def _post_token_request(self) -> dict[str, Any]:
        """Client-credentials grant over httpx (short-lived client, no lifecycle)."""
        import httpx

        form: dict[str, str] = {"grant_type": OAUTH_GRANT_TYPE}
        if self._settings.scope:
            form["scope"] = self._settings.scope
        try:
            async with httpx.AsyncClient(verify=self._verify, timeout=30.0) as client:
                response = await client.post(
                    self._settings.token_url,
                    data=form,
                    auth=(
                        self._settings.client_id,
                        self._settings.client_secret.get_secret_value(),
                    ),
                )
        except Exception as exc:
            logger.error(
                "Tenant telemetry token request failed",
                exc_info=exc,
                extra={
                    "context": {
                        "token_host": urlparse(self._settings.token_url).hostname,
                        "error_code": "TOKEN_ENDPOINT_UNREACHABLE",
                    }
                },
            )
            raise ProviderUnavailableException("Telemetry token endpoint is unreachable.") from exc
        if response.status_code in (400, 401, 403):
            logger.error(
                "Tenant telemetry token request rejected",
                extra={
                    "context": {
                        "token_host": urlparse(self._settings.token_url).hostname,
                        "status": response.status_code,
                    }
                },
            )
            raise ExecutionError(
                "Telemetry token endpoint rejected the client credentials.",
                retryable=False,
            )
        if response.status_code >= 400:
            logger.error(
                "Tenant telemetry token request failed",
                extra={
                    "context": {
                        "token_host": urlparse(self._settings.token_url).hostname,
                        "status": response.status_code,
                    }
                },
            )
            raise ProviderUnavailableException(
                f"Telemetry token endpoint returned status {response.status_code}."
            )
        try:
            payload = response.json()
        except ValueError:
            raise ExecutionError(
                "Telemetry token endpoint returned a non-JSON response.",
                retryable=False,
            ) from None
        if not isinstance(payload, dict):
            raise ExecutionError(
                "Telemetry token endpoint returned a malformed response.",
                retryable=False,
            )
        return payload


class OAuthBearerClient:
    """Duck-typed ES client injecting a cached OAuth bearer token per request.

    Wraps a real (unauthenticated) ``AsyncElasticsearch``: every ``search``
    resolves the current token from the provider — cached, refreshed only
    inside the skew window — and issues the request through
    ``client.options(bearer_auth=...)`` on the shared transport, so
    connection pooling is unaffected. Only ``search``/``close`` are
    exposed because that is the adapter's entire client surface.
    """

    def __init__(self, client: Any, provider: OAuthTokenProvider) -> None:
        self._client = client
        self._provider = provider

    async def search(self, *args: Any, **kwargs: Any) -> Any:
        bearer = await self._provider.token()
        return await self._client.options(bearer_auth=bearer).search(*args, **kwargs)

    async def close(self) -> None:
        await self._client.close()


DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_INDEX_PATTERN = "logs-*"
MANAGED_MANUAL = "manual"
MANAGED_AUTO = "auto"


def _env_fragment(value: str) -> str:
    """Normalize a tenant/endpoint name into its ``IAP_ES_*`` var fragment."""
    return re.sub(r"[^A-Z0-9]", "_", value.upper())


class TelemetryEndpoint(BaseModel):
    """One named Elasticsearch deployment homing a set of services.

    ``pattern`` is a comma-separated list of index patterns (mirroring
    ``resolve_index_pattern``): search passes it to Elasticsearch as one
    comma-joined index expression, and fetch accepts a stored index
    matching any segment. The list form matters for data streams, whose
    hits carry the concrete backing index (``.ds-<stream>-*``) rather
    than the stream name searched.

    Auth (exactly one effective kind; precedence api_key > oauth >
    bearer > basic > none, selected kind is logged by name at boot):
    ``user`` + ``password`` basic pair, ``api_key`` (encoded key string),
    ``bearer_token`` (static OAuth/service-account token), or the
    ``oauth_token_url``/``oauth_client_id``/``oauth_client_secret``
    client-credentials trio (plus optional ``oauth_scope``) whose token is
    fetched, cached for the provider-stated validity period, and refreshed
    inside the skew window. Each secret field accepts a ``fernet:<token>``
    envelope decrypted with ``IAP_ES_FERNET_KEY``. TLS: ``verify_certs``
    defaults on; ``ca_certs`` pins a private CA bundle (contradicting
    ``verify_certs = false`` + ``ca_certs`` aborts load).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1, max_length=64, pattern=NAME_PATTERN.pattern)
    url: str = Field(..., min_length=1, max_length=1024)
    user: str | None = Field(default=None, max_length=256)
    password: SecretStr = Field(default=SecretStr(""))
    api_key: SecretStr = Field(default=SecretStr(""))
    bearer_token: SecretStr = Field(default=SecretStr(""))
    oauth_token_url: str | None = Field(default=None, max_length=1024)
    oauth_client_id: str | None = Field(default=None, max_length=256)
    oauth_client_secret: SecretStr = Field(default=SecretStr(""))
    oauth_scope: str | None = Field(default=None, max_length=512)
    verify_certs: bool = Field(default=True)
    ca_certs: str | None = Field(default=None, max_length=1024)
    timeout_seconds: int = Field(default=DEFAULT_TIMEOUT_SECONDS, ge=1, le=300)
    pattern: str = Field(default=DEFAULT_INDEX_PATTERN, min_length=1, max_length=2048)
    services: tuple[str, ...] = Field(..., min_length=1)
    managed_by: str = Field(default=MANAGED_MANUAL, min_length=1, max_length=16)

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"telemetry endpoint url must be an absolute http(s) URL, got {v!r}.")
        if parsed.username or parsed.password:
            raise ValueError(
                "telemetry endpoint url must not embed credentials; "
                "use the user/password or api_key fields."
            )
        return v

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, v: str) -> str:
        segments = [segment.strip() for segment in v.split(",")]
        segments = [segment for segment in segments if segment]
        if not segments:
            raise ValueError("telemetry endpoint pattern must list at least one index pattern.")
        for segment in segments:
            if len(segment) > 256:
                raise ValueError(f"index pattern segment exceeds 256 characters: {segment!r}.")
            if not INDEX_PATTERN_CHARS.fullmatch(segment):
                raise ValueError(
                    f"index pattern segment {segment!r} uses characters outside "
                    "the operator pattern charset."
                )
        return ",".join(segments)

    @property
    def pattern_segments(self) -> tuple[str, ...]:
        """Individual index patterns comprising this endpoint's ``pattern``."""
        return tuple(self.pattern.split(","))

    @field_validator("services")
    @classmethod
    def validate_services(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(s.strip() for s in v if s and s.strip())
        if not cleaned:
            raise ValueError("telemetry endpoint must home at least one service.")
        for service in cleaned:
            if len(service) > 128:
                raise ValueError(f"service name exceeds 128 characters: {service!r}.")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("telemetry endpoint lists a service more than once.")
        return cleaned

    @field_validator("managed_by")
    @classmethod
    def validate_managed_by(cls, v: str) -> str:
        if v not in (MANAGED_MANUAL, MANAGED_AUTO):
            raise ValueError(
                f"managed_by must be '{MANAGED_MANUAL}' or '{MANAGED_AUTO}', got {v!r}."
            )
        return v

    @field_validator("ca_certs")
    @classmethod
    def validate_ca_certs(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        path = Path(v.strip()).expanduser()
        if not path.is_file():
            raise ValueError(f"ca_certs path does not exist or is not a file: {v!r}.")
        return str(path)

    @model_validator(mode="after")
    def validate_auth_coherence(self) -> "TelemetryEndpoint":
        oauth_fields = [
            self.oauth_token_url,
            self.oauth_client_id,
            self.oauth_client_secret.get_secret_value(),
            self.oauth_scope,
        ]
        oauth_set = [field for field in oauth_fields if field]
        if oauth_set and (
            not self.oauth_token_url
            or not self.oauth_client_id
            or not self.oauth_client_secret.get_secret_value()
        ):
            raise ValueError(
                "oauth requires token_url, client_id, and client_secret together "
                "(scope is optional); partial oauth configuration aborts."
            )
        if oauth_set and self.bearer_token.get_secret_value():
            raise ValueError(
                "oauth and bearer_token are two sources for the same bearer "
                "credential with no strength ordering — configure exactly one."
            )
        if self.oauth_token_url:
            # Force OAuthSettings validation at load: a bad token_url must
            # abort here, not on first token fetch.
            try:
                OAuthSettings(
                    token_url=self.oauth_token_url,
                    client_id=self.oauth_client_id or "",
                    client_secret=self.oauth_client_secret,
                    scope=self.oauth_scope,
                )
            except ValueError as exc:
                raise ValueError(f"oauth settings invalid: {exc}") from exc
        return self

    @model_validator(mode="after")
    def validate_tls_coherence(self) -> "TelemetryEndpoint":
        if not self.verify_certs and self.ca_certs is not None:
            raise ValueError(
                "ca_certs is set while verify_certs is false: pinning a CA "
                "bundle that will never be consulted is a misconfiguration."
            )
        return self

    def auth_kind(self) -> str:
        """Effective auth kind: api_key > oauth > bearer > basic > none.

        Precedence (not rejection) follows the established "key wins"
        rule across different schemes; within the bearer family oauth and
        a static bearer_token are mutually exclusive (rejected at load —
        two sources for one credential with no strength ordering).
        The winner is logged by name at boot — never silently ambiguous,
        never silently unauthenticated.
        """
        if self.api_key.get_secret_value():
            return "api_key"
        if self.oauth_token_url:
            return "oauth"
        if self.bearer_token.get_secret_value():
            return "bearer"
        if self.user and self.password.get_secret_value():
            return "basic"
        return "none"

    def oauth_settings(self) -> OAuthSettings | None:
        """Validated OAuth grant settings, or None for non-OAuth endpoints."""
        if not self.oauth_token_url:
            return None
        return OAuthSettings(
            token_url=self.oauth_token_url,
            client_id=self.oauth_client_id or "",
            client_secret=self.oauth_client_secret,
            scope=self.oauth_scope,
        )

    def connection_kwargs(self) -> dict[str, Any]:
        """AsyncElasticsearch constructor kwargs for this endpoint.

        Single construction site for every tenant client (the bootstrap
        factory delegates here), so auth/TLS/timeout mapping is tested
        once. Carries no secret values in keys — values stay SecretStr
        until this call, and callers must never log the result.
        """
        kwargs: dict[str, Any] = {
            "request_timeout": self.timeout_seconds,
            "verify_certs": self.verify_certs,
        }
        if self.ca_certs is not None:
            kwargs["ca_certs"] = self.ca_certs
        kind = self.auth_kind()
        if kind == "api_key":
            kwargs["api_key"] = self.api_key.get_secret_value()
        elif kind == "bearer":
            kwargs["bearer_auth"] = self.bearer_token.get_secret_value()
        elif kind == "basic":
            kwargs["basic_auth"] = (self.user, self.password.get_secret_value())
        # oauth carries no static credential: the bootstrap factory wraps
        # the base client in OAuthBearerClient, which injects the cached
        # token per request. Deliberately no else — "none" stays bare.
        return kwargs


class TenantTelemetryConfig(BaseModel):
    """All telemetry endpoints for one tenant (one operator-scope file)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(..., min_length=1, max_length=128)
    endpoints: tuple[TelemetryEndpoint, ...] = Field(..., min_length=1)

    @field_validator("endpoints")
    @classmethod
    def validate_homing(cls, v: tuple[TelemetryEndpoint, ...]) -> tuple[TelemetryEndpoint, ...]:
        names = [endpoint.name for endpoint in v]
        if len(set(names)) != len(names):
            raise ValueError("telemetry endpoint names must be unique per tenant.")
        seen: dict[str, str] = {}
        for endpoint in v:
            for service in endpoint.services:
                if service in seen:
                    raise ValueError(
                        f"service {service!r} is homed on both "
                        f"{seen[service]!r} and {endpoint.name!r}; every service "
                        "maps to exactly one endpoint."
                    )
                seen[service] = endpoint.name
        return v

    def endpoint_for(self, service: str) -> TelemetryEndpoint | None:
        for endpoint in self.endpoints:
            if service in endpoint.services:
                return endpoint
        return None


def _endpoint_env_prefix(tenant_id: str, endpoint_name: str) -> str:
    return f"{ENV_PREFIX}{_env_fragment(tenant_id)}_{_env_fragment(endpoint_name)}_"


def _apply_env_overrides(
    *, tenant_id: str, raw: dict[str, str], endpoint_name: str, source: str
) -> dict[str, str]:
    """Overlay ``IAP_ES_<TENANT>_<NAME>_*`` vars onto one endpoint's fields.

    Unknown-override detection happens at the tenant level (see
    ``load_tenant_telemetry_configs``): any ``IAP_ES_*`` var that names no
    file-defined endpoint fails closed there, so by the time this runs the
    prefix is known-good.
    """
    prefix = _endpoint_env_prefix(tenant_id, endpoint_name)
    merged = dict(raw)
    field_map = {
        "URL": "url",
        "USER": "user",
        "PASSWORD": "password",
        "API_KEY": "api_key",
        "BEARER_TOKEN": "bearer_token",
        "TIMEOUT": "timeout",
        "PATTERN": "pattern",
        "VERIFY_CERTS": "verify_certs",
        "CA_CERTS": "ca_certs",
        "OAUTH_TOKEN_URL": "oauth_token_url",
        "OAUTH_CLIENT_ID": "oauth_client_id",
        "OAUTH_CLIENT_SECRET": "oauth_client_secret",
        "OAUTH_SCOPE": "oauth_scope",
    }
    for suffix, field in field_map.items():
        value = os.environ.get(f"{prefix}{suffix}")
        if value is not None and value != "":
            merged[field] = value
    if "user" in merged or "password" in merged:
        user = merged.get("user", "")
        password = merged.get("password", "")
        if bool(user) != bool(password):
            raise PlatformConfigurationError(
                f"{source}: endpoint {endpoint_name!r} has a half-configured "
                "basic auth pair (user without password or vice versa); "
                "configure both or neither."
            )
    return merged


def _endpoint_from_section(
    *, tenant_id: str, section: str, items: Mapping[str, str], source: str
) -> TelemetryEndpoint:
    raw = {key.lower(): value.strip() for key, value in items.items()}
    merged = _apply_env_overrides(
        tenant_id=tenant_id, raw=raw, endpoint_name=section, source=source
    )
    services = tuple(part.strip() for part in merged.get("services", "").split(",") if part.strip())
    try:
        timeout = int(merged.get("timeout", str(DEFAULT_TIMEOUT_SECONDS)))
    except ValueError:
        raise PlatformConfigurationError(
            f"{source}: endpoint {section!r} has a non-integer timeout {merged.get('timeout')!r}."
        ) from None
    try:
        verify_raw = merged.get("verify_certs", "true").strip().lower()
        if verify_raw in VERIFY_TRUE:
            verify_certs = True
        elif verify_raw in VERIFY_FALSE:
            verify_certs = False
        else:
            raise PlatformConfigurationError(
                f"{source}: endpoint {section!r} has a non-boolean "
                f"verify_certs {merged.get('verify_certs')!r} "
                "(true/false/yes/no/1/0/on/off)."
            )
        return TelemetryEndpoint(
            name=section,
            url=merged.get("url", ""),
            user=merged.get("user") or None,
            password=SecretStr(
                decrypt_secret_value(
                    field_name="password",
                    value=merged.get("password", ""),
                    source=source,
                )
            ),
            api_key=SecretStr(
                decrypt_secret_value(
                    field_name="api_key",
                    value=merged.get("api_key", ""),
                    source=source,
                )
            ),
            bearer_token=SecretStr(
                decrypt_secret_value(
                    field_name="bearer_token",
                    value=merged.get("bearer_token", ""),
                    source=source,
                )
            ),
            oauth_token_url=merged.get("oauth_token_url") or None,
            oauth_client_id=merged.get("oauth_client_id") or None,
            oauth_client_secret=SecretStr(
                decrypt_secret_value(
                    field_name="oauth_client_secret",
                    value=merged.get("oauth_client_secret", ""),
                    source=source,
                )
            ),
            oauth_scope=merged.get("oauth_scope") or None,
            verify_certs=verify_certs,
            ca_certs=merged.get("ca_certs") or None,
            timeout_seconds=timeout,
            pattern=merged.get("pattern", DEFAULT_INDEX_PATTERN) or DEFAULT_INDEX_PATTERN,
            services=services,
            managed_by=merged.get("managed_by", MANAGED_MANUAL) or MANAGED_MANUAL,
        )
    except ValueError as exc:
        raise PlatformConfigurationError(
            f"{source}: endpoint {section!r} is invalid: {exc}"
        ) from exc


def load_tenant_telemetry_configs(
    telemetry_dir: str | os.PathLike[str] | None,
) -> dict[str, TenantTelemetryConfig]:
    """Load every ``<dir>/<tenant>/telemetry.conf``; fail closed on malformation.

    Empty/unset dir (or a dir with no tenant files) means no tenant
    telemetry is configured — the caller keeps the global default route.
    A present-but-malformed file aborts with ``PlatformConfigurationError``
    (F-001 spirit: a half-configured evidence layer must never serve).
    """
    if not telemetry_dir or not str(telemetry_dir).strip():
        return {}
    root = Path(telemetry_dir).expanduser()
    if not root.is_dir():
        logger.info(
            "Tenant telemetry directory absent; no per-tenant endpoints",
            extra={"telemetry_dir": str(root)},
        )
        return {}
    configs: dict[str, TenantTelemetryConfig] = {}
    for tenant_path in sorted(root.iterdir()):
        if not tenant_path.is_dir():
            continue
        conf_path = tenant_path / TELEMETRY_FILE_NAME
        if not conf_path.is_file():
            continue
        tenant_id = tenant_path.name
        source = f"{conf_path} (tenant {tenant_id!r})"
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(conf_path, encoding="utf-8")
        except configparser.Error as exc:
            raise PlatformConfigurationError(
                f"{source}: cannot parse telemetry.conf: {exc}"
            ) from exc
        if not parser.sections():
            raise PlatformConfigurationError(f"{source}: telemetry.conf defines no endpoints.")
        endpoints: list[TelemetryEndpoint] = []
        for section in parser.sections():
            if not NAME_PATTERN.match(section):
                raise PlatformConfigurationError(
                    f"{source}: invalid endpoint name {section!r} "
                    "(letters, digits, '_' and '-', must start alphanumerically)."
                )
            endpoints.append(
                _endpoint_from_section(
                    tenant_id=tenant_id,
                    section=section,
                    items=dict(parser.items(section)),
                    source=source,
                )
            )
        try:
            configs[tenant_id] = TenantTelemetryConfig(
                tenant_id=tenant_id, endpoints=tuple(endpoints)
            )
        except ValueError as exc:
            raise PlatformConfigurationError(f"{source}: {exc}") from exc
        _reject_unknown_env_overrides(tenant_id=tenant_id, config=configs[tenant_id])
        logger.info(
            "Tenant telemetry endpoints loaded",
            extra={
                "tenant_id": tenant_id,
                "endpoints": sorted(e.name for e in endpoints),
            },
        )
    return configs


def _reject_unknown_env_overrides(*, tenant_id: str, config: TenantTelemetryConfig) -> None:
    """Fail closed on ``IAP_ES_<TENANT>_*`` vars naming no file-defined endpoint.

    The file is the source of truth for endpoint existence: an override for
    an unknown endpoint is an operator typo (or a removed endpoint still
    referenced in the environment), never a reason to conjure infrastructure.
    """
    tenant_fragment = _env_fragment(tenant_id)
    prefix = f"{ENV_PREFIX}{tenant_fragment}_"
    known = {_env_fragment(endpoint.name) for endpoint in config.endpoints}
    for var in os.environ:
        if not var.startswith(prefix):
            continue
        remainder = var[len(prefix) :]
        # Endpoint names may themselves contain underscores (normalized
        # from '-' or native '_'), while field suffixes never do — split
        # at the last underscore so `GHOST_ES_URL` reads as endpoint
        # `GHOST_ES` + field `URL`, not endpoint `GHOST` + junk.
        endpoint_fragment, sep, field = remainder.rpartition("_")
        if not sep or field not in ENV_OVERRIDABLE_FIELDS:
            continue
        if endpoint_fragment not in known:
            raise PlatformConfigurationError(
                f"IAP_ES override {var} names unknown telemetry endpoint "
                f"{endpoint_fragment!r} for tenant {tenant_id!r}; endpoints "
                "are defined in the tenant telemetry.conf file."
            )


def merge_managed_defaults(
    existing: TenantTelemetryConfig | None,
    defaults: Iterable[TelemetryEndpoint],
    *,
    tenant_id: str,
) -> TenantTelemetryConfig:
    """Discovery-writer merge: manual entries are never clobbered.

    Rules: existing ``manual`` endpoints always win (same-name defaults are
    dropped); existing ``auto`` endpoints are refreshed by same-name
    defaults and removed when the new discovery no longer reports them;
    genuinely new defaults are appended. Used by the log-layout discovery
    writer; unit-tested here so the contract holds before any writer exists.
    """
    if existing is not None and existing.tenant_id != tenant_id:
        raise PlatformConfigurationError("tenant mismatch in managed-defaults merge.")
    manual = {
        e.name: e
        for e in (existing.endpoints if existing else ())
        if e.managed_by == MANAGED_MANUAL
    }
    refreshed: dict[str, TelemetryEndpoint] = {}
    for default in defaults:
        if default.name in manual:
            continue
        if default.managed_by != MANAGED_AUTO:
            raise PlatformConfigurationError(
                f"discovery default {default.name!r} must carry managed_by='auto'."
            )
        refreshed[default.name] = default
    merged = list(manual.values()) + [refreshed[name] for name in sorted(refreshed)]
    if not merged:
        raise PlatformConfigurationError(
            f"tenant {tenant_id!r}: merge produced zero endpoints; "
            "refusing to write an empty telemetry.conf."
        )
    return TenantTelemetryConfig(tenant_id=tenant_id, endpoints=tuple(merged))


@dataclass(frozen=True)
class ResolvedTenantEndpoint:
    """One request's home endpoint: connection, index pattern, timeout."""

    tenant_id: str
    endpoint_name: str
    client: Any
    index_pattern: str
    request_timeout: float


class TenantTelemetryRouter:
    """Service → home-endpoint resolution over loaded tenant configs.

    The adapter holds one router (built once at boot with real clients);
    per request it resolves (tenant, service identity) to exactly one
    endpoint connection. Zero matches fail closed with
    ``ProviderNotConfiguredError``; more than one is an operator
    misconfiguration that escaped load-time validation and fails closed
    with ``PlatformConfigurationError``.
    """

    def __init__(self, resolved: Mapping[str, Mapping[str, ResolvedTenantEndpoint]]) -> None:
        self._by_tenant_service = {tenant: dict(services) for tenant, services in resolved.items()}

    def has_tenant(self, tenant_id: str) -> bool:
        return tenant_id in self._by_tenant_service

    def endpoints_for_tenant(self, tenant_id: str) -> list[ResolvedTenantEndpoint]:
        """Distinct home endpoints for one tenant (fetch-path resolution)."""
        seen: dict[str, ResolvedTenantEndpoint] = {}
        for handle in self._by_tenant_service.get(tenant_id, {}).values():
            seen.setdefault(handle.endpoint_name, handle)
        return list(seen.values())

    def resolve(self, tenant_id: str, identity_names: Iterable[str]) -> ResolvedTenantEndpoint:
        names = [name for name in identity_names if name]
        if tenant_id not in self._by_tenant_service:
            raise ProviderNotConfiguredError(
                f"No telemetry endpoints configured for tenant '{tenant_id}'"
            )
        homed = self._by_tenant_service[tenant_id]
        matches = {homed[name].endpoint_name: homed[name] for name in names if name in homed}
        if not matches:
            logger.error(
                "Tenant telemetry request names no homed service",
                extra={"tenant_id": tenant_id},
            )
            raise ProviderNotConfiguredError(
                f"Tenant '{tenant_id}' configures telemetry but homes none of "
                "the requested services; refusing to query an unrelated deployment."
            )
        if len(matches) > 1:
            logger.error(
                "Tenant telemetry service homed on multiple endpoints",
                extra={"tenant_id": tenant_id, "endpoints": sorted(matches)},
            )
            raise PlatformConfigurationError(
                f"Tenant '{tenant_id}' homes the requested service on multiple "
                f"endpoints {sorted(matches)}; every service maps to exactly one."
            )
        return next(iter(matches.values()))


def build_tenant_client(endpoint: TelemetryEndpoint) -> Any:
    """Construct one endpoint's ES client (shared by bootstrap and verification).

    Single construction site for every tenant client: base client from
    ``connection_kwargs`` (auth/TLS/timeout), wrapped in
    ``OAuthBearerClient`` for OAuth endpoints. Secret values travel only
    as arguments — callers must never log the result.
    """
    from elasticsearch import AsyncElasticsearch

    base = AsyncElasticsearch(endpoint.url, **endpoint.connection_kwargs())
    oauth = endpoint.oauth_settings()
    if oauth is None:
        return base
    return OAuthBearerClient(
        base,
        OAuthTokenProvider(
            oauth,
            verify_certs=endpoint.verify_certs,
            ca_certs=endpoint.ca_certs,
        ),
    )


def build_tenant_router(
    configs: Mapping[str, TenantTelemetryConfig],
    client_factory: Callable[[TelemetryEndpoint], Any],
    *,
    query_timeout_seconds: float,
) -> TenantTelemetryRouter | None:
    """Build one router over all tenants; None when nothing is configured.

    ``query_timeout_seconds`` is the adapter's provider query timeout: an
    endpoint whose request timeout does not strictly exceed it is refused
    (the outer boundary would otherwise fire first and the query timeout
    could never take effect — same coherence rule as the global adapter).
    """
    if not configs:
        return None
    resolved: dict[str, dict[str, ResolvedTenantEndpoint]] = {}
    for tenant_id, config in configs.items():
        homed: dict[str, ResolvedTenantEndpoint] = {}
        for endpoint in config.endpoints:
            if not endpoint.timeout_seconds > query_timeout_seconds:
                raise PlatformConfigurationError(
                    f"tenant {tenant_id!r} endpoint {endpoint.name!r}: request "
                    f"timeout ({endpoint.timeout_seconds}s) must strictly exceed "
                    f"the provider query timeout ({query_timeout_seconds:g}s)."
                )
            try:
                client = client_factory(endpoint)
            except Exception as exc:
                raise PlatformConfigurationError(
                    f"tenant {tenant_id!r} endpoint {endpoint.name!r}: cannot "
                    f"construct client: {exc}"
                ) from exc
            handle = ResolvedTenantEndpoint(
                tenant_id=tenant_id,
                endpoint_name=endpoint.name,
                client=client,
                index_pattern=endpoint.pattern,
                request_timeout=float(endpoint.timeout_seconds),
            )
            for service in endpoint.services:
                homed[service] = handle
        resolved[tenant_id] = homed
    return TenantTelemetryRouter(resolved)


def endpoint_index_allowed(index: str, pattern: str) -> bool:
    """Whether a stored/provider index falls under an endpoint's pattern.

    The pattern may be comma-separated; any matching segment allows it.
    """
    return any(
        fnmatch.fnmatchcase(index, segment) for segment in pattern.split(",") if segment.strip()
    )
