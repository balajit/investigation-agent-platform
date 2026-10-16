"""Route-scenario registry for the system/API suite (design §5).

One :class:`RouteScenario` per OpenAPI operation (48 total). The same registry
drives the coverage assertion (``test_route_coverage.py``), the conformance
probes (``test_contract_conformance.py``), and the fuzz-corpus configuration.

Every probe in §5.2 of the design is encoded here and was verified against the
routers in ``src/investigation_agent_platform/api/`` to be side-effect-free:
no probe sends a valid id + valid payload + required key to a
workflow-coupled route in the default mode.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4

APPLICATION_ID_PLACEHOLDER = "{APPLICATION_ID}"
FABRICATED_UUID_PLACEHOLDER = "{FABRICATED_UUID}"


class Safety(str, Enum):
    READ = "READ"
    WRITE_BOUNDED = "WRITE_BOUNDED"
    WORKFLOW_EXPENSIVE = "WORKFLOW_EXPENSIVE"
    STREAMING = "STREAMING"


@dataclass(frozen=True)
class ProbeSpec:
    """The exact safe request sent for this route."""

    query: Mapping[str, Any] | None = None
    body: Any = None  # None = no body is sent
    use_fabricated_id: bool = False  # substitute fresh UUIDs into path params
    use_real_id_from: str | None = None  # path param filled from suite_ids key
    send_idempotency_key: bool = False
    expected_body_subset: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class RouteScenario:
    method: str
    path_template: str
    safety: Safety
    probe: ProbeSpec = field(default_factory=ProbeSpec)
    # True when the route (and its fuzz cases) must carry X-Idempotency-Key,
    # which the OpenAPI does not declare so Schemathesis never generates it.
    needs_idempotency_key: bool = False
    # Union of statuses the route may legitimately return in the default mode.
    expected_statuses: frozenset[int] = frozenset()


def _probe(**kwargs: Any) -> ProbeSpec:
    return ProbeSpec(**kwargs)


def _scenarios() -> list[RouteScenario]:
    read200 = frozenset({200})
    scenarios: list[RouteScenario] = []

    # ---- READ (25): collection/search GETs as-is; item GETs fabricated → 404.
    plain_reads = [
        "/api/v1/health/live",
        "/api/v1/health/ready",
        "/api/v1/capabilities",
        "/api/v1/profiles",
        "/api/v1/investigations",
        "/api/v1/jobs",
        "/api/v1/clusters",
        "/api/v1/aggregate-reports/history",
        "/api/v1/reference-docs/sources",
    ]
    for path in plain_reads:
        scenarios.append(RouteScenario("GET", path, Safety.READ, expected_statuses=read200))
    for path in ("/api/v1/knowledge/search", "/api/v1/reference-docs/search"):
        scenarios.append(
            RouteScenario(
                "GET",
                path,
                Safety.READ,
                probe=_probe(query={"q": "probe"}),
                expected_statuses=read200,
            )
        )
    scenarios.append(
        RouteScenario(
            "GET",
            "/api/v1/aggregate-reports/latest",
            Safety.READ,
            expected_statuses=frozenset({200, 404}),
        )
    )
    fabricated_item_reads = [
        "/api/v1/investigations/{investigation_id}",
        "/api/v1/investigations/{investigation_id}/conclusion",
        "/api/v1/investigations/{investigation_id}/evidence",
        "/api/v1/investigations/{investigation_id}/evidence/{evidence_id}",
        "/api/v1/investigations/{investigation_id}/findings",
        "/api/v1/investigations/{investigation_id}/hypotheses",
        "/api/v1/investigations/{investigation_id}/knowledge",
        "/api/v1/investigations/{investigation_id}/timeline",
        "/api/v1/jobs/{job_id}",
        "/api/v1/clusters/{cluster_id}",
        "/api/v1/batch-intake/{job_id}",
        "/api/v1/chat/sessions/{session_id}/messages",
        "/api/v1/profiles/{app_id}",
        "/api/v1/profiles/{app_id}/revisions",
        "/api/v1/profiles/{app_id}:spring-drift",
    ]
    for path in fabricated_item_reads:
        scenarios.append(
            RouteScenario(
                "GET",
                path,
                Safety.READ,
                probe=_probe(use_fabricated_id=True),
                expected_statuses=frozenset({404}),
            )
        )
    scenarios.append(
        RouteScenario(
            "GET",
            "/api/v1/reference-docs/status",
            Safety.READ,
            probe=_probe(query={"source_id": FABRICATED_UUID_PLACEHOLDER}),
            # Unknown sources yield a default ACTIVE/zero status, not 404.
            expected_statuses=frozenset({200, 404}),
        )
    )

    # ---- WRITE_BOUNDED (12).
    scenarios.append(
        RouteScenario(
            "POST",
            "/api/v1/investigations",
            Safety.WRITE_BOUNDED,
            probe=_probe(
                body={
                    "application_id": APPLICATION_ID_PLACEHOLDER,
                    "problem_description": "system-api probe",
                }
            ),
            expected_statuses=frozenset({201}),
        )
    )
    scenarios.append(
        RouteScenario(
            "POST",
            "/api/v1/chat/sessions",
            Safety.WRITE_BOUNDED,
            probe=_probe(body={}, send_idempotency_key=True),
            needs_idempotency_key=True,
            expected_statuses=frozenset({201}),
        )
    )
    scenarios.append(
        RouteScenario(
            "DELETE",
            "/api/v1/chat/sessions/{session_id}",
            Safety.WRITE_BOUNDED,
            probe=_probe(use_fabricated_id=True),
            expected_statuses=frozenset({404}),
        )
    )
    scenarios.append(
        RouteScenario(
            "POST",
            "/api/v1/investigations/{investigation_id}/cancel",
            Safety.WRITE_BOUNDED,
            probe=_probe(use_fabricated_id=True),
            expected_statuses=frozenset({202, 404}),
        )
    )
    scenarios.append(
        RouteScenario(
            "POST",
            "/api/v1/investigations/{investigation_id}/retry",
            Safety.WRITE_BOUNDED,
            # Gate: only FAILED → 202; a fresh investigation yields 409.
            probe=_probe(use_real_id_from="investigation_id"),
            expected_statuses=frozenset({202, 409}),
        )
    )
    scenarios.append(
        RouteScenario(
            "POST",
            "/api/v1/investigations/{investigation_id}/input-requirements/{requirement_id}/fulfill",
            Safety.WRITE_BOUNDED,
            # Body validation precedes the key check: a valid body without a
            # key → 400; with a key on a non-awaiting investigation → 409.
            probe=_probe(use_fabricated_id=True, body={"requirement_version": 1, "data": {}}),
            needs_idempotency_key=True,
            expected_statuses=frozenset({400, 409}),
        )
    )
    scenarios.append(
        RouteScenario(
            "POST",
            "/api/v1/jobs/{job_id}/cancel",
            Safety.WRITE_BOUNDED,
            probe=_probe(use_fabricated_id=True),
            expected_statuses=frozenset({404}),
        )
    )
    scenarios.append(
        RouteScenario(
            "POST",
            "/api/v1/batch-intake/{job_id}/cancel",
            Safety.WRITE_BOUNDED,
            probe=_probe(use_fabricated_id=True),
            expected_statuses=frozenset({404}),
        )
    )
    scenarios.append(
        RouteScenario(
            "DELETE",
            "/api/v1/batch-intake/{job_id}",
            Safety.WRITE_BOUNDED,
            probe=_probe(use_fabricated_id=True),
            expected_statuses=frozenset({200, 409}),
        )
    )
    scenarios.append(
        RouteScenario(
            "DELETE",
            "/api/v1/aggregate-reports",
            Safety.WRITE_BOUNDED,
            expected_statuses=frozenset({202, 409}),
        )
    )
    scenarios.append(
        RouteScenario(
            "POST",
            "/api/v1/reference-docs/rollback",
            Safety.WRITE_BOUNDED,
            probe=_probe(body={"source_id": FABRICATED_UUID_PLACEHOLDER, "generation": 1}),
            expected_statuses=frozenset({404}),
        )
    )
    # ---- Profiles lifecycle (verified live: fabricated ids 404, empty
    # bodies 400; PUT looks the profile up before validating the body).
    scenarios.append(
        RouteScenario(
            "POST",
            "/api/v1/profiles",
            Safety.WRITE_BOUNDED,
            probe=_probe(body={}),
            expected_statuses=frozenset({400}),
        )
    )
    scenarios.append(
        RouteScenario(
            "PUT",
            "/api/v1/profiles/{app_id}",
            Safety.WRITE_BOUNDED,
            probe=_probe(use_fabricated_id=True, body={}),
            expected_statuses=frozenset({404}),
        )
    )
    scenarios.append(
        RouteScenario(
            "DELETE",
            "/api/v1/profiles/{app_id}",
            Safety.WRITE_BOUNDED,
            probe=_probe(use_fabricated_id=True),
            expected_statuses=frozenset({404}),
        )
    )
    scenarios.append(
        RouteScenario(
            "POST",
            "/api/v1/profiles:derive-from-spring",
            Safety.WRITE_BOUNDED,
            probe=_probe(body={}),
            expected_statuses=frozenset({400}),
        )
    )
    scenarios.append(
        RouteScenario(
            "POST",
            "/api/v1/profiles/{app_id}:refresh-from-spring",
            Safety.WRITE_BOUNDED,
            probe=_probe(use_fabricated_id=True),
            expected_statuses=frozenset({404}),
        )
    )
    scenarios.append(
        RouteScenario(
            "DELETE",
            "/api/v1/reference-docs/sources/{source_id}",
            Safety.WRITE_BOUNDED,
            probe=_probe(
                use_fabricated_id=True,
                expected_body_subset={"status": "DELETED", "rows_removed": 0},
            ),
            expected_statuses=frozenset({200}),
        )
    )

    # ---- WORKFLOW_EXPENSIVE (10): probe stops before any workflow/LLM call.
    scenarios.append(
        RouteScenario(
            "POST",
            "/api/v1/investigations/{investigation_id}/start",
            Safety.WORKFLOW_EXPENSIVE,
            probe=_probe(use_fabricated_id=True),
            expected_statuses=frozenset({404}),
        )
    )
    scenarios.append(
        RouteScenario(
            "POST",
            "/api/v1/clusters/run",
            Safety.WORKFLOW_EXPENSIVE,
            probe=_probe(body={"limit": 2001}),
            needs_idempotency_key=True,
            expected_statuses=frozenset({400, 422}),
        )
    )
    scenarios.append(
        RouteScenario(
            "POST",
            "/api/v1/aggregate-reports",
            Safety.WORKFLOW_EXPENSIVE,
            # Valid body but NO key: the key check precedes job creation.
            probe=_probe(body={"legal_hold": False}),
            needs_idempotency_key=True,
            expected_statuses=frozenset({400}),
        )
    )
    scenarios.append(
        RouteScenario(
            "POST",
            "/api/v1/reference-docs/reindex",
            Safety.WORKFLOW_EXPENSIVE,
            probe=_probe(body={}),
            needs_idempotency_key=True,
            expected_statuses=frozenset({400, 422}),
        )
    )
    scenarios.append(
        RouteScenario(
            "POST",
            "/api/v1/batch-intake",
            Safety.WORKFLOW_EXPENSIVE,
            probe=_probe(body={}),
            needs_idempotency_key=True,
            expected_statuses=frozenset({400}),
        )
    )
    scenarios.append(
        RouteScenario(
            "POST",
            "/api/v1/intake/errors",
            Safety.WORKFLOW_EXPENSIVE,
            # Fail-closed 403 precedes intake (success path: disposable mode only).
            probe=_probe(
                body={
                    "application_id": APPLICATION_ID_PLACEHOLDER,
                    "error_class": "probe.ValueError",
                }
            ),
            expected_statuses=frozenset({403}),
        )
    )
    for path in (
        "/api/v1/investigations/{investigation_id}/pause",
        "/api/v1/investigations/{investigation_id}/resume",
    ):
        scenarios.append(
            RouteScenario(
                "POST",
                path,
                Safety.WORKFLOW_EXPENSIVE,
                # No running workflow: 502 signal failure, or 503 client unwired.
                probe=_probe(use_real_id_from="investigation_id"),
                expected_statuses=frozenset({404, 409, 502, 503}),
            )
        )
    scenarios.append(
        RouteScenario(
            "POST",
            "/api/v1/investigations/{investigation_id}/approve-action",
            Safety.WORKFLOW_EXPENSIVE,
            probe=_probe(
                use_real_id_from="investigation_id",
                query={"action_id": "probe-action", "approved": True},
            ),
            expected_statuses=frozenset({404, 409, 502, 503}),
        )
    )
    scenarios.append(
        RouteScenario(
            "POST",
            "/api/v1/batch-intake/{job_id}/retry",
            Safety.WORKFLOW_EXPENSIVE,
            probe=_probe(use_fabricated_id=True, send_idempotency_key=True),
            needs_idempotency_key=True,
            expected_statuses=frozenset({400, 404, 409}),
        )
    )

    # ---- STREAMING (1).
    scenarios.append(
        RouteScenario(
            "POST",
            "/api/v1/chat/sessions/{session_id}/messages",
            Safety.STREAMING,
            probe=_probe(body={}),
            expected_statuses=frozenset({400, 422}),
        )
    )
    return scenarios


SCENARIOS: list[RouteScenario] = _scenarios()

SCENARIO_KEYS: frozenset[tuple[str, str]] = frozenset(
    (s.method, s.path_template) for s in SCENARIOS
)

FUZZ_EXCLUDED: frozenset[tuple[str, str]] = frozenset(
    (s.method, s.path_template)
    for s in SCENARIOS
    if s.safety in (Safety.WORKFLOW_EXPENSIVE, Safety.STREAMING)
)

IDEMPOTENCY_KEY_ROUTES: frozenset[tuple[str, str]] = frozenset(
    (s.method, s.path_template) for s in SCENARIOS if s.needs_idempotency_key
)


def fuzz_tolerated_statuses(scenario: RouteScenario) -> frozenset[int]:
    """Statuses the fuzz corpus must tolerate for one route.

    The registry's ``expected_statuses`` plus what random fuzz input can
    legitimately produce on a fully-wired stack: malformed/unknown ids
    (400/404) for parameterized routes, and unknown referenced ids (404)
    for POST bodies (e.g. a fuzzed ``application_id``). Anything else —
    notably any 5xx — still fails ``status_code_conformance`` /
    ``not_a_server_error``.
    """
    out = set(scenario.expected_statuses)
    if "{" in scenario.path_template:
        out |= {400, 404}
    if scenario.method == "POST":
        # Unknown referenced ids (404) plus undecodable bodies: non-UTF8
        # bytes never reach JSON parsing — Starlette rejects them with a
        # clean 400 ("There was an error parsing the body"), no traceback.
        out |= {400, 404}
    return frozenset(out)


def inject_tolerated_responses(spec: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an OpenAPI dict with registry-tolerated statuses added.

    Each added status carries no content schema, so
    ``response_schema_conformance`` passes vacuously while
    ``status_code_conformance`` accepts the platform's real, undeclared-but-
    legitimate error statuses — scoped per route, never blanket (C4).
    """
    import copy

    spec = copy.deepcopy(spec)
    by_key = {(s.method, s.path_template): s for s in SCENARIOS}
    for path, item in spec.get("paths", {}).items():
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if method.lower() not in (
                "get",
                "post",
                "put",
                "patch",
                "delete",
                "head",
                "options",
            ):
                continue
            scenario = by_key.get((method.upper(), path))
            if scenario is None or not isinstance(operation, dict):
                continue
            responses = operation.setdefault("responses", {})
            for status in sorted(fuzz_tolerated_statuses(scenario)):
                if str(status) not in responses:
                    responses[str(status)] = {
                        "description": (
                            "Test-tolerated response "
                            f"(system_api registry: {method.upper()} {path})"
                        )
                    }
    return spec


