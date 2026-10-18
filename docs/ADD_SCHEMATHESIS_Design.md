# System-Level API Contract & Stateful Flow Testing — Design

**Status:** Design only. No code changes are included here.

**Goal:** Add a black-box, system/API-level test suite using **Schemathesis 4.x** +
**pytest** that runs against the live FastAPI server and validates (a) OpenAPI
contract conformance across **every** route, and (b) stateful use-case flows for
the platform's primary domain resources.

The originating task template (`prompts/ADD_SCHEMATHESIS.md`) assumes a generic
CRUD API (`POST` → `GET` → `PUT`/`PATCH` → `GET` → `DELETE` → `GET 404`) over
`http://127.0.0.1:8000`, using Schemathesis 3.x APIs. The real platform differs
enough that the design below adapts the template rather than reproducing it
verbatim. Section 2 records the mismatches that drive the adaptations.

---

## 1. Confirmed decisions

| # | Decision | Choice |
|---|----------|--------|
| 1 | Schemathesis version | **4.x** (`schemathesis.openapi.from_url`, `@schema.parametrize()`, `case.call_and_validate()`) |
| 2 | Fuzzing aggressiveness | **Cover every route**, but control intensity through a **modular per-route registry + configuration** |
| 3 | Env-var contract | The task's **`TEST_*` names** as primary, `IAP_LIVE_*` as fallback |
| 4 | Stateful flows | **Investigation + chat** by default; **batch-intake** behind an opt-in flag |
| 5 | Network gating | Whole package skips unless `IAP_LIVE_TESTS=1` or `TEST_TARGET_URL` is set |

---

## 2. Platform reality vs the task template

Verified against the generated OpenAPI document (48 operations under `/api/v1`)
and the routers in `src/investigation_agent_platform/api/`:

1. **No `PUT`/`PATCH` exists.** State changes are `POST` sub-resource transitions
   (`/investigations/{id}/cancel|start|pause|resume|retry`,
   `/jobs/{id}/cancel`, `/batch-intake/{id}/cancel`). The template's "Modify
   (`PUT`/`PATCH`)" step must be re-mapped to these.
2. **Only four `DELETE` routes exist**: `/aggregate-reports`,
   `/batch-intake/{job_id}`, `/chat/sessions/{session_id}`,
   `/reference-docs/sources/{source_id}`. Most resources have no delete/404 path.
3. **Auth is not plain Bearer.** In development, identity comes from
   `X-Tenant-ID`; in production, from a *verified JWT carrying a tenant claim*
   (`api/tenant.py`). The OpenAPI declares **no `securitySchemes`**, so
   Schemathesis attaches nothing. With no tenant header, every `/api/v1` route
   returns `401` — which the spec does not declare — causing mass false failures.
   A `TEST_TENANT_ID` is therefore **required**, not just `TEST_AUTH_TOKEN`.
4. **The spec declares only success + `422` responses** — no `401/404/409/503`
   models. Schemathesis's default `status_code_conformance` and schema checks
   flag every undeclared status. Checks must be tailored.
5. **`/openapi.json` is served only in non-production or with
   `IAP_ENABLE_DOCS=true`** (see the application factory / settings).
6. **Some routes are side-effectful or non-JSON.** `investigations/{id}/start`
   dispatches Temporal workflows; `POST /batch-intake` spawns child
   investigations; `POST /aggregate-reports` runs report generation;
   `POST /clusters/run`; `POST /reference-docs/reindex` makes embedding calls;
   `POST /chat/sessions/{id}/messages` streams SSE and calls the LLM.
   Indiscriminate fuzzing of these causes real work, cost, and subsystem `503`s.
