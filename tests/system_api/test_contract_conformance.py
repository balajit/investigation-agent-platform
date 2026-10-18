"""Contract conformance: Schemathesis fuzz corpus + registry probes (design §6).

Tier 1 runs Schemathesis over the raw spec with ``not_a_server_error``,
``status_code_conformance`` and ``response_schema_conformance``. Tier 2 sends
the §5.2 probes over plain httpx and asserts the per-route expectations —
the registry *is* the expectation there.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import schemathesis
from hypothesis import settings
from schemathesis import checks

from .conftest import (
    FUZZ_CLASSES,
    MAX_EXAMPLES,
    ApiConfig,
    auth_headers,
    needs_live,
)
from .scenarios import IDEMPOTENCY_KEY_ROUTES, SCENARIOS, Safety, render_probe

pytestmark = needs_live

_lazy_schema = schemathesis.pytest.from_fixture("api_schema")
# Exclusions live on the LazySchema: the loader clones the fixture schema at
# runtime and would drop fixture-level filters (verified against 4.31.0).
_ALWAYS_EXCLUDED = {Safety.WORKFLOW_EXPENSIVE, Safety.STREAMING}
for _scenario in SCENARIOS:
    if _scenario.safety in _ALWAYS_EXCLUDED or _scenario.safety.value not in FUZZ_CLASSES:
        _lazy_schema = _lazy_schema.exclude(method=_scenario.method, path=_scenario.path_template)

schema = _lazy_schema


def _fuzz_headers(case: Any, api_config: ApiConfig) -> dict[str, str]:
    # Schemathesis generates junk identity headers (e.g. X-Idempotency-Key:
    # "null", Authorization: "") for undeclared headers. A truthy junk key is
    # treated as a real idempotency key, so reuse across cases yields 409
    # conflicts unrelated to the contract — strip them; every other generated
    # header (e.g. x-schemathesis-unknown-property) is preserved.
    from collections.abc import MutableMapping

    case_headers = getattr(case, "headers", None)
    if isinstance(case_headers, MutableMapping):
        case_headers.pop("X-Idempotency-Key", None)
        case_headers.pop("Authorization", None)
    headers: dict[str, str] = auth_headers(api_config.auth_token, api_config.tenant_id)
    key = (case.operation.method.upper(), case.operation.path)
    if key in IDEMPOTENCY_KEY_ROUTES:
        # The OpenAPI does not declare X-Idempotency-Key, so Schemathesis
        # never generates a *valid* one; without a fresh key these routes
        # uniform-400.
        headers["X-Idempotency-Key"] = uuid.uuid4().hex
    return headers


@schema.parametrize()
@settings(max_examples=MAX_EXAMPLES, deadline=None)
def test_fuzz(case: Any, api_config: ApiConfig) -> None:
    # No id injection: fuzz uses generated ids against registry-tolerated
    # statuses (404/400/409 declared per route), so the corpus never mutates
    # shared fixture state and has no ordering hazards.
    case.call_and_validate(
        headers=_fuzz_headers(case, api_config),
        checks=[
            checks.not_a_server_error,
            checks.status_code_conformance,
            checks.response_schema_conformance,
        ],
    )


def _scenario_ids() -> list[str]:
    return [f"{s.method} {s.path_template}" for s in SCENARIOS]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=_scenario_ids())
def test_probe(
    scenario: Any, http_client: Any, suite_ids: dict[str, str], api_config: ApiConfig
) -> None:
    method, path, query, headers, body = render_probe(
        scenario,
        application_id=api_config.application_id,
        suite_ids=suite_ids,
    )
    kwargs: dict[str, Any] = {"params": query, "headers": headers}
    if body is not None:
        kwargs["json"] = body
    response = http_client.request(method, path, **kwargs)
    assert response.status_code in scenario.expected_statuses, (
        f"{method} {path} → {response.status_code} "
        f"(expected one of {sorted(scenario.expected_statuses)}): {response.text[:500]}"
    )
    if scenario.probe.expected_body_subset is not None:
        payload = response.json()
        for key, value in scenario.probe.expected_body_subset.items():
            assert payload.get(key) == value, (
                f"{method} {path}: body[{key!r}] == {payload.get(key)!r}"
            )
