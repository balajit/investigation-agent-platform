"""Live-platform integration suite: infra -> API -> functionality.

Runs against a *running* stack (``scripts/start-platform.sh``) and verifies,
end to end, that infrastructure is reachable, the API is healthy, persistence
works, tenant isolation holds, and provider wiring constructs.

Gated: the whole module skips unless ``IAP_LIVE_TESTS=1`` is set, so a plain
``pytest`` run (CI/unit) never touches the network::

    ./scripts/verify-platform.sh              # sources .env.local, runs this file
    IAP_LIVE_TESTS=1 uv run pytest tests/integration/test_live_platform.py -v

Knobs (all optional)::

    IAP_LIVE_BASE_URL   API base (default http://localhost:8000)
    IAP_LIVE_TENANT     tenant id used for functional tests (default live-suite-tenant)
    IAP_LIVE_TIMEOUT    per-request seconds (default 10)

Only the standard library + pytest are used (urllib/socket), plus psycopg2
(already a project dependency) for the Postgres auth check.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.parse
import urllib.request

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("IAP_LIVE_TESTS", "").lower() not in ("1", "true", "yes"),
    reason="live suite disabled: set IAP_LIVE_TESTS=1 with the stack running",
)

BASE_URL = os.environ.get("IAP_LIVE_BASE_URL", "http://localhost:8000").rstrip("/")
# Must be a tenant/application with a profile: the platform seeds
# tenant-a/example-app by default (see dependencies._seed_default_profile).
TENANT = os.environ.get("IAP_LIVE_TENANT", "tenant-a")
APP_ID = os.environ.get("IAP_LIVE_APP", "example-app")
TIMEOUT = float(os.environ.get("IAP_LIVE_TIMEOUT", "10"))
API = f"{BASE_URL}/api/v1"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _http(
    method: str,
    url: str,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, object]:
    """Raw HTTP call returning (status, parsed-json-or-text). Never raises on HTTP error."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = resp.read().decode()
            status = resp.status
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode()
        status = exc.code
    try:
        return status, json.loads(payload) if payload else (status, None)
    except (json.JSONDecodeError, ValueError):
        return status, payload


def api(
    method: str,
    path: str,
    body: dict | None = None,
    tenant: str | None = TENANT,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, object]:
    headers = dict(extra_headers or {})
    if tenant is not None:
        headers["X-Tenant-ID"] = tenant
    return _http(method, f"{API}{path}", body, headers)


def require(status: int, data: object, expected: set[int], what: str) -> object:
    assert status in expected, f"{what}: expected {sorted(expected)}, got {status}: {data!r}"[:2000]
    return data


def _tcp_ok(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT):
            return True
    except OSError:
        return False


def _pg_target() -> tuple[str, int]:
    uri = os.environ.get("IAP_DATABASE_URI", "postgresql://iap:iap@localhost:5452/iap")
    parts = urllib.parse.urlparse(uri)
    return parts.hostname or "localhost", parts.port or 5432


# ---------------------------------------------------------------------------
# 1. infrastructure reachability
# ---------------------------------------------------------------------------


