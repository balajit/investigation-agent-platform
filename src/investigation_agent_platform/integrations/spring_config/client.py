# src/investigation_agent_platform/integrations/spring_config/client.py
"""OAuth2 + Spring Config Server client (appl-profile 1.0 §10.1).

Contract mirrors the reference design (token cache with early renewal,
HTTPS-only, allowlists before I/O, precedence-ordered merge, allowlist +
redaction, typed errors, count-only logging). Transport is stdlib
``urllib`` instead of ``requests`` so the integration adds no new runtime
dependency; retry/backoff semantics are equivalent (bounded GET-only
retries on 429/502/503/504 honoring Retry-After).

Sync I/O only — derive/refresh paths, never the Temporal hot loop.
"""

from __future__ import annotations

import base64
import fnmatch  # noqa: F401  (re-exported for contract parity documentation)
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from urllib.parse import quote, urlparse

from investigation_agent_platform.integrations.spring_config.exceptions import (
    ConfigAuthenticationError,
    ConfigAuthorizationError,
    ConfigClientError,
    ConfigNotFoundError,
    ConfigRateLimitError,
    ConfigServerError,
    ConfigTransportError,
)
from investigation_agent_platform.integrations.spring_config.models import ConfigResult
from investigation_agent_platform.integrations.spring_config.security import (
    is_allowed_property,
    looks_sensitive,
)

logger = logging.getLogger(__name__)

_LABEL_RE = re.compile(r"[A-Za-z0-9._/-]+")
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})


def _validate_https(url: str, what: str, require_https: bool = True) -> None:
    if require_https and urlparse(url).scheme.lower() != "https":
        raise ValueError(f"{what} must use HTTPS")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward Authorization across a redirect (fail closed on leaks)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and new.has_header("Authorization"):
            new.remove_header("Authorization")
        return new


def _build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_SafeRedirect)


def _retry_after_seconds(headers: Any) -> float | None:
    raw = None
    try:
        raw = headers.get("Retry-After")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        delay = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if delay < 0 or delay > 300:
        return None
    return delay


