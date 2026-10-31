# tests/unit/test_tenant_telemetry.py
"""Phase A: per-tenant telemetry endpoints (DES-tenant-telemetry-config).

All fixtures are synthetic (tenant-a / sample services); no operator
paths, credentials, or customer identifiers appear anywhere.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from investigation_agent_platform.domain.common.exceptions import (
    EvidenceNotFoundException,
    PlatformConfigurationError,
    ProviderNotConfiguredError,
)
from investigation_agent_platform.domain.evidence.requests import (
    RuntimeEvidenceRequest,
    TimeRange,
)
from investigation_agent_platform.domain.profile.models import ApplicationProfile, Service
from investigation_agent_platform.infrastructure.configuration.config import EvidenceConfig
from investigation_agent_platform.infrastructure.evidence.runtime.elastic import (
    AsyncElasticAdapter,
    ElasticAdapterSettings,
)
from investigation_agent_platform.infrastructure.evidence.runtime.tenant_telemetry import (
    ResolvedTenantEndpoint,
    TelemetryEndpoint,
    TenantTelemetryConfig,
    TenantTelemetryRouter,
    build_tenant_router,
    endpoint_index_allowed,
    load_tenant_telemetry_configs,
    merge_managed_defaults,
)

TEST_KEY = b"test-signing-key"


def _settings(**overrides):  # type: ignore[no-untyped-def]
    kwargs = {"cursor_signing_key": TEST_KEY}
    kwargs.update(overrides)
    return ElasticAdapterSettings(**kwargs)


def _conf(path, tenant, body):  # type: ignore[no-untyped-def]
    tenant_dir = path / tenant
    tenant_dir.mkdir(parents=True, exist_ok=True)
    (tenant_dir / "telemetry.conf").write_text(body, encoding="utf-8")


HAPPY = """\
[orders-es]
url = https://elastic.example.com:9200
api_key = secret-1
timeout = 30
pattern = logs-orders-*
services = svc-a, svc-b
managed_by = manual