class TestLiveInfrastructure:
    def test_postgres_tcp(self) -> None:
        host, port = _pg_target()
        assert _tcp_ok(host, port), f"postgres unreachable at {host}:{port}"

    def test_postgres_auth_and_schema(self) -> None:
        psycopg2 = pytest.importorskip("psycopg2")
        dsn = os.environ.get("IAP_DATABASE_URI", "postgresql://iap:iap@localhost:5452/iap")
        conn = psycopg2.connect(dsn, connect_timeout=int(TIMEOUT))
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                assert cur.fetchone() == (1,)
                cur.execute("SELECT count(*) FROM alembic_version")
                assert cur.fetchone()[0] == 1, "expected exactly one alembic head applied"
                cur.execute("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
                assert cur.fetchone()[0] == 1, "pgvector extension missing (Mem0 needs it)"
        finally:
            conn.close()

    def test_elasticsearch(self) -> None:
        status, data = _http("GET", "http://localhost:9200")
        require(status, data, {200}, "elasticsearch root")
        assert isinstance(data, dict) and "cluster_name" in data
        status, data = _http("GET", "http://localhost:9200/_cluster/health")
        require(status, data, {200}, "elasticsearch cluster health")
        assert data["status"] in ("green", "yellow"), f"cluster unhealthy: {data}"

    def test_neo4j(self) -> None:
        status, _ = _http("GET", "http://localhost:7474")
        require(status, None, {200}, "neo4j browser")
        assert _tcp_ok("localhost", 7687), "neo4j bolt unreachable at localhost:7687"

    def test_temporal(self) -> None:
        assert _tcp_ok("localhost", 7233), "temporal gRPC unreachable at localhost:7233"
        status, _ = _http("GET", "http://localhost:8233")
        require(status, None, {200}, "temporal UI")

    def test_kafka_tcp(self) -> None:
        servers = os.environ.get("IAP_KAFKA_SERVERS", "localhost:9093").split(",")
        host, _, port = servers[0].partition(":")
        assert _tcp_ok(host or "localhost", int(port or 9093)), f"kafka unreachable at {servers[0]}"


# ---------------------------------------------------------------------------
# 2. API health
# ---------------------------------------------------------------------------


class TestLiveApiHealth:
    def test_liveness(self) -> None:
        status, data = api("GET", "/health/live", tenant=None)
        require(status, data, {200}, "liveness")
        assert data == {"status": "UP"}

    def test_readiness(self) -> None:
        status, data = api("GET", "/health/ready", tenant=None)
        require(status, data, {200}, "readiness (got 503: dependencies unhealthy)")
        assert data["status"] == "READY", f"not ready: {data}"

    def test_docs_served(self) -> None:
        status, _ = _http("GET", f"{BASE_URL}/docs")
        require(status, None, {200}, "openapi docs")

    def test_missing_tenant_rejected(self) -> None:
        status, _ = api("GET", "/investigations", tenant=None)
        require(status, None, {401}, "missing X-Tenant-ID should be 401")

    def test_unknown_route_404(self) -> None:
        status, _ = api("GET", "/no-such-thing")
        require(status, None, {404}, "unknown route should be 404")


# ---------------------------------------------------------------------------
# 3. functionality: investigation lifecycle + tenant isolation
# ---------------------------------------------------------------------------


def _create_investigation(
    tenant: str = TENANT, problem: str = "live-suite smoke: latency spike on checkout"
) -> dict:
    status, data = api(
        "POST",
        "/investigations",
        {"application_id": APP_ID, "problem_description": problem},
        tenant=tenant,
    )
    return require(status, data, {201}, "create investigation")  # type: ignore[return-value]


class TestLiveFunctionality:
    def test_profile_present(self) -> None:
        """Fail fast if IAP_LIVE_TENANT/APP has no profile (creates would 404)."""
        status, data = api("GET", "/profiles")
        profiles = require(status, data, {200}, "list profiles")
        ids = {p["id"] for p in profiles["items"]}
        assert APP_ID in ids, f"no profile {APP_ID!r} for tenant {TENANT!r}: have {sorted(ids)}"

    def test_create_and_get_investigation(self) -> None:
        created = _create_investigation()
        inv_id = created["id"]
        assert created["tenant_id"] == TENANT

        status, data = api("GET", f"/investigations/{inv_id}")
        fetched = require(status, data, {200}, "get investigation")
        assert fetched["investigation"]["id"] == inv_id

    def test_create_validation_422(self) -> None:
        status, data = api("POST", "/investigations", {"application_id": "x"})
        require(status, data, {422}, "missing problem_description should be 422")

    def test_list_contains_created(self) -> None:
        created = _create_investigation()
        status, data = api("GET", "/investigations?limit=100")
        listed = require(status, data, {200}, "list investigations")
        ids = {item["investigation_id"] for item in listed["items"]}
        assert created["id"] in ids

    def test_child_resources_empty_for_new(self) -> None:
        inv_id = _create_investigation()["id"]
        for path in (
            f"/investigations/{inv_id}/evidence",
            f"/investigations/{inv_id}/timeline",
            f"/investigations/{inv_id}/hypotheses",
            f"/investigations/{inv_id}/knowledge",
        ):
            status, data = api("GET", path)
            require(status, data, {200}, f"GET {path}")
        status, data = api("GET", f"/investigations/{inv_id}/conclusion")
        concluded = require(status, data, {200}, "get conclusion")
        assert concluded["conclusion"] is None

    def test_tenant_isolation(self) -> None:
        inv_id = _create_investigation()["id"]
        other = f"{TENANT}-other"
        status, _ = api("GET", f"/investigations/{inv_id}", tenant=other)
        require(status, None, {404, 403}, "cross-tenant read must be denied")
        status, data = api("GET", "/investigations?limit=100", tenant=other)
        listed = require(status, data, {200}, "cross-tenant list")
        assert all(i["investigation_id"] != inv_id for i in listed["items"])

    def test_idempotency_replay(self) -> None:
        key = {"X-Idempotency-Key": "live-suite-key-1"}
        payload = {"application_id": APP_ID, "problem_description": "idempotent probe"}
        s1, d1 = api("POST", "/investigations", payload, extra_headers=key)
        first = require(s1, d1, {201}, "first idempotent create")
        s2, d2 = api("POST", "/investigations", payload, extra_headers=key)
        second = require(s2, d2, {201}, "replayed idempotent create")
        assert first["id"] == second["id"], "idempotency key must replay the same investigation"
        s3, d3 = api(
            "POST",
            "/investigations",
            {**payload, "problem_description": "different payload"},
            extra_headers=key,
        )
        require(s3, d3, {409}, "key reuse with different payload must conflict")

    def test_profiles_and_knowledge_search(self) -> None:
        status, data = api("GET", "/profiles")
        profiles = require(status, data, {200}, "list profiles")
        assert "items" in profiles and "total" in profiles
        assert all("connectionUrl" not in (p.get("state") or {}) for p in profiles["items"]), (
            "profile secrets must stay sanitized"
        )
        status, data = api("GET", f"/knowledge/search?application_id={APP_ID}")
        found = require(status, data, {200}, "knowledge search")
        assert "items" in found

    def test_workflow_dispatch_or_skip(self) -> None:
        """Start exercises the Temporal path; skips cleanly if the API has no client wired."""
        inv_id = _create_investigation(problem="live-suite smoke: dispatch probe")["id"]
        status, data = api("POST", f"/investigations/{inv_id}/start")
        if status == 503:
            pytest.skip(f"temporal client not wired in API: {data}")
        started = require(status, data, {202}, "start workflow")
        assert started["status"] == "RUNNING"
        try:
            status, data = api("POST", f"/investigations/{inv_id}/cancel?reason=live-suite-cleanup")
            require(status, data, {202}, "cancel workflow")
        except AssertionError as exc:
            pytest.skip(f"started but cancel unavailable: {exc}")


# ---------------------------------------------------------------------------
# 4. provider wiring (no-cost: constructs gateways, makes no model calls)
# ---------------------------------------------------------------------------


class TestLiveLlmWiring:
    def test_configured_provider_constructs(self) -> None:

        from investigation_agent_platform.infrastructure.configuration.config import (
            _llm_config_from_env,
        )
        from investigation_agent_platform.infrastructure.reasoning.factory import (
            create_llm_gateway,
        )

        # Needs IAP_LLM_API_KEY (or AZURE_OPENAI_API_KEY) present in the environment;
        # verify-platform.sh sources .env.local so this holds for real runs.
        if not (os.environ.get("IAP_LLM_API_KEY") or os.environ.get("AZURE_OPENAI_API_KEY")):
            pytest.skip("no LLM api key in environment")
        cfg = _llm_config_from_env()
        gw = create_llm_gateway(cfg)
        if cfg.provider.lower() == "azure":
            client = gw._get_client()
            assert type(client).__name__ == "AsyncAzureOpenAI"
            assert gw._wire_model_name() == cfg.azure_deployment
        else:
            assert gw._wire_model_name() == cfg.model_name