7. **Dependencies:** `httpx` (`0.28.1`) is already present transitively;
   `schemathesis` is **not installed**. Latest is `4.31.x`, whose pytest idioms
   are used here (the template's `schemathesis.from_uri` is 3.x-only).

---

## 3. Directory layout

```
tests/system_api/
├── __init__.py
├── conftest.py                  # env parsing, readiness gate, httpx clients, schema fixture, auth
├── scenarios.py                 # RouteScenario registry: one entry per OpenAPI operation
├── test_route_coverage.py       # registry keys == every spec operation (fails on new routes)
├── test_contract_conformance.py # Schemathesis fuzz; "no 500" + 2xx schema conformance
└── test_usecase_flows.py        # investigation + chat lifecycles (batch-intake opt-in)
```

The suite is isolated from `tests/unit` and runs only against a live stack, the
same operating model as `tests/integration/test_live_platform.py`.

---

## 4. Configuration contract

### 4.1 Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `TEST_TARGET_URL` | `http://127.0.0.1:8000` | API base URL |
| `OPENAPI_URL` | `${TEST_TARGET_URL}/openapi.json` | OpenAPI document location |
| `TEST_TENANT_ID` | `tenant-a` | `X-Tenant-ID` for dev auth (**required**) |
| `TEST_AUTH_TOKEN` | unset | Optional `Authorization: Bearer <token>` |
| `TEST_API_FUZZ_CLASSES` | `READ,WRITE_BOUNDED` | Safety classes fuzzed beyond a single presence probe |
| `TEST_API_MAX_EXAMPLES` | `10` | Hypothesis `max_examples` (kept low; live server) |
| `TEST_API_FLOWS` | `investigation,chat` | Enabled stateful flow modules |

**Fallbacks:** `TEST_TARGET_URL` falls back to `IAP_LIVE_BASE_URL`;
`TEST_TENANT_ID` falls back to `IAP_LIVE_TENANT` (default `live-suite-tenant`).

### 4.2 Network gating

The whole package is skipped unless `IAP_LIVE_TESTS=1` **or** `TEST_TARGET_URL`
is explicitly set, so a plain `pytest` run (CI/unit) never touches the network.
Before the suite runs, a readiness probe hits `GET /api/v1/health/live`; on
failure it fails fast with a message pointing at `scripts/start-platform.sh`.

### 4.3 Authentication

Auth headers are applied via `case.call_and_validate(headers=auth_headers)` in
the test body, **not** a `map_*` hook. Schemathesis's `map_*` hooks do not run
for every phase (examples/coverage), and the tenant header must apply to every
request. `auth_headers` always includes `X-Tenant-ID`; `Authorization: Bearer`
is added only when `TEST_AUTH_TOKEN` is set.

---

## 5. The "one scenario" abstraction (`scenarios.py`)

Every operation gets exactly one declarative entry. The **same** registry drives
both the coverage assertion and the per-route conformance probe.

```python
from dataclasses import dataclass
from enum import Enum

class Safety(str, Enum):
    READ = "READ"                            # no side effects; fuzz freely
    WRITE_BOUNDED = "WRITE_BOUNDED"          # bounded state change; examples / light fuzz
    WORKFLOW_EXPENSIVE = "WORKFLOW_EXPENSIVE"# Temporal / LLM / embeddings; probe only
    STREAMING = "STREAMING"                  # SSE / non-JSON; probe only

@dataclass(frozen=True)
class RouteScenario:
    method: str
    path_template: str
    safety: Safety
    resolve_params: Callable[[Fixtures], dict] | None   # real ids from fixtures
    query: dict | None
    body: dict | None
    expected_statuses: frozenset[int]                   # per-route contract expectation
```

`resolve_params` maps path parameters (`{investigation_id}`, `{job_id}`,
`{session_id}`, …) to real identifiers provisioned by fixtures, so parameterized
reads hit `200`, not a `404` from a random UUID.

### 5.1 Route inventory (all 48 operations, by safety class)

**READ (25)** — fuzzed by default:

```
GET  /api/v1/health/live
GET  /api/v1/health/ready
GET  /api/v1/capabilities
GET  /api/v1/profiles
GET  /api/v1/investigations
GET  /api/v1/investigations/{investigation_id}
GET  /api/v1/investigations/{investigation_id}/conclusion
GET  /api/v1/investigations/{investigation_id}/evidence
GET  /api/v1/investigations/{investigation_id}/evidence/{evidence_id}
GET  /api/v1/investigations/{investigation_id}/findings
GET  /api/v1/investigations/{investigation_id}/hypotheses
GET  /api/v1/investigations/{investigation_id}/knowledge
GET  /api/v1/investigations/{investigation_id}/timeline
GET  /api/v1/jobs
GET  /api/v1/jobs/{job_id}
GET  /api/v1/clusters
GET  /api/v1/clusters/{cluster_id}
GET  /api/v1/aggregate-reports/latest
GET  /api/v1/aggregate-reports/history
GET  /api/v1/batch-intake/{job_id}
GET  /api/v1/chat/sessions/{session_id}/messages
GET  /api/v1/knowledge/search
GET  /api/v1/reference-docs/sources
GET  /api/v1/reference-docs/search
GET  /api/v1/reference-docs/status
```

**WRITE_BOUNDED (17)** — examples + light fuzz by default:

```
POST   /api/v1/investigations                                              (create)
POST   /api/v1/chat/sessions                                               (create)
DELETE /api/v1/chat/sessions/{session_id}
POST   /api/v1/intake/errors
POST   /api/v1/investigations/{investigation_id}/cancel
POST   /api/v1/investigations/{investigation_id}/pause
POST   /api/v1/investigations/{investigation_id}/resume
POST   /api/v1/investigations/{investigation_id}/retry
POST   /api/v1/investigations/{investigation_id}/approve-action
POST   /api/v1/investigations/{investigation_id}/input-requirements/{requirement_id}/fulfill
POST   /api/v1/jobs/{job_id}/cancel
POST   /api/v1/batch-intake/{job_id}/cancel
POST   /api/v1/batch-intake/{job_id}/retry
DELETE /api/v1/batch-intake/{job_id}
DELETE /api/v1/aggregate-reports
POST   /api/v1/reference-docs/rollback
DELETE /api/v1/reference-docs/sources/{source_id}
```

**WORKFLOW_EXPENSIVE (5)** — presence probe only by default (no fuzz):

```
POST /api/v1/investigations/{investigation_id}/start   (Temporal workflow)
POST /api/v1/clusters/run
POST /api/v1/aggregate-reports                         (report generation)
POST /api/v1/reference-docs/reindex                    (embeddings / LLM)
POST /api/v1/batch-intake                              (spawns child investigations)
```

**STREAMING (1)** — presence probe only by default:

```
POST /api/v1/chat/sessions/{session_id}/messages        (SSE + LLM)
```

### 5.2 Intensity control

All four classes are **covered** — every route is present in the registry and hit
by at least one safe probe. Only fuzz depth differs:

| Class | Default behavior |
|-------|------------------|
| `READ` | Examples + bounded random fuzz (`TEST_API_MAX_EXAMPLES`) |
| `WRITE_BOUNDED` | Examples + light fuzz |
| `WORKFLOW_EXPENSIVE` | One probe with a payload guaranteed to fail validation → expect `422`; no real work |
| `STREAMING` | One probe with a validation-failing payload → expect `422`; no LLM/SSE |

`WORKFLOW_EXPENSIVE` and `STREAMING` can be promoted into the fuzz set via
`TEST_API_FUZZ_CLASSES` **only** against a disposable stack.

---

## 6. Contract conformance (`test_contract_conformance.py`)

1. **Test-local spec view.** Fetch `OPENAPI_URL`, then inject a shared tolerated
   error-response set (`400/401/403/404/409/429/503`) into each operation, and
   load it with `schemathesis.openapi.from_dict(...)`. This keeps
   `status_code_conformance` meaningful for *genuinely* unexpected codes while
   not flagging the platform's real, undeclared-but-legitimate errors.
2. **Parametrization.** `@schema.parametrize()` over the test-local schema; the
   test body calls `case.call_and_validate(headers=auth_headers)`.
3. **Checks kept:** `not_a_server_error` (headline: zero 500s),
   `response_schema_conformance`, `negative_data_rejection`,
   `positive_data_acceptance`. Disabled: `ignored_auth` (no security schemes)
   and the stateful-only checks (the spec defines no `links`).
4. **Exclusions.** `WORKFLOW_EXPENSIVE` and `STREAMING` operations are removed
   from `@schema.parametrize()` via `schema.exclude(...)` and covered instead by
   the registry probe (Section 5.2) — so they are exercised without triggering
   real workflows or LLM calls.
5. **Bounds.** `@settings(max_examples=TEST_API_MAX_EXAMPLES, deadline=None)`.

The template's `scenario` for response headers (`response_headers_conformance`)
is retained through the default check set; SSE `itemSchema` validation is
unavailable because the platform's SSE response does not declare one, which is
another reason the streaming route is probe-only.

---

## 7. Route coverage guarantee (`test_route_coverage.py`)

Assert `set(registry) == set(spec operations)`. A newly added route fails the
suite until its scenario is added. This is the "same scenario covers every
route" contract: the registry is the single source of truth, and the test fails
loudly on drift.

---

## 8. Stateful use-case flows (`test_usecase_flows.py`)

Because the OpenAPI document declares **no `links`**, `schema.as_state_machine()`
cannot infer operation sequences, and the `use_after_free` /
`ensure_resource_availability` checks never fire. Flows are therefore
hand-written deterministic sequences using `httpx` (sync client; no async
lifecycle needed for these requests).

### 8.1 Investigation lifecycle (default)

```
POST /api/v1/investigations            → 201  (extract id)
GET  /api/v1/investigations/{id}       → 200  (verify read)
POST /api/v1/investigations/{id}/cancel→ 202  ("modify" — state transition)
GET  /api/v1/investigations/{id}       → 200  (verify state == CANCELLED)
```

There is no investigation `DELETE`; cleanup is not possible over the API
(documented as a known gap). Tests run against a dedicated tenant id.

### 8.2 Chat session lifecycle (default) — closest match to the template

```
POST   /api/v1/chat/sessions                        → 201  (X-Idempotency-Key; extract session_id)
GET    /api/v1/chat/sessions/{id}/messages          → 200  (verify read)
DELETE /api/v1/chat/sessions/{id}                   → 200  (cleanup)
GET    /api/v1/chat/sessions/{id}/messages          → 404  (verify deleted)
```

The template's update step maps to `POST …/messages` (append), but that route is
SSE + LLM-backed and is **skipped by default**; it is only exercised by the
streaming presence probe.

### 8.3 Batch-intake job lifecycle (opt-in)

Enabled via `TEST_API_FLOWS` including `batch-intake`:

```
POST   /api/v1/batch-intake             → 202  (extract job_id; spawns children)
GET    /api/v1/batch-intake/{job_id}    → 200
POST   /api/v1/batch-intake/{job_id}/cancel → 202
DELETE /api/v1/batch-intake/{job_id}    → 200
GET    /api/v1/batch-intake/{job_id}    → 404
```

---

## 9. Dependencies

`pyproject.toml` (dev group):

- `schemathesis>=4.31,<5`
- `httpx` pinned explicitly (already transitive at `0.28.1`)

---

## 10. Running

Prerequisites: a live stack (`scripts/start-platform.sh` brings up compose infra
+ Postgres + API on `:8000`). `/openapi.json` is served in development or when
`IAP_ENABLE_DOCS=true`.

```bash
# System/API suite (contract + coverage + flows)
IAP_LIVE_TESTS=1 TEST_TARGET_URL=http://127.0.0.1:8000 \
  uv run pytest tests/system_api -v

# Direct Schemathesis CLI spot-check
uv run schemathesis run http://127.0.0.1:8000/openapi.json \
  -H "X-Tenant-ID: tenant-a"
```

---

## 11. Risks, non-goals, and open questions

- **Live stack is mandatory.** No in-process shortcut is viable because of the
  app lifespan (DB/Temporal wiring). This mirrors the existing live suite.
- **Side-effect containment.** `WORKFLOW_EXPENSIVE`/`STREAMING` probes must use
  payloads guaranteed to fail validation (`422`) so they never trigger real
  work. Promoting them into the fuzz set requires a disposable stack.
- **Undocumented error statuses.** `status_code_conformance` depends on the
  injected tolerated error set. If testing the *raw* spec is preferred, disable
  that check and assert `expected_statuses` per route from the registry instead.
- **Non-goals:** OpenAPI `links`-driven stateful testing (spec has no links);
  object-level authorization checks (no security schemes declared); load/recency
  testing.
- **Open question:** whether to add a short `tests/system_api/README.md` or keep
  usage documented in `conftest.py` docstrings only.
