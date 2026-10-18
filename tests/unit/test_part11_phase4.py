# tests/unit/test_part11_phase4.py
"""Part 11 Phase 11.4 tests: outbound credential providers.

Covers: token cache hit/refresh, expiry, concurrent single-flight refresh,
static round-trip, cross-tenant isolation, SSRF/TLS validation, sanitized
failures, manifest registration, and audit emission. The token endpoint is
mocked (no network); DNS is stubbed for endpoint-guard tests.
"""

from __future__ import annotations

import socket
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from investigation_agent_platform.application.extensions.registries import (
    CredentialProviderRegistry,
)
from investigation_agent_platform.domain.common.exceptions import CredentialProviderError
from investigation_agent_platform.domain.common.extension import CapabilityScope
from investigation_agent_platform.infrastructure.security.endpoint_guard import (
    EndpointPolicy,
    validate_endpoint,
)
from investigation_agent_platform.infrastructure.security.oauth2_client_credentials import (
    OAuth2ClientCredentialsProvider,
    OAuth2ProviderConfig,
    oauth2_manifest,
)
from investigation_agent_platform.infrastructure.security.static_token_provider import (
    static_manifest,
)
from investigation_agent_platform.ports.security.outbound_credentials import AccessToken

MODULE = "investigation_agent_platform.infrastructure.security.oauth2_client_credentials"

_PUBLIC_ADDR = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


def _scope(tenant: str = "tenant-a", app: str | None = "app-1") -> CapabilityScope:
    return CapabilityScope(tenant_id=tenant, application_id=app)


def _config(**overrides) -> OAuth2ProviderConfig:
    base: dict = {
        "provider_id": "oauth-main",
        "token_endpoint": "https://auth.example.com/oauth/token",
        "client_id": "client-1",
        "client_secret_env": "IAP_TEST_OAUTH_SECRET",
        "allowed_hosts": frozenset({"auth.example.com"}),
    }
    base.update(overrides)
    return OAuth2ProviderConfig(**base)


def _dns_public(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: list(_PUBLIC_ADDR))


def _provider(
    monkeypatch: pytest.MonkeyPatch, secret: str = "s3cret", **overrides
) -> OAuth2ClientCredentialsProvider:
    monkeypatch.setenv("IAP_TEST_OAUTH_SECRET", secret)
    _dns_public(monkeypatch)
    return OAuth2ClientCredentialsProvider(
        _config(**overrides),
        allowed_env_names=frozenset({"IAP_TEST_OAUTH_SECRET"}),
    )


def _token_payload(token: str = "tok-1", ttl: int = 600) -> dict:
    return {"access_token": token, "token_type": "Bearer", "expires_in": ttl}


# ===========================================================================
# AccessToken usability
# ===========================================================================


class TestAccessToken:
    def test_usable_with_margin(self) -> None:
        token = AccessToken(
            token=SecretStr("x"),
            expires_at=datetime.now(UTC) + timedelta(seconds=300),
            provider_id="p",
        )
        assert token.is_usable() is True

    def test_unusable_inside_floor(self) -> None:
        token = AccessToken(
            token=SecretStr("x"),
            expires_at=datetime.now(UTC) + timedelta(seconds=30),
            provider_id="p",
        )
        assert token.is_usable() is False


# ===========================================================================
# OAuth2 cache behavior (mocked token endpoint)
# ===========================================================================


