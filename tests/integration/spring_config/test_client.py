# tests/integration/spring_config/test_client.py
"""SpringConfigClient tests (stub transport, no network)."""

import json
import logging
import urllib.error
from types import SimpleNamespace

import pytest

from investigation_agent_platform.integrations.spring_config.client import SpringConfigClient
from investigation_agent_platform.integrations.spring_config.exceptions import (
    ConfigAuthorizationError,
    ConfigClientError,
    ConfigNotFoundError,
    ConfigServerError,
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


def _sources_body(sources):
    return json.dumps({"propertySources": sources}).encode()


def _client(opener, **overrides):
    kwargs = {
        "server_url": "https://cfg.example",
        "oauth": SimpleNamespace(get_token=lambda: "tok"),
        "allowed_applications": {"shop"},
        "allowed_profiles": {"prod"},
        "allowed_property_patterns": ("spring.application.name", "feature.*"),
    }
    kwargs.update(overrides)
    client = SpringConfigClient(**kwargs)
    client._opener = opener
    return client


def test_allowlist_enforced_before_io() -> None:
    opener = _FakeOpener([_FakeResponse(200, _sources_body([]))])
    client = _client(opener)
    with pytest.raises(ConfigAuthorizationError):
        client.fetch("other-app", "prod")
    with pytest.raises(ConfigAuthorizationError):
        client.fetch("shop", "dev")
    assert opener.calls == []


def test_invalid_label_rejected_without_io() -> None:
    opener = _FakeOpener([_FakeResponse(200, _sources_body([]))])
    client = _client(opener)
    with pytest.raises(ValueError):
        client.fetch("shop", "prod", label="../../etc")
    assert opener.calls == []


def test_precedence_merge_first_source_wins() -> None:
    body = _sources_body(
        [
            {"name": "high", "source": {"feature.a": "high", "feature.shared": "high"}},
            {"name": "low", "source": {"feature.a": "low", "feature.shared": "low"}},
        ]
    )
    result = _client(_FakeOpener([_FakeResponse(200, body)])).fetch("shop", "prod")
    assert result.properties["feature.a"] == "high"
    assert result.properties["feature.shared"] == "high"
    assert result.property_sources == ("high", "low")


def test_unallowlisted_and_sensitive_keys_dropped() -> None:
    body = _sources_body(
        [
            {
                "name": "s",
                "source": {
                    "spring.application.name": "shop",
                    "spring.datasource.password": "s3cr3t",
                    "other.key": "x",
                },
            }
        ]
    )
    client = _client(
        _FakeOpener([_FakeResponse(200, body)]),
        allowed_property_patterns=("spring.application.*", "spring.datasource.*"),
    )
    result = client.fetch("shop", "prod")
    assert result.properties == {"spring.application.name": "shop"}


def test_404_maps_to_not_found() -> None:
    err = urllib.error.HTTPError("https://cfg.example/x", 404, "nf", {}, None)
    with pytest.raises(ConfigNotFoundError):
        _client(_FakeOpener([err])).fetch("shop", "prod")


def test_401_403_do_not_retry() -> None:
    for code, exc_type in (
        (401, ConfigClientError),
        (403, ConfigAuthorizationError),
    ):
        err = urllib.error.HTTPError("https://cfg.example/x", code, "denied", {}, None)
        opener = _FakeOpener([err])
        with pytest.raises(exc_type):
            _client(opener).fetch("shop", "prod")
        assert len(opener.calls) == 1


def test_429_retries_then_succeeds() -> None:
    throttled = urllib.error.HTTPError("https://cfg.example/x", 429, "slow", {}, None)
    ok = _FakeResponse(200, _sources_body([]))
    opener = _FakeOpener([throttled, ok])
    result = _client(opener, allowed_property_patterns=()).fetch("shop", "prod")
    assert result.properties == {}
    assert len(opener.calls) == 2


def test_503_exhausted_raises_server_error() -> None:
    errs = [urllib.error.HTTPError("https://cfg.example/x", 503, "down", {}, None)] * 3
    with pytest.raises(ConfigServerError):
        _client(_FakeOpener(errs)).fetch("shop", "prod")


def test_transport_failure_is_typed() -> None:
    with pytest.raises(ConfigTransportError):
        _client(_FakeOpener([urllib.error.URLError("conn refused")] * 3)).fetch("shop", "prod")


def test_values_never_logged(caplog) -> None:
    body = _sources_body([{"name": "s", "source": {"feature.token": "VALUE-9f8e7d6c"}}])
    client = _client(
        _FakeOpener([_FakeResponse(200, body)]),
        allowed_property_patterns=("feature.*",),
    )
    with caplog.at_level(
        logging.INFO, logger="investigation_agent_platform.integrations.spring_config.client"
    ):
        client.fetch("shop", "prod")
    assert "VALUE-9f8e7d6c" not in caplog.text


def test_rejects_non_https_server() -> None:
    with pytest.raises(ValueError):
        SpringConfigClient(
            server_url="http://cfg.example",
            oauth=SimpleNamespace(get_token=lambda: "tok"),
            allowed_applications=set(),
            allowed_profiles=set(),
            allowed_property_patterns=(),
        )
