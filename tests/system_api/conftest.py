"""Shared fixtures for the system/API suite (design §4).

Environment contract::

    TEST_TARGET_URL          API base (default http://127.0.0.1:8000)
    OPENAPI_URL              spec location (default ${TEST_TARGET_URL}/openapi.json)
    TEST_TENANT_ID           scratch tenant (default system-api-suite; never shared)
    TEST_API_APPLICATION_ID  application_id for POST /investigations (default example;
                             must exist in the profile store for TEST_TENANT_ID)
    TEST_AUTH_TOKEN          optional Bearer token
    TEST_API_FUZZ_CLASSES    safety classes in the fuzz corpus (default READ,WRITE_BOUNDED)
    TEST_API_MAX_EXAMPLES    Hypothesis max_examples (default 10)
    TEST_API_REQUEST_TIMEOUT per-request seconds (default 10)
    TEST_API_FLOWS           enabled flow modules (default investigation,chat)

``TEST_TARGET_URL`` falls back to ``IAP_LIVE_BASE_URL``; ``TEST_TENANT_ID``
falls back to ``IAP_LIVE_TENANT``. The whole package is skipped unless
``IAP_LIVE_TESTS=1`` or ``TEST_TARGET_URL`` is present in ``os.environ``.

Auth headers are set at the Schemathesis config level (``config.update``), so
they apply to every request in every phase — stronger than a ``map_*`` hook,
which does not run for every phase.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from .scenarios import SCENARIOS, Safety, inject_tolerated_responses

TARGET_URL = os.environ.get(
    "TEST_TARGET_URL",
    os.environ.get("IAP_LIVE_BASE_URL", "http://127.0.0.1:8000"),
).rstrip("/")
OPENAPI_URL = os.environ.get("OPENAPI_URL", f"{TARGET_URL}/openapi.json")
TENANT_ID = os.environ.get("TEST_TENANT_ID", os.environ.get("IAP_LIVE_TENANT", "system-api-suite"))
APPLICATION_ID = os.environ.get("TEST_API_APPLICATION_ID", "example")
AUTH_TOKEN = os.environ.get("TEST_AUTH_TOKEN")
FUZZ_CLASSES = frozenset(
    c.strip().upper()
    for c in os.environ.get("TEST_API_FUZZ_CLASSES", "READ,WRITE_BOUNDED").split(",")
    if c.strip()
)
MAX_EXAMPLES = int(os.environ.get("TEST_API_MAX_EXAMPLES", "10"))
REQUEST_TIMEOUT = float(os.environ.get("TEST_API_REQUEST_TIMEOUT", "10"))
FLOWS = frozenset(
    f.strip().lower() for f in os.environ.get("TEST_API_FLOWS", "investigation,chat").split(",")
)

SUITE_ENABLED = os.environ.get("IAP_LIVE_TESTS") == "1" or "TEST_TARGET_URL" in os.environ

needs_live: pytest.MarkDecorator = pytest.mark.skipif(
    not SUITE_ENABLED, reason="live API suite disabled"
)


@dataclass(frozen=True)
class ApiConfig:
    target_url: str
    openapi_url: str
    tenant_id: str
    application_id: str
    auth_token: str | None
    fuzz_classes: frozenset[str]
    max_examples: int
    request_timeout: float
    flows: frozenset[str]


def auth_headers(token: str | None = AUTH_TOKEN, tenant_id: str = TENANT_ID) -> dict[str, str]:
    headers = {"X-Tenant-ID": tenant_id}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


@pytest.fixture(scope="session")
def api_config() -> ApiConfig:
    return ApiConfig(
        target_url=TARGET_URL,
        openapi_url=OPENAPI_URL,
        tenant_id=TENANT_ID,
        application_id=APPLICATION_ID,
        auth_token=AUTH_TOKEN,
        fuzz_classes=FUZZ_CLASSES,
        max_examples=MAX_EXAMPLES,
        request_timeout=REQUEST_TIMEOUT,
        flows=FLOWS,
    )


@pytest.fixture(scope="session")
def http_client(api_config: ApiConfig) -> Any:
    with httpx.Client(
        base_url=api_config.target_url,
        headers=auth_headers(api_config.auth_token, api_config.tenant_id),
        timeout=api_config.request_timeout,
    ) as client:
        yield client


@pytest.fixture(scope="session", autouse=True)
def require_ready_stack(http_client: Any, api_config: ApiConfig) -> None:
    """Fail fast unless the stack reports READY (design §4.3)."""
    try:
        response = http_client.get("/api/v1/health/ready")
    except httpx.HTTPError as exc:
        pytest.exit(f"API not reachable at {api_config.target_url}: {exc}", returncode=2)
        raise AssertionError("unreachable")  # for type-checkers; exit raises
    body: Any = None
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code != 200 or not isinstance(body, dict) or body.get("status") != "READY":
        pytest.exit(
            f"stack at {api_config.target_url} is not READY "
            f"(GET /api/v1/health/ready → {response.status_code} {body}); "
            "start it first: ./scripts/start-platform.sh",
            returncode=2,
        )


@pytest.fixture(scope="session")
def live_spec(http_client: Any, api_config: ApiConfig) -> dict[str, Any]:
    from typing import cast

    response = http_client.get(api_config.openapi_url)
    response.raise_for_status()
    return cast("dict[str, Any]", response.json())


def local_spec() -> dict[str, Any]:
    """The spec generated import-time from the app (no network)."""
    from typing import cast

    from investigation_agent_platform.api.app import create_app

    return cast("dict[str, Any]", create_app().openapi())


@pytest.fixture(scope="session")
def api_schema(live_spec: dict[str, Any], api_config: ApiConfig) -> Any:
    """Schemathesis schema for the fuzz corpus (deferred: no network at collection).

    Tolerated statuses come from the registry (per-route, never blanket).
    Class exclusions are applied here for direct fixture use; the lazy
    ``from_fixture`` parametrization path re-applies them via
    ``LazySchema.exclude`` (the loader clones the schema and would otherwise
    drop fixture-level filters).
    """
    import schemathesis.pytest  # noqa: F401  (registers the pytest plugin entry point)
    from schemathesis import openapi

    schema: Any = openapi.from_dict(inject_tolerated_responses(live_spec))
    schema.config.update(
        base_url=api_config.target_url,
        headers=auth_headers(api_config.auth_token, api_config.tenant_id),
        request_timeout=api_config.request_timeout,
    )
    always_excluded = {Safety.WORKFLOW_EXPENSIVE, Safety.STREAMING}
    for scenario in SCENARIOS:
        if (
            scenario.safety in always_excluded
            or scenario.safety.value not in api_config.fuzz_classes
        ):
            schema = schema.exclude(method=scenario.method, path=scenario.path_template)
    return schema


def _create(url: str, client: Any, **kwargs: Any) -> dict[str, Any]:
    from typing import cast

    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("X-Idempotency-Key", uuid.uuid4().hex)
    response = client.post(url, headers=headers, **kwargs)
    if response.status_code not in (200, 201, 202):
        pytest.fail(
            f"suite setup {url} → {response.status_code}: {response.text[:500]} "
            f"(seed TEST_API_APPLICATION_ID={APPLICATION_ID!r} "
            f"for tenant {TENANT_ID!r} if POST /investigations fails)",
            pytrace=False,
        )
    return cast("dict[str, Any]", response.json())


@pytest.fixture(scope="session")
def suite_ids(http_client: Any) -> dict[str, str]:
    """Real investigation + chat session ids for id injection and real-id probes."""
    investigation = _create(
        "/api/v1/investigations",
        http_client,
        json={"application_id": APPLICATION_ID, "problem_description": "system-api suite setup"},
    )
    session = _create("/api/v1/chat/sessions", http_client, json={})
    inv_id = investigation.get("id") or investigation.get("investigation_id")
    sess = session.get("session") or {}
    sess_id = sess.get("id") or session.get("session_id")
    if not inv_id or not sess_id:
        pytest.fail(f"suite setup returned no ids: {investigation!r} {session!r}", pytrace=False)
    return {"investigation_id": str(inv_id), "session_id": str(sess_id)}
