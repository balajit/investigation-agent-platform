# tests/unit/test_probe_api.py
"""P2-2: PROBE_API sandboxing battery."""

from typing import ClassVar

import httpx
import pytest

from investigation_agent_platform.domain.common.exceptions import (
    ProbeEndpointNotFoundError,
    SecurityPolicyViolationException,
)
from investigation_agent_platform.infrastructure.evidence.api.probe_adapter import (
    HttpxApiProbeAdapter,
)
from investigation_agent_platform.ports.evidence.api_probe import ApiProbeRequest


class _Endpoint:
    def __init__(self, **kw):
        self.id = kw.get("id", "list-things")
        self.description = kw.get("description", "d")
        self.method = kw.get("method", "GET")
        self.base_url = kw.get("base_url", "http://internal:8000")
        self.path_template = kw.get("path_template", "/api/v1/things/{thing_id}")
        self.path_placeholders = kw.get("path_placeholders", ["thing_id"])
        self.allowed_query_params = kw.get("allowed_query_params", ["verbose"])


class _Service:
    probeable_endpoints: ClassVar[list] = [_Endpoint()]


class _Profile:
    services: ClassVar[list] = [_Service()]


class _Repo:
    async def get_by_application_id(self, tenant_id, application_id):
        return _Profile()


def _adapter(handler=None):
    def _factory():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler or _ok))
    return HttpxApiProbeAdapter(profile_repo=_Repo(), client_factory=_factory)


def _ok(request: httpx.Request) -> httpx.Response:
    assert request.headers["X-Tenant-ID"] == "tenant-a"
    return httpx.Response(
        200,
        headers={"Content-Type": "application/json", "Set-Cookie": "s=1", "X-Correlation-Id": "c1"},
        json={"items": [1, 2]},
    )


@pytest.mark.asyncio
async def test_probe_success_redacts_headers_and_injects_tenant():
    result = await _adapter().probe(
        "tenant-a", "app", ApiProbeRequest(endpoint_id="list-things", path_params={"thing_id": "42"})
    )
    assert result.status_code == 200
    assert result.resolved_url == "http://internal:8000/api/v1/things/42"
    assert "set-cookie" not in result.response_headers
    assert result.response_headers["x-correlation-id"] == "c1"


@pytest.mark.asyncio
async def test_unknown_endpoint_rejected_before_network():
    calls = []

    def _handler(request):
        calls.append(request)
        return _ok(request)

    with pytest.raises(ProbeEndpointNotFoundError):
        await _adapter(_handler).probe(
            "tenant-a", "app", ApiProbeRequest(endpoint_id="nope")
        )
    assert calls == []


@pytest.mark.asyncio
async def test_undeclared_path_param_rejected():
    with pytest.raises(SecurityPolicyViolationException):
        await _adapter().probe(
            "tenant-a",
            "app",
            ApiProbeRequest(endpoint_id="list-things", path_params={"evil": "../../x"}),
        )


@pytest.mark.asyncio
async def test_disallowed_query_params_dropped():
    seen = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return _ok(request)

    await _adapter(_handler).probe(
        "tenant-a",
        "app",
        ApiProbeRequest(
            endpoint_id="list-things",
            path_params={"thing_id": "1"},
            query_params={"verbose": "true", "drop_table": "x"},
        ),
    )
    assert "verbose=true" in seen["url"]
    assert "drop_table" not in seen["url"]


@pytest.mark.asyncio
async def test_non_http_base_url_rejected():
    class _BadService:
        probeable_endpoints: ClassVar[list] = [_Endpoint(base_url="ftp://internal/x")]

    class _BadProfile:
        services: ClassVar[list] = [_BadService()]

    class _BadRepo:
        async def get_by_application_id(self, tenant_id, application_id):
            return _BadProfile()

    adapter = HttpxApiProbeAdapter(profile_repo=_BadRepo())
    with pytest.raises(SecurityPolicyViolationException):
        await adapter.probe(
            "tenant-a", "app", ApiProbeRequest(endpoint_id="list-things")
        )


@pytest.mark.asyncio
async def test_anonymous_tenant_rejected():
    with pytest.raises(SecurityPolicyViolationException):
        await _adapter().probe(
            "anonymous", "app", ApiProbeRequest(endpoint_id="list-things")
        )


@pytest.mark.asyncio
async def test_empty_path_param_rejected_before_network():
    calls = []

    def _handler(request):
        calls.append(request)
        return _ok(request)

    with pytest.raises(SecurityPolicyViolationException):
        await _adapter(_handler).probe(
            "tenant-a", "app", ApiProbeRequest(endpoint_id="list-things")
        )
    assert calls == []


@pytest.mark.asyncio
async def test_oversized_body_truncated_to_model_cap():
    big = {"items": list(range(5000))}

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "application/json"}, json=big)

    result = await _adapter(_handler).probe(
        "tenant-a",
        "app",
        ApiProbeRequest(endpoint_id="list-things", path_params={"thing_id": "1"}),
    )
    assert result.response_body_truncated is True
    assert len(result.response_body_excerpt) <= 4096


@pytest.mark.asyncio
async def test_excerpt_stays_below_evidence_externalization_boundary():
    """Boundary off-by-one killed a live run: a 4096-char excerpt trips the
    Evidence invariant (content_snippet >= 4096 requires content_uri)."""
    big = {"items": list(range(20000))}

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "application/json"}, json=big)

    result = await _adapter(_handler).probe(
        "tenant-a",
        "app",
        ApiProbeRequest(endpoint_id="list-things", path_params={"thing_id": "1"}),
    )
    assert result.response_body_truncated is True
    assert len(result.response_body_excerpt) < 4096