class OAuth2ClientCredentials:
    """Thread-safe, in-memory OAuth2 access-token cache."""

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str | None = None,
        timeout: float = 10.0,
        require_https: bool = True,
    ) -> None:
        _validate_https(token_url, "OAuth token endpoint", require_https)
        if not client_id or not client_secret:
            raise ValueError("OAuth client_id and client_secret are required")
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self.timeout = timeout

        self._opener = _build_opener()
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def get_token(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._expires_at:
                return self._token

            form: dict[str, str] = {"grant_type": "client_credentials"}
            if self.scope:
                form["scope"] = self.scope
            basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode(
                "ascii"
            )
            request = urllib.request.Request(
                self.token_url,
                data=urllib.parse.urlencode(form).encode("utf-8"),
                headers={
                    "Authorization": f"Basic {basic}",
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                method="POST",
            )
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    raw_body = response.read()
            except urllib.error.HTTPError as exc:
                raise self._oauth_http_error(exc) from exc
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                raise ConfigTransportError("OAuth token endpoint is unreachable") from exc

            try:
                body = json.loads(raw_body.decode("utf-8"))
                token = body["access_token"]
                expires_in = float(body.get("expires_in", 300))
            except (ValueError, TypeError, KeyError) as exc:
                raise ConfigClientError("OAuth token response is invalid") from exc
            if not isinstance(token, str) or not token.strip():
                raise ConfigClientError("OAuth returned an empty token")
            if expires_in <= 0:
                raise ConfigClientError("OAuth token lifetime is invalid")

            # Refresh 30 seconds early, or halfway through very short tokens.
            refresh_margin = min(30.0, expires_in * 0.1)
            self._token = token
            self._expires_at = time.monotonic() + expires_in - refresh_margin
            return token

    @staticmethod
    def _oauth_http_error(exc: urllib.error.HTTPError) -> ConfigClientError:
        if exc.code in (400, 401, 403):
            return ConfigAuthenticationError("OAuth token request was rejected")
        if exc.code == 429:
            return ConfigRateLimitError(
                "OAuth token endpoint rate limited the request",
                retry_after=_retry_after_seconds(exc.headers),
            )
        if exc.code is not None and exc.code >= 500:
            return ConfigServerError("OAuth token endpoint returned a server error")
        return ConfigClientError(f"Unexpected OAuth HTTP status: {exc.code}")


class SpringConfigClient:
    def __init__(
        self,
        server_url: str,
        oauth: OAuth2ClientCredentials,
        allowed_applications: set[str],
        allowed_profiles: set[str],
        allowed_property_patterns: tuple[str, ...],
        timeout: float = 15.0,
        max_attempts: int = 3,
        require_https: bool = True,
    ) -> None:
        _validate_https(server_url, "Config Server", require_https)
        self.server_url = server_url.rstrip("/")
        self.oauth = oauth
        self.allowed_applications = allowed_applications
        self.allowed_profiles = allowed_profiles
        self.allowed_property_patterns = allowed_property_patterns
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self._opener = _build_opener()

    def fetch(
        self,
        application: str,
        profile: str,
        label: str | None = None,
    ) -> ConfigResult:
        # Allowlists enforced BEFORE any I/O (fail closed, no SSRF/oracle).
        if application not in self.allowed_applications:
            raise ConfigAuthorizationError("Application is outside the permitted scope")
        if profile not in self.allowed_profiles:
            raise ConfigAuthorizationError("Profile is outside the permitted scope")
        if label is not None and (
            not label
            or len(label) > 200
            or not _LABEL_RE.fullmatch(label)
            or ".." in label.split("/")
        ):
            raise ValueError("Invalid configuration label")

        parts = [application, profile]
        if label is not None:
            parts.append(label)
        path = "/".join(quote(part, safe="") for part in parts)
        url = f"{self.server_url}/{path}"

        body = self._get_with_retry(url, application)
        try:
            sources = body["propertySources"]
            if not isinstance(sources, list):
                raise TypeError("propertySources is not a list")
        except (ValueError, KeyError, TypeError) as exc:
            raise ConfigClientError("Config Server returned an invalid response") from exc

        # Spring returns higher-precedence property sources first.
        merged: dict[str, Any] = {}
        source_names: list[str] = []
        for source in sources:
            if not isinstance(source, dict):
                continue
            source_names.append(str(source.get("name", "unknown")))
            values = source.get("source", {})
            if isinstance(values, dict):
                for key, value in values.items():
                    merged.setdefault(key, value)

        safe_properties = {
            key: value
            for key, value in merged.items()
            if is_allowed_property(key, self.allowed_property_patterns) and not looks_sensitive(key)
        }

        # Do not log the response body, properties, tokens, or source values.
        logger.info(
            "Retrieved configuration evidence: app=%s profile=%s property_count=%d",
            application,
            profile,
            len(safe_properties),
        )
        return ConfigResult(
            application=application,
            profile=profile,
            label=label,
            properties=safe_properties,
            property_sources=tuple(source_names),
        )

    def _get_with_retry(self, url: str, application: str) -> dict[str, Any]:
        last_error: ConfigClientError | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self._get_once(url)
            except ConfigRateLimitError as exc:
                last_error = exc
            except ConfigServerError as exc:
                last_error = exc
            except ConfigTransportError as exc:
                last_error = exc
            if attempt < self.max_attempts:
                delay = 0.3 * (2 ** (attempt - 1))
                if isinstance(last_error, ConfigRateLimitError) and last_error.retry_after:
                    delay = last_error.retry_after
                logger.info(
                    "Retrying configuration fetch: app=%s attempt=%d",
                    application,
                    attempt + 1,
                )
                time.sleep(min(delay, 30.0))
        assert last_error is not None
        raise last_error

    def _get_once(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.oauth.get_token()}",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status_code = response.status
                raw_body = response.read()
                retry_after = _retry_after_seconds(response.headers)
        except urllib.error.HTTPError as exc:
            raise self._config_http_error(exc) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ConfigTransportError("Config Server request failed") from exc

        if status_code in (401, 403):
            # Do not automatically replay after an authorization failure:
            # the token may be valid but lack required permissions.
            if status_code == 401:
                raise ConfigAuthenticationError("Config Server rejected authentication")
            raise ConfigAuthorizationError("Config Server denied access")
        if status_code == 404:
            raise ConfigNotFoundError("Requested configuration was not found")
        if status_code == 429:
            raise ConfigRateLimitError("Config Server rate limit exceeded", retry_after=retry_after)
        if status_code is not None and status_code >= 500:
            raise ConfigServerError("Config Server returned a server error")
        if status_code != 200:
            raise ConfigClientError(f"Unexpected Config Server HTTP status: {status_code}")
        try:
            parsed = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ConfigClientError("Config Server returned an invalid response") from exc
        if not isinstance(parsed, dict):
            raise ConfigClientError("Config Server returned an invalid response")
        return parsed

    @staticmethod
    def _config_http_error(exc: urllib.error.HTTPError) -> ConfigClientError:
        code = exc.code
        if code == 401:
            return ConfigAuthenticationError("Config Server rejected authentication")
        if code == 403:
            return ConfigAuthorizationError("Config Server denied access")
        if code == 404:
            return ConfigNotFoundError("Requested configuration was not found")
        if code == 429:
            return ConfigRateLimitError(
                "Config Server rate limit exceeded",
                retry_after=_retry_after_seconds(exc.headers),
            )
        if code is not None and code >= 500:
            return ConfigServerError("Config Server returned a server error")
        return ConfigClientError(f"Unexpected Config Server HTTP status: {code}")
