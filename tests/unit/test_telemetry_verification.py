# tests/unit/test_telemetry_verification.py
"""Unit tests for the platform telemetry verification service (fake clients)."""

from __future__ import annotations

import pytest

from investigation_agent_platform.application.onboarding.telemetry_verification import (
    verify_tenant_telemetry,
    verify_tenant_telemetry_from_dir,
)
from investigation_agent_platform.infrastructure.evidence.runtime.tenant_telemetry import (
    TelemetryEndpoint,
    TenantTelemetryConfig,
)


def _endpoint(**overrides):  # type: ignore[no-untyped-def]
    kwargs = {
        "name": "probe-es",
        "url": "http://127.0.0.1:9",
        "pattern": "logs-a-*,.ds-logs-a-*",
        "services": ("svc-a",),
    }
    kwargs.update(overrides)
    return TelemetryEndpoint(**kwargs)


def _configs(endpoint=None):  # type: ignore[no-untyped-def]
    endpoint = endpoint or _endpoint()
    return {"tenant-a": TenantTelemetryConfig(tenant_id="tenant-a", endpoints=(endpoint,))}


def _hit(doc_id="doc1", index="logs-a-1"):  # type: ignore[no-untyped-def]
    return {
        "_id": doc_id,
        "_index": index,
        "_source": {
            "@timestamp": "2026-01-01T00:00:00Z",
            "message": "boom",
            "service": {"name": "svc-a"},
        },
        "sort": [1767225600000, doc_id],
    }