[billing-es]
url = http://elastic-billing.example.com:9200
user = reader
password = secret-2
services = svc-c
"""


def _profile(app_id="svc-a", services=()):  # type: ignore[no-untyped-def]
    return ApplicationProfile(
        id=app_id,
        tenant_id="tenant-a",
        name="n",
        description="d",
        environment="production",
        services=[Service(name=name) for name in services],
    )


def _request(**overrides):  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    kwargs = {
        "environment": "production",
        "identifiers": {"trace_id": "abc123"},
        "keywords": ["timeout"],
        "services": ["svc-a"],
        "severities": ["ERROR"],
        "time_range": TimeRange(start_time=now - timedelta(minutes=15), end_time=now),
        "limit": 2,
    }
    kwargs.update(overrides)
    return RuntimeEvidenceRequest(**kwargs)


def _hit(doc_id="doc1", index="logs-orders-42", sort=None):  # type: ignore[no-untyped-def]
    return {
        "_id": doc_id,
        "_index": index,
        "_source": {"@timestamp": "2026-01-01T00:00:00Z", "message": "boom"},
        "sort": sort if sort is not None else [1767225600000, doc_id],
    }


def _search_response(hits, total=None):  # type: ignore[no-untyped-def]
    return {"hits": {"hits": hits, "total": {"value": total if total is not None else len(hits)}}}


def _router_with(client, pattern="logs-orders-*", timeout=30.0, name="orders-es"):  # type: ignore[no-untyped-def]
    handle = ResolvedTenantEndpoint(
        tenant_id="tenant-a",
        endpoint_name=name,
        client=client,
        index_pattern=pattern,
        request_timeout=timeout,
    )
    return TenantTelemetryRouter({"tenant-a": {"svc-a": handle}})


# --- loader -----------------------------------------------------------------


def test_load_happy_path(tmp_path):  # type: ignore[no-untyped-def]
    _conf(tmp_path, "tenant-a", HAPPY)
    configs = load_tenant_telemetry_configs(tmp_path)
    config = configs["tenant-a"]
    assert config.tenant_id == "tenant-a"
    orders, billing = config.endpoints
    assert orders.name == "orders-es"
    assert orders.auth_kind() == "api_key"
    assert orders.timeout_seconds == 30
    assert orders.pattern == "logs-orders-*"
    assert orders.services == ("svc-a", "svc-b")
    assert billing.auth_kind() == "basic"
    assert billing.user == "reader"
    assert billing.password.get_secret_value() == "secret-2"
    # Secrets are SecretStr, never plain attributes.
    assert not isinstance(billing.password, str)


def test_load_comma_separated_pattern(tmp_path):  # type: ignore[no-untyped-def]
    _conf(
        tmp_path,
        "tenant-a",
        "[e]\nurl = https://h.example.com\npattern = logs-a-*, .ds-logs-a-*\nservices = svc\n",
    )
    endpoint = load_tenant_telemetry_configs(tmp_path)["tenant-a"].endpoints[0]
    assert endpoint.pattern == "logs-a-*,.ds-logs-a-*"
    assert endpoint.pattern_segments == ("logs-a-*", ".ds-logs-a-*")
    assert endpoint_index_allowed("logs-a-1", endpoint.pattern)
    assert endpoint_index_allowed(".ds-logs-a-1-000001", endpoint.pattern)
    assert not endpoint_index_allowed("logs-b-1", endpoint.pattern)


def test_load_rejects_bad_pattern_segment(tmp_path):  # type: ignore[no-untyped-def]
    _conf(
        tmp_path,
        "tenant-a",
        "[e]\nurl = https://h.example.com\npattern = logs-a-*, has space\nservices = svc\n",
    )
    with pytest.raises(PlatformConfigurationError, match="outside"):
        load_tenant_telemetry_configs(tmp_path)


def test_load_empty_and_missing(tmp_path):  # type: ignore[no-untyped-def]
    assert load_tenant_telemetry_configs("") == {}
    assert load_tenant_telemetry_configs(None) == {}
    assert load_tenant_telemetry_configs(tmp_path / "nope") == {}
    (tmp_path / "tenant-b").mkdir()
    assert load_tenant_telemetry_configs(tmp_path) == {}


def test_load_rejects_empty_file(tmp_path):  # type: ignore[no-untyped-def]
    _conf(tmp_path, "tenant-a", "# nothing here\n")
    with pytest.raises(PlatformConfigurationError):
        load_tenant_telemetry_configs(tmp_path)


def test_load_rejects_duplicate_service(tmp_path):  # type: ignore[no-untyped-def]
    _conf(
        tmp_path,
        "tenant-a",
        "[one]\nurl = https://a.example.com\nservices = svc\n\n"
        "[two]\nurl = https://b.example.com\nservices = svc\n",
    )
    with pytest.raises(PlatformConfigurationError, match="exactly one"):
        load_tenant_telemetry_configs(tmp_path)


def test_load_rejects_bad_url_and_userinfo(tmp_path):  # type: ignore[no-untyped-def]
    _conf(tmp_path, "tenant-a", "[e]\nurl = not-a-url\nservices = svc\n")
    with pytest.raises(PlatformConfigurationError):
        load_tenant_telemetry_configs(tmp_path)
    _conf(tmp_path, "tenant-a", HAPPY)
    _conf(
        tmp_path,
        "tenant-b",
        "[e]\nurl = https://user:pass@h.example.com\nservices = svc\n",
    )
    with pytest.raises(PlatformConfigurationError, match="must not embed credentials"):
        load_tenant_telemetry_configs(tmp_path)


def test_load_rejects_half_basic_auth(tmp_path):  # type: ignore[no-untyped-def]
    _conf(tmp_path, "tenant-a", "[e]\nurl = https://h.example.com\nuser = u\nservices = svc\n")
    with pytest.raises(PlatformConfigurationError, match="half-configured"):
        load_tenant_telemetry_configs(tmp_path)


def test_load_api_key_wins_over_basic(tmp_path):  # type: ignore[no-untyped-def]
    _conf(
        tmp_path,
        "tenant-a",
        "[e]\nurl = https://h.example.com\nuser = u\npassword = p\napi_key = k\nservices = svc\n",
    )
    config = load_tenant_telemetry_configs(tmp_path)["tenant-a"]
    assert config.endpoints[0].auth_kind() == "api_key"


def test_load_rejects_bad_timeout_and_name(tmp_path):  # type: ignore[no-untyped-def]
    _conf(
        tmp_path, "tenant-a", "[e]\nurl = https://h.example.com\ntimeout = soon\nservices = svc\n"
    )
    with pytest.raises(PlatformConfigurationError, match="non-integer timeout"):
        load_tenant_telemetry_configs(tmp_path)
    _conf(tmp_path, "tenant-a", HAPPY)
    _conf(tmp_path, "tenant-b", "[bad name!]\nurl = https://h.example.com\nservices = svc\n")
    with pytest.raises(PlatformConfigurationError, match="invalid endpoint name"):
        load_tenant_telemetry_configs(tmp_path)


def test_env_overrides_apply(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    _conf(tmp_path, "tenant-a", HAPPY)
    monkeypatch.setenv("IAP_ES_TENANT_A_ORDERS_ES_TIMEOUT", "45")
    monkeypatch.setenv("IAP_ES_TENANT_A_ORDERS_ES_PATTERN", "logs-orders-v2-*")
    monkeypatch.setenv("IAP_ES_TENANT_A_BILLING_ES_USER", "alt-reader")
    configs = load_tenant_telemetry_configs(tmp_path)
    orders = configs["tenant-a"].endpoint_for("svc-a")
    assert orders is not None and orders.timeout_seconds == 45
    assert orders is not None and orders.pattern == "logs-orders-v2-*"
    billing = configs["tenant-a"].endpoint_for("svc-c")
    assert billing is not None and billing.user == "alt-reader"


def test_env_override_unknown_endpoint_fails_closed(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    _conf(tmp_path, "tenant-a", HAPPY)
    monkeypatch.setenv("IAP_ES_TENANT_A_GHOST_ES_URL", "https://ghost.example.com")
    with pytest.raises(PlatformConfigurationError, match="unknown telemetry endpoint"):
        load_tenant_telemetry_configs(tmp_path)


# --- managed-defaults merge --------------------------------------------------


def _endpoint(name, services, managed_by="manual"):  # type: ignore[no-untyped-def]
    return TelemetryEndpoint(
        name=name,
        url="https://h.example.com",
        timeout_seconds=30,
        pattern="logs-*",
        services=tuple(services),
        managed_by=managed_by,
    )


def test_merge_never_clobbers_manual():  # type: ignore[no-untyped-def]
    existing = TenantTelemetryConfig(
        tenant_id="t",
        endpoints=(
            _endpoint("manual-es", ["svc-a"], "manual"),
            _endpoint("old-auto", ["svc-b"], "auto"),
        ),
    )
    merged = merge_managed_defaults(
        existing,
        [
            _endpoint("manual-es", ["svc-CHANGED"], "manual"),
            _endpoint("new-auto", ["svc-c"], "auto"),
        ],
        tenant_id="t",
    )
    by_name = {e.name: e for e in merged.endpoints}
    # Same-name default dropped: manual entry untouched.
    assert by_name["manual-es"].services == ("svc-a",)
    # Stale auto entry removed; new auto entry added.
    assert "old-auto" not in by_name
    assert by_name["new-auto"].services == ("svc-c",)


def test_merge_rejects_non_auto_and_empty_and_mismatch():  # type: ignore[no-untyped-def]
    with pytest.raises(PlatformConfigurationError, match="managed_by='auto'"):
        merge_managed_defaults(None, [_endpoint("e", ["s"], "manual")], tenant_id="t")
    with pytest.raises(PlatformConfigurationError, match="zero endpoints"):
        merge_managed_defaults(None, [], tenant_id="t")
    existing = TenantTelemetryConfig(tenant_id="other", endpoints=(_endpoint("e", ["s"]),))
    with pytest.raises(PlatformConfigurationError, match="tenant mismatch"):
        merge_managed_defaults(existing, [_endpoint("n", ["x"], "auto")], tenant_id="t")


# --- router ------------------------------------------------------------------


def test_router_resolution_and_failures():  # type: ignore[no-untyped-def]
    handle = ResolvedTenantEndpoint("t", "e1", object(), "logs-*", 30.0)
    router = TenantTelemetryRouter({"t": {"svc": handle}})
    assert router.has_tenant("t")
    assert not router.has_tenant("unknown")
    assert router.resolve("t", ["svc"]) is handle
    with pytest.raises(ProviderNotConfiguredError):
        router.resolve("unknown", ["svc"])
    with pytest.raises(ProviderNotConfiguredError):
        router.resolve("t", ["unhomed"])
    with pytest.raises(ProviderNotConfiguredError):
        router.resolve("t", [])
    other = ResolvedTenantEndpoint("t", "e2", object(), "logs-*", 30.0)
    overlap = TenantTelemetryRouter({"t": {"a": handle, "b": other}})
    with pytest.raises(PlatformConfigurationError, match="multiple endpoints"):
        overlap.resolve("t", ["a", "b"])
    assert {h.endpoint_name for h in overlap.endpoints_for_tenant("t")} == {"e1", "e2"}


def test_build_router_coherence_and_factory_errors():  # type: ignore[no-untyped-def]
    config = TenantTelemetryConfig(tenant_id="t", endpoints=(_endpoint("slow", ["svc"]),))
    with pytest.raises(PlatformConfigurationError, match="must strictly exceed"):
        build_tenant_router({"t": config}, lambda endpoint: object(), query_timeout_seconds=30.0)

    def _boom(endpoint):  # type: ignore[no-untyped-def]
        raise RuntimeError("dial failed")

    with pytest.raises(PlatformConfigurationError, match="cannot construct client"):
        build_tenant_router({"t": config}, _boom, query_timeout_seconds=25.0)
    assert build_tenant_router({}, lambda endpoint: object(), query_timeout_seconds=25.0) is None


def test_endpoint_index_allowed():  # type: ignore[no-untyped-def]
    assert endpoint_index_allowed("logs-orders-42", "logs-orders-*")
    assert not endpoint_index_allowed("logs-billing-1", "logs-orders-*")


# --- adapter routing ----------------------------------------------------------


def _tenant_adapter(tenant_client=None, router=None, **overrides):  # type: ignore[no-untyped-def]
    return AsyncElasticAdapter(
        client=MagicMock() if tenant_client is None else tenant_client,
        settings=_settings(**overrides),
        tenant_router=router,
    )


@pytest.mark.asyncio
async def test_global_path_unchanged_without_router():  # type: ignore[no-untyped-def]
    client = MagicMock()
    client.search = AsyncMock(return_value=_search_response([_hit("d1")]))
    adapter = _tenant_adapter(tenant_client=client)
    result = await adapter.search_runtime_evidence("tenant-a", uuid.uuid4(), _request(), _profile())
    (index,) = [call.kwargs["index"] for call in client.search.call_args_list]
    # The request names service "svc-a": the legacy scope narrows to it (R2).
    assert index == "logs-tenant-a-svc-a-production-*"
    assert result.execution_metadata["endpoint"] is None


@pytest.mark.asyncio
async def test_tenant_routed_search_uses_home_endpoint():  # type: ignore[no-untyped-def]
    tenant_client = MagicMock()
    tenant_client.search = AsyncMock(return_value=_search_response([_hit("d1")]))
    global_client = MagicMock()
    global_client.search = AsyncMock(return_value=_search_response([_hit("d1")]))
    adapter = AsyncElasticAdapter(
        client=global_client,
        settings=_settings(),
        tenant_router=_router_with(tenant_client),
    )
    result = await adapter.search_runtime_evidence("tenant-a", uuid.uuid4(), _request(), _profile())
    assert global_client.search.call_count == 0
    (index,) = [call.kwargs["index"] for call in tenant_client.search.call_args_list]
    assert index == "logs-orders-*"
    assert result.execution_metadata["endpoint"] == "orders-es"


@pytest.mark.asyncio
async def test_tenant_unhomed_service_fails_closed():  # type: ignore[no-untyped-def]
    adapter = AsyncElasticAdapter(
        client=MagicMock(), settings=_settings(), tenant_router=_router_with(MagicMock())
    )
    with pytest.raises(ProviderNotConfiguredError):
        await adapter.search_runtime_evidence(
            "tenant-a", uuid.uuid4(), _request(), _profile(app_id="unknown-app")
        )


@pytest.mark.asyncio
async def test_tenant_routing_without_profile_uses_request_services():  # type: ignore[no-untyped-def]
    tenant_client = MagicMock()
    tenant_client.search = AsyncMock(return_value=_search_response([_hit("d1")]))
    adapter = AsyncElasticAdapter(
        client=MagicMock(), settings=_settings(), tenant_router=_router_with(tenant_client)
    )
    result = await adapter.search_runtime_evidence("tenant-a", uuid.uuid4(), _request(), None)
    assert result.execution_metadata["endpoint"] == "orders-es"
    with pytest.raises(ProviderNotConfiguredError):
        await adapter.search_runtime_evidence("tenant-a", uuid.uuid4(), _request(services=[]), None)


def test_adapter_requires_client_or_router():  # type: ignore[no-untyped-def]
    with pytest.raises(PlatformConfigurationError, match="or a tenant telemetry router"):
        AsyncElasticAdapter(client=None, settings=_settings())
    adapter = AsyncElasticAdapter(
        client=None, settings=_settings(), tenant_router=_router_with(MagicMock())
    )
    assert adapter is not None


@pytest.mark.asyncio
async def test_tenant_only_adapter_global_path_fails_closed():  # type: ignore[no-untyped-def]
    adapter = AsyncElasticAdapter(
        client=None, settings=_settings(), tenant_router=_router_with(MagicMock())
    )
    with pytest.raises(ProviderNotConfiguredError):
        await adapter.search_runtime_evidence(
            "tenant-unknown", uuid.uuid4(), _request(), _profile()
        )


class _Row:  # type: ignore[no-untyped-def]
    def __init__(self, tenant_id, investigation_id, evidence_id, source):
        self.tenant_id = tenant_id
        self.investigation_id = investigation_id
        self.evidence_id = evidence_id
        self.provider = "ELASTIC"
        self.source = source


class _Lookup:  # type: ignore[no-untyped-def]
    def __init__(self, row):
        self._row = row

    async def get_evidence(self, tenant_id, evidence_id):
        return self._row


@pytest.mark.asyncio
async def test_tenant_fetch_resolves_endpoint_from_stored_index():  # type: ignore[no-untyped-def]
    tenant_client = MagicMock()
    tenant_client.search = AsyncMock(return_value=_search_response([_hit("d9")]))
    adapter = AsyncElasticAdapter(
        client=MagicMock(), settings=_settings(), tenant_router=_router_with(tenant_client)
    )
    iid, eid = uuid.uuid4(), uuid.uuid4()
    row = _Row("tenant-a", iid, eid, "elasticsearch://logs-orders-7/doc9")
    result = await adapter.get_runtime_evidence("tenant-a", iid, eid, "production", _Lookup(row))
    assert tenant_client.search.call_count == 1
    assert result is not None


@pytest.mark.asyncio
async def test_tenant_fetch_outside_patterns_fails_closed():  # type: ignore[no-untyped-def]
    adapter = AsyncElasticAdapter(
        client=MagicMock(), settings=_settings(), tenant_router=_router_with(MagicMock())
    )
    iid, eid = uuid.uuid4(), uuid.uuid4()
    row = _Row("tenant-a", iid, eid, "elasticsearch://logs-other-1/doc9")
    with pytest.raises(EvidenceNotFoundException):
        await adapter.get_runtime_evidence("tenant-a", iid, eid, "production", _Lookup(row))


def test_evidence_config_carries_telemetry_dir():  # type: ignore[no-untyped-def]
    config = EvidenceConfig()
    assert config.tenant_telemetry_dir == ""


# --- auth: kinds, precedence, TLS, Fernet ------------------------------------


def _auth_endpoint(**overrides):  # type: ignore[no-untyped-def]

    from investigation_agent_platform.infrastructure.evidence.runtime.tenant_telemetry import (
        TelemetryEndpoint as _TE,
    )

    kwargs = {
        "name": "e",
        "url": "https://h.example.com",
        "services": ("svc",),
    }
    kwargs.update(overrides)
    return _TE(**kwargs)


def test_auth_precedence_api_key_over_bearer_over_basic():  # type: ignore[no-untyped-def]
    from pydantic import SecretStr

    full = _auth_endpoint(
        user="u",
        password=SecretStr("p"),
        api_key=SecretStr("k"),
        bearer_token=SecretStr("t"),
    )
    assert full.auth_kind() == "api_key"
    assert full.connection_kwargs()["api_key"] == "k"
    assert "bearer_auth" not in full.connection_kwargs()
    assert "basic_auth" not in full.connection_kwargs()

    bearer_basic = _auth_endpoint(user="u", password=SecretStr("p"), bearer_token=SecretStr("t"))
    assert bearer_basic.auth_kind() == "bearer"
    assert bearer_basic.connection_kwargs()["bearer_auth"] == "t"

    basic = _auth_endpoint(user="u", password=SecretStr("p"))
    assert basic.auth_kind() == "basic"
    assert basic.connection_kwargs()["basic_auth"] == ("u", "p")

    assert _auth_endpoint().auth_kind() == "none"
    assert "api_key" not in _auth_endpoint().connection_kwargs()


def test_connection_kwargs_carry_tls_and_timeout():  # type: ignore[no-untyped-def]
    kwargs = _auth_endpoint(timeout_seconds=120).connection_kwargs()
    assert kwargs["request_timeout"] == 120
    assert kwargs["verify_certs"] is True
    assert "ca_certs" not in kwargs

    insecure = _auth_endpoint(verify_certs=False).connection_kwargs()
    assert insecure["verify_certs"] is False


def test_ca_certs_validated_at_load(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    bundle = tmp_path / "ca.pem"
    bundle.write_text("fake-pem", encoding="utf-8")
    endpoint = _auth_endpoint(ca_certs=str(bundle))
    assert endpoint.connection_kwargs()["ca_certs"] == str(bundle)

    with pytest.raises(Exception, match="ca_certs"):
        _auth_endpoint(ca_certs=str(tmp_path / "missing.pem"))

    with pytest.raises(Exception, match="verify_certs is false"):
        _auth_endpoint(verify_certs=False, ca_certs=str(bundle))

    # Env override form.
    _conf(tmp_path, "tenant-a", HAPPY)
    monkeypatch.setenv("IAP_ES_TENANT_A_ORDERS_ES_CA_CERTS", str(bundle))
    configs = load_tenant_telemetry_configs(tmp_path)
    assert configs["tenant-a"].endpoint_for("svc-a").ca_certs == str(bundle)


def test_verify_certs_strict(tmp_path):  # type: ignore[no-untyped-def]
    _conf(
        tmp_path,
        "tenant-a",
        "[e]\nurl = https://h.example.com\nverify_certs = maybe\nservices = svc\n",
    )
    with pytest.raises(PlatformConfigurationError, match="non-boolean"):
        load_tenant_telemetry_configs(tmp_path)


def _fernet_pair(secret):  # type: ignore[no-untyped-def]
    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    token = Fernet(key).encrypt(secret.encode("utf-8")).decode("utf-8")
    return key.decode("utf-8"), f"fernet:{token}"


def test_fernet_password_round_trip(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    key, envelope = _fernet_pair("s3cr3t-basic-pw")
    monkeypatch.setenv("IAP_ES_FERNET_KEY", key)
    _conf(
        tmp_path,
        "tenant-a",
        f"[e]\nurl = https://h.example.com\nuser = svc-account\npassword = {envelope}\nservices = svc\n",
    )
    endpoint = load_tenant_telemetry_configs(tmp_path)["tenant-a"].endpoints[0]
    assert endpoint.auth_kind() == "basic"
    assert endpoint.connection_kwargs()["basic_auth"] == ("svc-account", "s3cr3t-basic-pw")


def test_fernet_bearer_and_api_key(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    from cryptography.fernet import Fernet

    raw_key = Fernet.generate_key()
    key = raw_key.decode("utf-8")
    bearer_env = "fernet:" + Fernet(raw_key).encrypt(b"oauth-access-token").decode("utf-8")
    api_env = "fernet:" + Fernet(raw_key).encrypt(b"encoded-api-key").decode("utf-8")
    monkeypatch.setenv("IAP_ES_FERNET_KEY", key)
    _conf(
        tmp_path,
        "tenant-a",
        f"[e]\nurl = https://h.example.com\nbearer_token = {bearer_env}\n"
        f"api_key = {api_env}\nservices = svc\n",
    )
    endpoint = load_tenant_telemetry_configs(tmp_path)["tenant-a"].endpoints[0]
    # api_key wins over bearer by precedence; both decrypt.
    assert endpoint.auth_kind() == "api_key"
    assert endpoint.connection_kwargs()["api_key"] == "encoded-api-key"


def test_fernet_missing_key_fails_closed(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    _, envelope = _fernet_pair("x")
    monkeypatch.delenv("IAP_ES_FERNET_KEY", raising=False)
    _conf(
        tmp_path,
        "tenant-a",
        f"[e]\nurl = https://h.example.com\nuser = u\npassword = {envelope}\nservices = svc\n",
    )
    with pytest.raises(PlatformConfigurationError, match="IAP_ES_FERNET_KEY.*not set"):
        load_tenant_telemetry_configs(tmp_path)


def test_fernet_corrupt_token_and_empty_secret_fail_closed(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setenv("IAP_ES_FERNET_KEY", key)
    _conf(
        tmp_path,
        "tenant-a",
        "[e]\nurl = https://h.example.com\nuser = u\npassword = fernet:corrupt!!\nservices = svc\n",
    )
    with pytest.raises(PlatformConfigurationError, match="outside url-safe base64"):
        load_tenant_telemetry_configs(tmp_path)

    empty_token = Fernet(key.encode()).encrypt(b"").decode("utf-8")
    _conf(
        tmp_path,
        "tenant-a",
        f"[e]\nurl = https://h.example.com\nuser = u\npassword = fernet:{empty_token}\nservices = svc\n",
    )
    with pytest.raises(PlatformConfigurationError, match="empty secret"):
        load_tenant_telemetry_configs(tmp_path)


def test_fernet_via_env_override(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    key, envelope = _fernet_pair("env-supplied-pw")
    monkeypatch.setenv("IAP_ES_FERNET_KEY", key)
    _conf(
        tmp_path,
        "tenant-a",
        "[e]\nurl = https://h.example.com\nuser = u\npassword = plain\nservices = svc\n",
    )
    monkeypatch.setenv("IAP_ES_TENANT_A_E_PASSWORD", envelope)
    endpoint = load_tenant_telemetry_configs(tmp_path)["tenant-a"].endpoints[0]
    assert endpoint.connection_kwargs()["basic_auth"] == ("u", "env-supplied-pw")


# --- oauth client-credentials ------------------------------------------------

import asyncio as _asyncio

from investigation_agent_platform.domain.common.exceptions import (
    ExecutionError as _ExecutionError,
)
from investigation_agent_platform.domain.common.exceptions import (
    ProviderUnavailableException as _ProviderUnavailable,
)
from investigation_agent_platform.infrastructure.evidence.runtime.tenant_telemetry import (
    OAuthBearerClient as _OAuthBearerClient,
)
from investigation_agent_platform.infrastructure.evidence.runtime.tenant_telemetry import (
    OAuthSettings as _OAuthSettings,
)
from investigation_agent_platform.infrastructure.evidence.runtime.tenant_telemetry import (
    OAuthTokenProvider as _OAuthTokenProvider,
)


def _oauth_endpoint(**overrides):  # type: ignore[no-untyped-def]
    kwargs = {
        "name": "e",
        "url": "https://h.example.com",
        "services": ("svc",),
        "oauth_token_url": "https://idp.example.com/oauth/token",
        "oauth_client_id": "cid",
    }
    kwargs.update(overrides)
    return _auth_endpoint(**kwargs)


def _secret(value):  # type: ignore[no-untyped-def]
    from pydantic import SecretStr

    return SecretStr(value)


def test_oauth_requires_full_trio():  # type: ignore[no-untyped-def]
    with pytest.raises(Exception, match="oauth requires"):
        _oauth_endpoint(oauth_client_secret=None or _secret(""))
    with pytest.raises(Exception, match="oauth requires"):
        _oauth_endpoint(oauth_client_id=None)
    with pytest.raises(Exception, match="oauth requires"):
        _auth_endpoint(
            name="e",
            url="https://h.example.com",
            services=("svc",),
            oauth_token_url="https://idp.example.com/oauth/token",
        )


def test_oauth_conflicts_with_static_bearer():  # type: ignore[no-untyped-def]
    with pytest.raises(Exception, match="exactly one"):
        _oauth_endpoint(oauth_client_secret=_secret("s"), bearer_token=_secret("static"))


def test_oauth_token_url_scheme():  # type: ignore[no-untyped-def]
    assert _oauth_endpoint(oauth_client_secret=_secret("s")).auth_kind() == "oauth"
    with pytest.raises(Exception, match="https"):
        _oauth_endpoint(
            oauth_client_secret=_secret("s"),
            oauth_token_url="http://idp.example.com/oauth/token",
        )
    loopback = _oauth_endpoint(
        oauth_client_secret=_secret("s"),
        oauth_token_url="http://127.0.0.1:9/token",
    )
    assert loopback.auth_kind() == "oauth"


def test_oauth_settings_accessor_and_bare_kwargs():  # type: ignore[no-untyped-def]
    assert _auth_endpoint().oauth_settings() is None
    endpoint = _oauth_endpoint(oauth_client_secret=_secret("s"), oauth_scope="read write")
    settings = endpoint.oauth_settings()
    assert settings is not None and settings.client_id == "cid"
    assert settings.scope == "read write"
    kwargs = endpoint.connection_kwargs()
    assert "api_key" not in kwargs and "bearer_auth" not in kwargs
    assert "basic_auth" not in kwargs


def _provider(payloads, **overrides):  # type: ignore[no-untyped-def]
    calls: list[int] = []

    async def fetch():  # type: ignore[no-untyped-def]
        calls.append(1)
        payload = payloads[min(len(calls) - 1, len(payloads) - 1)]
        if isinstance(payload, BaseException):
            raise payload
        return dict(payload)

    settings = _OAuthSettings(
        token_url="https://idp.example.com/oauth/token",
        client_id="cid",
        client_secret=_secret("s"),
    )
    return _OAuthTokenProvider(settings, fetch=fetch, **overrides), calls


@pytest.mark.asyncio
async def test_oauth_token_cached_for_validity_period():  # type: ignore[no-untyped-def]
    provider, calls = _provider([{"access_token": "tok-1", "expires_in": 3600}])
    first = await provider.token()
    second = await provider.token()
    third = await provider.token()
    assert first == second == third == "tok-1"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_oauth_refreshes_after_expiry():  # type: ignore[no-untyped-def]
    provider, calls = _provider(
        [
            {"access_token": "tok-1", "expires_in": 1},
            {"access_token": "tok-2", "expires_in": 3600},
        ]
    )
    assert await provider.token() == "tok-1"
    await _asyncio.sleep(1.2)
    assert await provider.token() == "tok-2"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_oauth_missing_expires_in_fails_closed():  # type: ignore[no-untyped-def]
    provider, calls = _provider([{"access_token": "tok-1"}])
    with pytest.raises(_ExecutionError, match="omitted expires_in"):
        await provider.token()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_oauth_bad_lifetime_fails_closed():  # type: ignore[no-untyped-def]
    for bad in ("soon", 0, -5):
        provider, _ = _provider([{"access_token": "tok-1", "expires_in": bad}])
        with pytest.raises(_ExecutionError):
            await provider.token()


@pytest.mark.asyncio
async def test_oauth_empty_token_fails_closed():  # type: ignore[no-untyped-def]
    provider, _ = _provider([{"access_token": "", "expires_in": 3600}])
    with pytest.raises(_ExecutionError, match="no access token"):
        await provider.token()


@pytest.mark.asyncio
async def test_oauth_fetch_failure_not_cached():  # type: ignore[no-untyped-def]
    err = _ProviderUnavailable("down")
    provider, calls = _provider([err, {"access_token": "tok-2", "expires_in": 3600}])
    with pytest.raises(_ProviderUnavailable):
        await provider.token()
    assert await provider.token() == "tok-2"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_oauth_concurrent_callers_single_fetch():  # type: ignore[no-untyped-def]
    provider, calls = _provider([{"access_token": "tok-1", "expires_in": 3600}])
    tokens = await _asyncio.gather(*[provider.token() for _ in range(10)])
    assert set(tokens) == {"tok-1"}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_oauth_wrapper_injects_bearer_per_request():  # type: ignore[no-untyped-def]
    seen: list[dict] = []

    class _Options:
        async def search(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            seen.append(kwargs)
            return {"ok": True}

    class _Inner:
        def __init__(self):  # type: ignore[no-untyped-def]
            self.closed = False
            self.option_calls: list[dict] = []

        def options(self, **kwargs):  # type: ignore[no-untyped-def]
            self.option_calls.append(kwargs)
            return _Options()

        async def close(self):  # type: ignore[no-untyped-def]
            self.closed = True

    inner = _Inner()
    provider, _ = _provider([{"access_token": "cached-tok", "expires_in": 3600}])
    client = _OAuthBearerClient(inner, provider)
    assert await client.search(index="logs-*", body={}) == {"ok": True}
    assert inner.option_calls == [{"bearer_auth": "cached-tok"}]
    await client.close()
    assert inner.closed is True


def test_oauth_loader_round_trip_with_fernet(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    key, envelope = _fernet_pair("oauth-client-secret")
    monkeypatch.setenv("IAP_ES_FERNET_KEY", key)
    _conf(
        tmp_path,
        "tenant-a",
        "[e]\nurl = https://h.example.com\n"
        "oauth_token_url = https://idp.example.com/oauth/token\n"
        "oauth_client_id = cid\n"
        f"oauth_client_secret = {envelope}\n"
        "oauth_scope = telemetry.read\n"
        "services = svc\n",
    )
    endpoint = load_tenant_telemetry_configs(tmp_path)["tenant-a"].endpoints[0]
    assert endpoint.auth_kind() == "oauth"
    settings = endpoint.oauth_settings()
    assert settings is not None
    assert settings.client_secret.get_secret_value() == "oauth-client-secret"
    assert settings.scope == "telemetry.read"


def test_oauth_env_overrides(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    _conf(
        tmp_path,
        "tenant-a",
        "[e]\nurl = https://h.example.com\n"
        "oauth_token_url = https://idp.example.com/oauth/token\n"
        "oauth_client_id = cid\n"
        "oauth_client_secret = s\n"
        "services = svc\n",
    )
    monkeypatch.setenv("IAP_ES_TENANT_A_E_OAUTH_SCOPE", "telemetry.read")
    endpoint = load_tenant_telemetry_configs(tmp_path)["tenant-a"].endpoints[0]
    assert endpoint.oauth_settings() is not None
    assert endpoint.oauth_settings().scope == "telemetry.read"


def test_fernet_two_part_inline_cipher_round_trip(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    from cryptography.fernet import Fernet

    monkeypatch.delenv("IAP_ES_FERNET_KEY", raising=False)
    raw_key = Fernet.generate_key()
    token = Fernet(raw_key).encrypt(b"two-part-pw").decode("utf-8")
    envelope = f"fernet:{raw_key.decode('utf-8')}:{token}"
    _conf(
        tmp_path,
        "tenant-a",
        f"[e]\nurl = https://h.example.com\nuser = u\npassword = {envelope}\nservices = svc\n",
    )
    endpoint = load_tenant_telemetry_configs(tmp_path)["tenant-a"].endpoints[0]
    assert endpoint.auth_kind() == "basic"
    assert endpoint.connection_kwargs()["basic_auth"] == ("u", "two-part-pw")


def test_fernet_two_part_malformed_fails_closed(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    from cryptography.fernet import Fernet

    monkeypatch.delenv("IAP_ES_FERNET_KEY", raising=False)
    raw_key = Fernet.generate_key()
    good_token = Fernet(raw_key).encrypt(b"x").decode("utf-8")
    bad_cases = [
        "fernet::" + good_token,  # empty cipher
        "fernet:" + raw_key.decode("utf-8") + ":",  # empty token
        "fernet:not-a-key:" + good_token,  # bad cipher
        "fernet:" + raw_key.decode("utf-8") + ":corrupt!!",  # corrupt token
    ]
    for envelope in bad_cases:
        _conf(
            tmp_path,
            "tenant-a",
            f"[e]\nurl = https://h.example.com\nuser = u\npassword = {envelope}\nservices = svc\n",
        )
        with pytest.raises(PlatformConfigurationError):
            load_tenant_telemetry_configs(tmp_path)


def test_fernet_two_part_wrong_cipher_fails_closed(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    from cryptography.fernet import Fernet

    monkeypatch.delenv("IAP_ES_FERNET_KEY", raising=False)
    token = Fernet.generate_key()
    other = Fernet.generate_key()
    envelope = f"fernet:{other.decode('utf-8')}:{Fernet(token).encrypt(b'x').decode('utf-8')}"
    _conf(
        tmp_path,
        "tenant-a",
        f"[e]\nurl = https://h.example.com\nuser = u\npassword = {envelope}\nservices = svc\n",
    )
    with pytest.raises(PlatformConfigurationError, match="failed to decrypt"):
        load_tenant_telemetry_configs(tmp_path)


def test_fernet_diagnostics_name_the_bad_part(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    from cryptography.fernet import Fernet

    monkeypatch.delenv("IAP_ES_FERNET_KEY", raising=False)
    raw_key = Fernet.generate_key()
    good_token = Fernet(raw_key).encrypt(b"x").decode("utf-8")
    cases = [
        (f"fernet:{raw_key.decode('utf-8')}:has space", "outside url-safe base64"),
        ("fernet:short:" + good_token, "cipher is 5 characters"),
        ("fernet:b'abc':" + good_token, "outside url-safe base64"),
    ]
    for envelope, fragment in cases:
        _conf(
            tmp_path,
            "tenant-a",
            f"[e]\nurl = https://h.example.com\nuser = u\npassword = {envelope}\nservices = svc\n",
        )
        with pytest.raises(PlatformConfigurationError, match=fragment):
            load_tenant_telemetry_configs(tmp_path)
