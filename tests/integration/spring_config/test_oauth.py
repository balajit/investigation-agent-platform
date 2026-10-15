# tests/integration/spring_config/test_oauth.py
"""OAuth2 client-credentials tests (stub transport, no network)."""

import json
import urllib.error

import pytest

from investigation_agent_platform.integrations.spring_config.client import (
    OAuth2ClientCredentials,
)
from investigation_agent_platform.integrations.spring_config.exceptions import (
    ConfigAuthenticationError,
    ConfigClientError,
    ConfigTransportError,
)


class _FakeResponse:
    def __init__(self, status=200, body=b"{}", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeOpener:
    def __init__(self, items):
        self._items = list(items)
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append(request)
        item = self._items.pop(0) if len(self._items) > 1 else self._items[0]
        if isinstance(item, Exception):
            raise item
        return item


def _token_body(token="tok-1", expires_in=300):
    return json.dumps({"access_token": token, "expires_in": expires_in}).encode()


def _oauth(opener, **overrides):
    kwargs = {
        "token_url": "https://auth.example/token",
        "client_id": "id",
        "client_secret": "secret",
    }
    kwargs.update(overrides)
    oauth = OAuth2ClientCredentials(**kwargs)
    oauth._opener = opener
    return oauth


def test_token_cached_until_expiry() -> None:
    opener = _FakeOpener([_FakeResponse(200, _token_body())])
    oauth = _oauth(opener)
    assert oauth.get_token() == "tok-1"
    assert oauth.get_token() == "tok-1"
    assert len(opener.calls) == 1


def test_expired_token_refetched() -> None:
    opener = _FakeOpener(
        [_FakeResponse(200, _token_body("tok-1")), _FakeResponse(200, _token_body("tok-2"))]
    )
    oauth = _oauth(opener)
    assert oauth.get_token() == "tok-1"
    oauth._expires_at = 0.0  # force expiry
    assert oauth.get_token() == "tok-2"
    assert len(opener.calls) == 2


def test_rejects_non_https_token_url() -> None:
    with pytest.raises(ValueError):
        OAuth2ClientCredentials(
            token_url="http://auth.example/token", client_id="id", client_secret="s"
        )


def test_401_is_authentication_error() -> None:
    err = urllib.error.HTTPError("https://auth.example/token", 401, "unauth", {}, None)
    oauth = _oauth(_FakeOpener([err]))
    with pytest.raises(ConfigAuthenticationError):
        oauth.get_token()


def test_empty_token_rejected() -> None:
    opener = _FakeOpener([_FakeResponse(200, _token_body(token="  "))])
    with pytest.raises(ConfigClientError):
        _oauth(opener).get_token()


def test_invalid_lifetime_rejected() -> None:
    opener = _FakeOpener([_FakeResponse(200, _token_body(expires_in=0))])
    with pytest.raises(ConfigClientError):
        _oauth(opener).get_token()


def test_unreachable_endpoint_is_transport_error() -> None:
    opener = _FakeOpener([urllib.error.URLError("dns fail")])
    with pytest.raises(ConfigTransportError):
        _oauth(opener).get_token()