class FakeClient:
    """Duck-typed provider client: canned pages, records search indexes."""

    def __init__(self, hits=None):  # type: ignore[no-untyped-def]
        self.hits = hits if hits is not None else [_hit()]
        self.indexes: list[str] = []

    async def search(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.indexes.append(kwargs.get("index", args[0] if args else None))
        return {"hits": {"hits": self.hits, "total": {"value": len(self.hits)}}}

    async def info(self):  # type: ignore[no-untyped-def]
        return {"cluster_name": "fake-cluster", "version": {"number": "8.12.0"}}

    @property
    def indices(self):  # type: ignore[no-untyped-def]
        class _Body:
            def __init__(self, payload):  # type: ignore[no-untyped-def]
                self.body = payload

        class _Indices:
            async def get(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                # ObjectApiResponse-shaped (body attr), not a plain dict —
                # proves the service unwraps provider envelopes.
                return _Body({"logs-a-1": {}, ".ds-logs-a-1-000001": {}})

        return _Indices()

    def options(self, **kwargs):  # type: ignore[no-untyped-def]
        return self

    async def close(self):  # type: ignore[no-untyped-def]
        pass


def _factory(client):  # type: ignore[no-untyped-def]
    def make(endpoint):  # type: ignore[no-untyped-def]
        return client

    return make


def _by_name(results):  # type: ignore[no-untyped-def]
    return {r.name: r for r in results}


@pytest.mark.asyncio
async def test_verify_all_green_with_fake_client():  # type: ignore[no-untyped-def]
    client = FakeClient()
    results = await verify_tenant_telemetry(
        configs=_configs(),
        tenant_id="tenant-a",
        home_service="svc-a",
        client_factory=_factory(client),
    )
    by_name = _by_name(results)
    assert by_name["config loads"].passed
    assert by_name["router built"].passed
    assert by_name["endpoint reachable and authenticated"].passed
    assert by_name["pattern resolves to indices"].passed
    assert by_name["routed search returns rows"].passed
    assert by_name["endpoint attributed"].passed
    assert by_name["home pattern used"].passed
    assert by_name["service filter returns rows"].passed
    assert by_name["unhomed service fails closed"].passed
    assert by_name["unknown tenant fails closed"].passed
    assert by_name["fetch resolves home endpoint"].passed
    assert all(i == "logs-a-*,.ds-logs-a-*" for i in client.indexes[:3])
    # The fetch leg addresses the concrete stored index (validated against
    # the pattern, not replaced by it) — designed behavior.
    assert client.indexes[3] == "logs-a-1"


@pytest.mark.asyncio
async def test_verify_empty_rows_still_attributes_and_fetch_fails():  # type: ignore[no-untyped-def]
    client = FakeClient(hits=[])
    results = await verify_tenant_telemetry(
        configs=_configs(),
        tenant_id="tenant-a",
        home_service="svc-a",
        client_factory=_factory(client),
    )
    by_name = _by_name(results)
    assert not by_name["routed search returns rows"].passed
    assert by_name["endpoint attributed"].passed
    assert not by_name["fetch resolves home endpoint"].passed


@pytest.mark.asyncio
async def test_verify_unknown_tenant_short_circuits():  # type: ignore[no-untyped-def]
    results = await verify_tenant_telemetry(
        configs=_configs(),
        tenant_id="nope",
        home_service="svc-a",
        client_factory=_factory(FakeClient()),
    )
    assert [r.name for r in results] == ["config loads"]
    assert results[0].passed is False


@pytest.mark.asyncio
async def test_verify_unhomed_service_short_circuits():  # type: ignore[no-untyped-def]
    results = await verify_tenant_telemetry(
        configs=_configs(),
        tenant_id="tenant-a",
        home_service="ghost",
        client_factory=_factory(FakeClient()),
    )
    assert [r.name for r in results] == ["config loads", "home endpoint homing service"]
    assert results[-1].passed is False


@pytest.mark.asyncio
async def test_verify_from_dir_loads_file(tmp_path):  # type: ignore[no-untyped-def]
    tenant_dir = tmp_path / "tenant-a"
    tenant_dir.mkdir()
    (tenant_dir / "telemetry.conf").write_text(
        "[e]\nurl = http://127.0.0.1:9\npattern = logs-a-*\nservices = svc-a\n",
        encoding="utf-8",
    )
    results = await verify_tenant_telemetry_from_dir(
        telemetry_dir=str(tmp_path),
        tenant_id="tenant-a",
        home_service="svc-a",
        client_factory=_factory(FakeClient()),
    )
    assert _by_name(results)["fetch resolves home endpoint"].passed


@pytest.mark.asyncio
async def test_verify_from_dir_malformed_fails_closed(tmp_path):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.common.exceptions import (
        PlatformConfigurationError,
    )

    tenant_dir = tmp_path / "tenant-a"
    tenant_dir.mkdir()
    (tenant_dir / "telemetry.conf").write_text(
        "[one]\nurl = https://a.example.com\nservices = svc\n\n"
        "[two]\nurl = https://b.example.com\nservices = svc\n",
        encoding="utf-8",
    )
    with pytest.raises(PlatformConfigurationError):
        await verify_tenant_telemetry_from_dir(
            telemetry_dir=str(tmp_path),
            tenant_id="tenant-a",
            home_service="svc",
            client_factory=_factory(FakeClient()),
        )


def test_check_result_serializes_without_secrets():  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.onboarding.telemetry_verification import (
        TelemetryCheckResult,
    )

    result = TelemetryCheckResult(name="n", passed=True, detail="d").to_dict()
    assert result == {"name": "n", "passed": True, "detail": "d"}


class FailingClient(FakeClient):
    async def search(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.domain.common.exceptions import (
            ProviderUnavailableException,
        )

        raise ProviderUnavailableException("down")


@pytest.mark.asyncio
async def test_verify_provider_error_records_fail_and_continues():  # type: ignore[no-untyped-def]
    results = await verify_tenant_telemetry(
        configs=_configs(),
        tenant_id="tenant-a",
        home_service="svc-a",
        client_factory=_factory(FailingClient()),
    )
    by_name = _by_name(results)
    assert by_name["routed search returns rows"].passed is False
    assert "ProviderUnavailableException" in by_name["routed search returns rows"].detail
    # Non-search checks still execute.
    assert by_name["unhomed service fails closed"].passed is True
    assert by_name["unknown tenant fails closed"].passed is True
    assert by_name["fetch resolves home endpoint"].passed is False


MIN_MAPPING = """\
source_id: test-svc-map
index_patterns:
  - "logs-a-*"
tenant_scope:
  strategy: index_pattern
timestamp:
  candidates: ["@timestamp"]
  value_type: date_iso
message:
  candidates: ["message"]
  value_type: text
service:
  candidates: ["appName", "Service"]
  value_type: keyword
severity:
  strategy: error_flag
  error_field: error
identifiers:
  trace_id:
    candidates: ["traceId"]
    value_type: keyword
code_location: null
sql_statement: null
sort:
  field: "@timestamp"
  order: desc
  format: epoch_millis
  unmapped_type: long
top_level_allowlist:
  - "@timestamp"
  - message
  - appName
  - Service
  - error
  - traceId
"""


def _app_hit(doc_id="doc1", index="logs-a-1", app="svc-a"):  # type: ignore[no-untyped-def]
    return {
        "_id": doc_id,
        "_index": index,
        "_source": {
            "@timestamp": "2026-01-01T00:00:00Z",
            "message": "boom",
            "appName": app,
            "error": False,
            "traceId": "t1",
        },
        "sort": [1767225600000, doc_id],
    }


@pytest.mark.asyncio
async def test_verify_with_mapping_attributes_service(tmp_path):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.infrastructure.configuration.mapping_registry import (
        load_mapping_profiles,
    )

    (tmp_path / "m.yaml").write_text(MIN_MAPPING, encoding="utf-8")
    registry = load_mapping_profiles(tmp_path)
    client = FakeClient(hits=[_app_hit()])
    results = await verify_tenant_telemetry(
        configs=_configs(),
        tenant_id="tenant-a",
        home_service="svc-a",
        client_factory=_factory(client),
        mapping_source_id="test-svc-map",
        mapping_registry=registry,
    )
    by_name = _by_name(results)
    assert by_name["endpoint attributed"].passed
    assert by_name["service filter returns rows"].passed
    assert "service=svc-a" in by_name["service filter returns rows"].detail


@pytest.mark.asyncio
async def test_verify_mapping_without_registry_fails_closed():  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="mapping_registry or mapping_profiles_path"):
        await verify_tenant_telemetry(
            configs=_configs(),
            tenant_id="tenant-a",
            home_service="svc-a",
            client_factory=_factory(FakeClient()),
            mapping_source_id="test-svc-map",
        )