def _resolve_placeholders(value: Any, application_id: str) -> Any:
    if isinstance(value, str):
        if FABRICATED_UUID_PLACEHOLDER in value:
            value = value.replace(FABRICATED_UUID_PLACEHOLDER, str(uuid4()))
        return value.replace(APPLICATION_ID_PLACEHOLDER, application_id)
    if isinstance(value, Mapping):
        return {k: _resolve_placeholders(v, application_id) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_placeholders(v, application_id) for v in value]
    return value


def render_probe(
    scenario: RouteScenario,
    *,
    application_id: str,
    suite_ids: Mapping[str, str] | None = None,
) -> tuple[str, str, dict[str, Any], dict[str, str], Any]:
    """Render a scenario's probe into ``(method, path, query, headers, body)``.

    Path params come from ``suite_ids`` when the probe names a real-id source,
    otherwise from fresh fabricated UUIDs. ``suite_ids`` is required only for
    probes with ``use_real_id_from``.
    """
    suite_ids = suite_ids or {}
    path = scenario.path_template
    start = 0
    rendered: list[str] = []
    while True:
        open_idx = path.find("{", start)
        if open_idx == -1:
            rendered.append(path[start:])
            break
        close_idx = path.find("}", open_idx)
        rendered.append(path[start:open_idx])
        param = path[open_idx + 1 : close_idx]
        if scenario.probe.use_real_id_from == param:
            if param not in suite_ids:
                raise KeyError(f"probe for {scenario.method} {path} needs suite id {param!r}")
            rendered.append(suite_ids[param])
        else:
            rendered.append(str(uuid4()))
        start = close_idx + 1
    query = {
        k: _resolve_placeholders(v, application_id) for k, v in (scenario.probe.query or {}).items()
    }
    headers: dict[str, str] = {}
    if scenario.probe.send_idempotency_key:
        headers["X-Idempotency-Key"] = uuid4().hex
    body = _resolve_placeholders(scenario.probe.body, application_id)
    return scenario.method, "".join(rendered), query, headers, body