class TestOAuth2Cache:
    @pytest.mark.asyncio
    async def test_cache_hit_avoids_refetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _provider(monkeypatch)
        with patch(f"{MODULE}._post_token_request", return_value=(200, _token_payload())) as mock:
            first = await provider.get_token(_scope(), "oauth-main")
            second = await provider.get_token(_scope(), "oauth-main")
            assert mock.call_count == 1
            assert first.token.get_secret_value() == "tok-1"
            assert second.token.get_secret_value() == "tok-1"

    @pytest.mark.asyncio
    async def test_expiry_triggers_refresh(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _provider(monkeypatch)
        calls = {"n": 0}

        def _fetch(request, timeout, limit=None, **kwargs):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            return 200, _token_payload(token=f"tok-{calls['n']}")

        with patch(f"{MODULE}._post_token_request", side_effect=_fetch):
            first = await provider.get_token(_scope(), "oauth-main")
            assert first.token.get_secret_value() == "tok-1"
            # Force the refresh watermark into the past.
            key = provider._cache_key(_scope())
            provider._cache[key].refresh_at = time.monotonic() - 1
            second = await provider.get_token(_scope(), "oauth-main")
            assert second.token.get_secret_value() == "tok-2"
            assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_concurrent_callers_single_fetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        provider = _provider(monkeypatch)
        calls = {"n": 0}

        def _slow_fetch(request, timeout, limit=None, **kwargs):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            time.sleep(0.05)
            return 200, _token_payload()

        with patch(f"{MODULE}._post_token_request", side_effect=_slow_fetch):
            results = await asyncio.gather(
                *[provider.get_token(_scope(), "oauth-main") for _ in range(10)]
            )
            assert calls["n"] == 1
            assert {r.token.get_secret_value() for r in results} == {"tok-1"}

    @pytest.mark.asyncio
    async def test_invalidate_forces_refetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _provider(monkeypatch)
        with patch(f"{MODULE}._post_token_request", return_value=(200, _token_payload())) as mock:
            await provider.get_token(_scope(), "oauth-main")
            provider.invalidate(_scope())
            await provider.get_token(_scope(), "oauth-main")
            assert mock.call_count == 2

    @pytest.mark.asyncio
    async def test_unknown_provider_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _provider(monkeypatch)
        with pytest.raises(CredentialProviderError):
            await provider.get_token(_scope(), "no-such-provider")


# ===========================================================================
# OAuth2 response validation
# ===========================================================================


class TestOAuth2Responses:
    @pytest.mark.asyncio
    async def test_401_invalidates_and_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _provider(monkeypatch)
        with patch(f"{MODULE}._post_token_request", return_value=(401, {})):
            with pytest.raises(CredentialProviderError):
                await provider.get_token(_scope(), "oauth-main")
            key = provider._cache_key(_scope())
            assert key not in provider._cache

    @pytest.mark.asyncio
    async def test_server_error_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _provider(monkeypatch)
        with patch(f"{MODULE}._post_token_request", return_value=(500, {})):
            with pytest.raises(CredentialProviderError):
                await provider.get_token(_scope(), "oauth-main")

    @pytest.mark.asyncio
    async def test_non_bearer_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _provider(monkeypatch)
        payload = {"access_token": "x", "token_type": "MAC", "expires_in": 600}
        with patch(f"{MODULE}._post_token_request", return_value=(200, payload)):
            with pytest.raises(CredentialProviderError):
                await provider.get_token(_scope(), "oauth-main")

    @pytest.mark.asyncio
    async def test_missing_token_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _provider(monkeypatch)
        with patch(f"{MODULE}._post_token_request", return_value=(200, {})):
            with pytest.raises(CredentialProviderError):
                await provider.get_token(_scope(), "oauth-main")

    @pytest.mark.asyncio
    async def test_short_ttl_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _provider(monkeypatch)
        with patch(f"{MODULE}._post_token_request", return_value=(200, _token_payload(ttl=30))):
            with pytest.raises(CredentialProviderError):
                await provider.get_token(_scope(), "oauth-main")

    @pytest.mark.asyncio
    async def test_invalid_expiry_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _provider(monkeypatch)
        payload = {"access_token": "x", "token_type": "Bearer", "expires_in": "soon"}
        with patch(f"{MODULE}._post_token_request", return_value=(200, payload)):
            with pytest.raises(CredentialProviderError):
                await provider.get_token(_scope(), "oauth-main")

    @pytest.mark.asyncio
    async def test_network_failure_sanitized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _provider(monkeypatch, secret="super-secret-value")
        with patch(f"{MODULE}._post_token_request", side_effect=OSError("conn refused")):
            with pytest.raises(CredentialProviderError) as exc_info:
                await provider.get_token(_scope(), "oauth-main")
            blob = f"{exc_info.value} {exc_info.value.details}"
            assert "super-secret-value" not in blob
            assert "s3cret" not in blob

    @pytest.mark.asyncio
    async def test_scope_and_audience_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _provider(monkeypatch, scope="read write", audience="https://api.example.com")
        seen: dict = {}

        def _capture(request, timeout, limit=None, **kwargs):  # type: ignore[no-untyped-def]
            seen["body"] = request.data.decode("ascii")
            seen["content_type"] = request.get_header("Content-type")
            return 200, _token_payload()

        with patch(f"{MODULE}._post_token_request", side_effect=_capture):
            await provider.get_token(_scope(), "oauth-main")
        assert "scope=read+write" in seen["body"]
        assert "audience=https%3A%2F%2Fapi.example.com" in seen["body"]
        assert seen["content_type"] == "application/x-www-form-urlencoded"
        assert "super-secret" not in seen["body"]

    @pytest.mark.asyncio
    async def test_invalid_audience_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pydantic import ValidationError

        _dns_public(monkeypatch)
        with pytest.raises(ValidationError):
            OAuth2ClientCredentialsProvider(
                _config(audience="x" * 600),
                allowed_env_names=frozenset({"IAP_TEST_OAUTH_SECRET"}),
            )


# ===========================================================================
# Cross-tenant isolation
# ===========================================================================


class TestTenantIsolation:
    @pytest.mark.asyncio
    async def test_same_provider_id_never_shares_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = _provider(monkeypatch)
        calls = {"n": 0}

        def _fetch(request, timeout, limit=None, **kwargs):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            return 200, _token_payload(token=f"tok-{calls['n']}")

        with patch(f"{MODULE}._post_token_request", side_effect=_fetch):
            token_a = await provider.get_token(_scope("tenant-a"), "oauth-main")
            token_b = await provider.get_token(_scope("tenant-b"), "oauth-main")
            assert token_a.token.get_secret_value() == "tok-1"
            assert token_b.token.get_secret_value() == "tok-2"
            assert calls["n"] == 2
            # Repeat hits stay scoped.
            again = await provider.get_token(_scope("tenant-a"), "oauth-main")
            assert again.token.get_secret_value() == "tok-1"
            assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_application_scoping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        provider = _provider(monkeypatch)
        with patch(f"{MODULE}._post_token_request", return_value=(200, _token_payload())) as mock:
            await provider.get_token(_scope("tenant-a", "app-1"), "oauth-main")
            await provider.get_token(_scope("tenant-a", "app-2"), "oauth-main")
            assert mock.call_count == 2


# ===========================================================================
# Static provider
# ===========================================================================


class TestStaticProvider:
    @pytest.mark.asyncio
    async def test_round_trip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from investigation_agent_platform.infrastructure.security.static_token_provider import (
            StaticTokenProvider,
        )

        monkeypatch.setenv("IAP_TEST_STATIC_TOKEN", "static-abc")
        provider = StaticTokenProvider(
            "static-dev",
            "IAP_TEST_STATIC_TOKEN",
            allowed_env_names=frozenset({"IAP_TEST_STATIC_TOKEN"}),
        )
        token = await provider.get_token(_scope(), "static-dev")
        assert token.token.get_secret_value() == "static-abc"
        assert token.is_usable() is True
        provider.invalidate(_scope())

    @pytest.mark.asyncio
    async def test_unset_secret_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from investigation_agent_platform.infrastructure.security.static_token_provider import (
            StaticTokenProvider,
        )

        monkeypatch.delenv("IAP_TEST_STATIC_TOKEN", raising=False)
        provider = StaticTokenProvider(
            "static-dev",
            "IAP_TEST_STATIC_TOKEN",
            allowed_env_names=frozenset({"IAP_TEST_STATIC_TOKEN"}),
        )
        with pytest.raises(CredentialProviderError):
            await provider.get_token(_scope(), "static-dev")

    @pytest.mark.asyncio
    async def test_non_allowlisted_secret_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from investigation_agent_platform.infrastructure.security.static_token_provider import (
            StaticTokenProvider,
        )

        monkeypatch.setenv("IAP_TEST_STATIC_TOKEN", "static-abc")
        provider = StaticTokenProvider(
            "static-dev", "IAP_TEST_STATIC_TOKEN", allowed_env_names=frozenset()
        )
        with pytest.raises(CredentialProviderError):
            await provider.get_token(_scope(), "static-dev")

    @pytest.mark.asyncio
    async def test_unknown_provider_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from investigation_agent_platform.infrastructure.security.static_token_provider import (
            StaticTokenProvider,
        )

        monkeypatch.setenv("IAP_TEST_STATIC_TOKEN", "static-abc")
        provider = StaticTokenProvider(
            "static-dev",
            "IAP_TEST_STATIC_TOKEN",
            allowed_env_names=frozenset({"IAP_TEST_STATIC_TOKEN"}),
        )
        with pytest.raises(CredentialProviderError):
            await provider.get_token(_scope(), "other")


# ===========================================================================
# SSRF / TLS endpoint validation
# ===========================================================================


class TestEndpointGuard:
    def test_http_rejected_by_default(self) -> None:
        with pytest.raises(CredentialProviderError):
            validate_endpoint("http://auth.example.com/token", EndpointPolicy(), purpose="t")

    def test_forbidden_scheme_rejected(self) -> None:
        with pytest.raises(CredentialProviderError):
            validate_endpoint("ftp://auth.example.com/token", EndpointPolicy(), purpose="t")

    def test_non_allowlisted_host_rejected(self) -> None:
        policy = EndpointPolicy(allowed_hosts=frozenset({"good.example.com"}))
        with pytest.raises(CredentialProviderError):
            validate_endpoint("https://evil.example.com/token", policy, purpose="t")

    def test_metadata_hosts_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _dns_public(monkeypatch)
        for host in ("169.254.169.254", "100.100.100.200"):
            with pytest.raises(CredentialProviderError):
                validate_endpoint(f"https://{host}/latest", EndpointPolicy(), purpose="t")

    def test_loopback_rejected_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))],
        )
        with pytest.raises(CredentialProviderError):
            validate_endpoint("https://localhost/token", EndpointPolicy(), purpose="t")

    def test_loopback_allowed_with_explicit_policy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))],
        )
        url = validate_endpoint(
            "https://localhost/token",
            EndpointPolicy(allow_loopback=True),
            purpose="t",
        )
        assert url.startswith("https://localhost")

    def test_private_address_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0))],
        )
        with pytest.raises(CredentialProviderError):
            validate_endpoint("https://internal.example.com/t", EndpointPolicy(), purpose="t")

    def test_valid_public_endpoint_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _dns_public(monkeypatch)
        url = validate_endpoint(
            "https://auth.example.com/oauth/token",
            EndpointPolicy(allowed_hosts=frozenset({"auth.example.com"})),
            purpose="t",
        )
        assert url == "https://auth.example.com/oauth/token"

    def test_unresolvable_host_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fail(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise OSError("no dns")

        monkeypatch.setattr(socket, "getaddrinfo", _fail)
        with pytest.raises(CredentialProviderError):
            validate_endpoint("https://ghost.example.com/t", EndpointPolicy(), purpose="t")

    def test_malformed_url_rejected(self) -> None:
        with pytest.raises(CredentialProviderError):
            validate_endpoint("not-a-url", EndpointPolicy(), purpose="t")


# ===========================================================================
# Manifest registration + audit
# ===========================================================================


class TestManifestRegistration:
    def test_both_manifests_register(self) -> None:
        registry = CredentialProviderRegistry()
        registry.register(static_manifest("static-dev"), object())
        registry.register(oauth2_manifest("oauth-main"), object())
        assert len(registry.manifests()) == 2
        assert registry.get("static-dev") is not None

    def test_manifest_contract(self) -> None:
        manifest = oauth2_manifest("oauth-main")
        assert manifest.plugin_kind.value == "CREDENTIAL_PROVIDER"
        assert manifest.contract_version == "1.0"
        assert "get_token" in manifest.capabilities

    def test_unknown_provider_id_fails_closed(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import (
            PlatformConfigurationError,
        )

        registry = CredentialProviderRegistry()
        with pytest.raises(PlatformConfigurationError):
            registry.get("ghost")

    @pytest.mark.asyncio
    async def test_audit_emitted_without_secrets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        events: list = []
        provider = _provider(monkeypatch, secret="audit-secret-value")
        provider._audit_sink = events.append  # type: ignore[method-assign]
        with patch(f"{MODULE}._post_token_request", return_value=(200, _token_payload())):
            await provider.get_token(_scope(), "oauth-main")
        assert len(events) == 1
        blob = f"{events[0].action} {events[0].detail} {events[0].resource_id}"
        assert "audit-secret-value" not in blob
        assert events[0].tenant_id == "tenant-a"

    @pytest.mark.asyncio
    async def test_failing_audit_sink_does_not_break_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(event) -> None:  # type: ignore[no-untyped-def]
            raise RuntimeError("sink down")

        provider = _provider(monkeypatch)
        provider._audit_sink = _boom  # type: ignore[method-assign]
        with patch(f"{MODULE}._post_token_request", return_value=(200, _token_payload())):
            token = await provider.get_token(_scope(), "oauth-main")
            assert token.token.get_secret_value() == "tok-1"


class TestExceptionContract:
    def test_credential_error_shape(self) -> None:
        err = CredentialProviderError("bad", details={"provider_id": "p"})
        assert err.code == "CREDENTIAL_PROVIDER_ERROR"
        assert err.http_status_code == 502
        assert err.retryable is False
