# System-Level API Contract & Stateful Flow Testing — Design v1.0

**Status:** Final design. Supersedes `docs/ADD_SCHEMATHESIS_Design.md` (draft).
Open review comments in `docs/ADD_SCHEMATHESIS_Design_feedback.md` (C1–C6,
M1–M10) are dispositioned in §12; every BLOCKER/MAJOR is resolved in the
design below. Still design-only — no code changes are included here.

**Goal:** Add a black-box, system/API-level test suite using **Schemathesis 4.x**
+ **pytest** that runs against the live FastAPI server and validates (a) OpenAPI
contract conformance across **every** route, and (b) stateful use-case flows for
the platform's primary domain resources.

The originating task template (`prompts/ADD_SCHEMATHESIS.md`) assumes a generic
CRUD API (`POST` → `GET` → `PUT`/`PATCH` → `GET` → `DELETE` → `GET 404`) over
`http://127.0.0.1:8000`, using Schemathesis 3.x APIs. The real platform differs
enough that this design adapts the template rather than reproducing it verbatim.
Section 2 records the mismatches that drive the adaptations. All route-level
claims below were verified against the routers in
`src/investigation_agent_platform/api/` and the generated OpenAPI document
(48 operations under `/api/v1`).

---

## 1. Confirmed decisions

| # | Decision | Choice |
|---|----------|--------|
| 1 | Schemathesis version | **4.x** (`schemathesis.openapi.from_url` / `from_dict`, `@schema.parametrize()`, `case.call_and_validate()`, `schema.exclude(...)`) |
| 2 | Fuzzing aggressiveness | **Cover every route**, but control intensity through a **modular per-route registry + configuration** |
| 3 | Env-var contract | The task's **`TEST_*` names** as primary, `IAP_LIVE_*` as fallback |
| 4 | Stateful flows | **Investigation + chat** by default; **batch-intake** behind an opt-in flag |
| 5 | Network gating | Whole package skips unless `IAP_LIVE_TESTS=1` or `TEST_TARGET_URL` is **present in `os.environ`** |
| 6 | Scratch tenant | All mutation runs against a dedicated suite tenant, never a shared one (C6) |
| 7 | Wiring assumption | Fuzz corpus assumes a **fully-wired** stack; readiness gates on `/health/ready` (C5) |

---

## 2. Platform reality vs the task template

1. **No `PUT`/`PATCH` exists.** State changes are `POST` sub-resource transitions
   (`/investigations/{id}/cancel|start|pause|resume|retry`,
   `/jobs/{id}/cancel`, `/batch-intake/{id}/cancel`). The template's "Modify
   (`PUT`/`PATCH`)" step is re-mapped to these.
2. **Only four `DELETE` routes exist**: `/aggregate-reports`,
   `/batch-intake/{job_id}`, `/chat/sessions/{session_id}`,
   `/reference-docs/sources/{source_id}`. Most resources have no delete/404 path.
3. **Auth is not plain Bearer.** In development, identity comes from
   `X-Tenant-ID`; in production, from a *verified JWT carrying a tenant claim*
   (`api/tenant.py`, including 403 on tenant mismatch). The OpenAPI declares
   **no `securitySchemes`**, so Schemathesis attaches nothing. With no tenant
   header, every `/api/v1` route returns `401` — which the spec does not declare.
   A `TEST_TENANT_ID` is therefore **required**, not just `TEST_AUTH_TOKEN`.
4. **The spec declares only success + `422` responses** — no `400/401/403/404/
   409/429/502/503` models — yet the routers genuinely return all of these
   (400 malformed ids, 403 tenant mismatch / intake service-principal gate,
   404 unknown ids, 409 idempotency/state conflicts and legal hold, 429 quota,
   502 workflow start/signal failure, 503 unwired subsystem). Checks must be
   two-tiered (§6): raw-spec conformance for the fuzz corpus, per-route
   expectations for probes/flows.
5. **`/openapi.json` is served only in non-production or with
   `IAP_ENABLE_DOCS=true`.**
6. **Several routes are workflow-coupled or non-JSON** (§5.1): `start`,
   `clusters/run`, `aggregate-reports` POST, `batch-intake` POST,
   `batch-intake/{id}/retry`, `intake/errors`, `pause`/`resume`/`approve-action`,
   `reference-docs/reindex` (Temporal/LLM/embeddings), and `chat/.../messages`
   POST (SSE + LLM).
7. **Dependencies:** `httpx` is **not declared** in `pyproject.toml` (present
   only transitively at 0.28.1 — it must be added); `schemathesis` is **not
   installed** (target `>=4.31,<5`); `pytest-asyncio>=1.4` is already declared
   but unused — flows stay synchronous.
8. **A websocket route exists** (`/jobs/{job_id}/stream`, `jobs.py`) but is not
   part of the OpenAPI document and is **out of scope** for this HTTP suite.
9. **Investigation-scoped reads follow a uniform 400/404 pattern** (malformed id
   → 400; unknown id → 404; cross-tenant → 403), verified in `investigations.py`,
   `evidence.py`, `hypotheses.py`, `timeline.py`, `events.py`.

---

## 3. Directory layout

```
tests/system_api/
├── __init__.py
├── conftest.py                  # env parsing, gates, httpx clients, schema fixture, auth, id injection
├── scenarios.py                 # RouteScenario registry: one entry per OpenAPI operation
├── test_route_coverage.py       # registry keys == every spec operation (fails on new routes)
├── test_contract_conformance.py # Schemathesis fuzz (raw spec) + registry probes (per-route)
└── test_usecase_flows.py        # investigation + chat lifecycles (batch-intake opt-in)
```

Isolated from `tests/unit`; same live-stack operating model as
`tests/integration/test_live_platform.py`.

---

## 4. Configuration contract

### 4.1 Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `TEST_TARGET_URL` | `http://127.0.0.1:8000` | API base URL |
| `OPENAPI_URL` | `${TEST_TARGET_URL}/openapi.json` | OpenAPI document location |
| `TEST_TENANT_ID` | `system-api-suite` | Dedicated scratch tenant; **must not be a shared tenant** (C6) |
| `TEST_API_APPLICATION_ID` | `example` | `application_id` for `POST /investigations`; must exist in the profile store **for `TEST_TENANT_ID`** (seed e.g. via `scripts/seed_profile.py`) |
| `TEST_AUTH_TOKEN` | unset | Optional `Authorization: Bearer <token>` |
| `TEST_API_FUZZ_CLASSES` | `READ,WRITE_BOUNDED` | Safety classes in the fuzz corpus beyond probes |
| `TEST_API_MAX_EXAMPLES` | `10` | Hypothesis `max_examples` (kept low; live server) |
| `TEST_API_REQUEST_TIMEOUT` | `10` | Per-request seconds (matches Schemathesis `--request-timeout` default) |
| `TEST_API_FLOWS` | `investigation,chat` | Enabled stateful flow modules (`batch-intake` opt-in) |

**Fallbacks:** `TEST_TARGET_URL` ← `IAP_LIVE_BASE_URL`;
`TEST_TENANT_ID` ← `IAP_LIVE_TENANT`.

### 4.2 Network gating

Skip the whole package unless `IAP_LIVE_TESTS=1` **or** `TEST_TARGET_URL` is
present in `os.environ` (presence, not value — the variable has a default, so
value-based gating would never skip).

### 4.3 Readiness gate (C5)

Gate on **`GET /api/v1/health/ready`** (public, no auth), requiring HTTP 200
with `{"status": "READY"}`. `/health/live` only proves the process is up; the
routes under test return 503 whenever a subsystem (Temporal, chat store,
cluster/reference/artifact stores, input store) is absent from the app context.
A non-READY stack fails fast with a message pointing at
`scripts/start-platform.sh`. Note `IAP_REQUIRE_TEMPORAL`/`IAP_REQUIRE_BROKER`
(env-dependent defaults): the gate trusts the server's own verdict rather than
re-implementing it. The fuzz corpus assumes the gated fully-wired state.

### 4.4 Authentication

Auth headers are applied via `case.call_and_validate(headers=auth_headers)` and
in every hand-written probe/flow request — **not** a `map_*` hook, which does
not run for every Schemathesis phase. `auth_headers` always includes
`X-Tenant-ID`; `Authorization: Bearer` is added only when `TEST_AUTH_TOKEN` is
set.

---

## 5. The scenario registry (`scenarios.py`)

One entry per operation. The **same** registry drives the coverage assertion,
the conformance probes, and the fuzz-corpus configuration.

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

class Safety(str, Enum):
    READ = "READ"                              # no side effects; fuzz freely
    WRITE_BOUNDED = "WRITE_BOUNDED"            # bounded state change; examples only
    WORKFLOW_EXPENSIVE = "WORKFLOW_EXPENSIVE"  # Temporal / LLM / embeddings; probe only
    STREAMING = "STREAMING"                    # SSE / non-JSON; probe only

@dataclass(frozen=True)
class ProbeSpec:
    """The exact safe request sent for this route and the statuses it may
    legitimately return. Verified per route in §5.2 — never a generic
    'invalid payload → 422' (C1)."""
    query: dict[str, Any] | None = None
    headers: dict[str, str] | None = None      # e.g. {"X-Idempotency-Key": ...} or {}
    body: Any = None                           # None = no body
    use_fabricated_id: bool = False            # substitute random UUIDs into path params
    expected_statuses: frozenset[int] = frozenset()

@dataclass(frozen=True)
class RouteScenario:
    method: str
    path_template: str
    safety: Safety
    probe: ProbeSpec
    headers: dict[str, str] | None = None      # required headers factory input,
                                               # notably X-Idempotency-Key (C2)
    resolve_params: Callable[[Any], dict] | None = None  # fixture ids, ONLY where
                                               # attainable without a workflow run (C3)
    query: dict | None = None
    body: dict | None = None
    expected_statuses: frozenset[int] = frozenset()  # asserted by probes/flows
```

`resolve_params` is used **only** for ids obtainable in the constrained mode
(investigation id and chat session id created by the suite's own fixtures).
Ids that require a workflow run (`evidence_id`, `requirement_id`, `cluster_id`,
`job_id`) are **never fabricated as 200-promises**: those routes are
probe-covered with fabricated UUIDs expecting 404 (C3).

If a future route admits *no* safe probe at all, add `Safety.COVERAGE_ONLY`
(registered, coverage-asserted, never called). No current route needs it: §5.2
gives every one of the 48 operations a verified side-effect-free probe.

### 5.1 Route inventory (48 operations)

**READ (25)** — fuzz corpus (examples + bounded fuzz):

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

**WRITE_BOUNDED (12)** — fuzz corpus, examples phase only, with required headers
supplied (`X-Idempotency-Key` where mandatory); `POST /investigations` capped
(C6 — rows are evergreen, no DELETE exists):

```
POST   /api/v1/investigations
POST   /api/v1/chat/sessions
DELETE /api/v1/chat/sessions/{session_id}
POST   /api/v1/investigations/{investigation_id}/cancel
POST   /api/v1/investigations/{investigation_id}/retry
POST   /api/v1/investigations/{investigation_id}/input-requirements/{requirement_id}/fulfill
POST   /api/v1/jobs/{job_id}/cancel
POST   /api/v1/batch-intake/{job_id}/cancel
DELETE /api/v1/batch-intake/{job_id}
DELETE /api/v1/aggregate-reports
POST   /api/v1/reference-docs/rollback
DELETE /api/v1/reference-docs/sources/{source_id}
```

**WORKFLOW_EXPENSIVE (10)** — excluded from `@schema.parametrize()` via
`schema.exclude(operation_id=...)`; probe-covered only in the default mode:

```
POST /api/v1/investigations/{investigation_id}/start
POST /api/v1/clusters/run
POST /api/v1/aggregate-reports
POST /api/v1/reference-docs/reindex
POST /api/v1/batch-intake
POST /api/v1/intake/errors
POST /api/v1/investigations/{investigation_id}/pause
POST /api/v1/investigations/{investigation_id}/resume
POST /api/v1/investigations/{investigation_id}/approve-action
POST /api/v1/batch-intake/{job_id}/retry
```

**STREAMING (1)** — excluded from parametrize; probe-covered only:

```
POST /api/v1/chat/sessions/{session_id}/messages
```

Reclassifications vs the draft (C2): `intake/errors` (fail-closed 403 +
dispatches workflow), `pause`/`resume`/`approve-action` (Temporal-signal
coupled; 502/503 without a running workflow), and `batch-intake/{id}/retry`
(starts a workflow on success) moved from WRITE_BOUNDED to WORKFLOW_EXPENSIVE.
`batch-intake/{id}/cancel` stays WRITE_BOUNDED (Temporal cancel is best-effort,
row state governs — no 502 path).

### 5.2 Per-route probe specifications (C1, C2, C3)

Each probe is side-effect-free by construction. `F` = fabricated UUID in path
params; `K` = fresh `X-Idempotency-Key` header sent.

**READ probes** — collection/search GETs as-is; item GETs with fabricated ids:

| Route | Probe | Expect |
|---|---|---|
| `GET health/live`, `health/ready`, `capabilities`, `profiles`, `investigations`, `jobs`, `clusters`, `aggregate-reports/history`, `reference-docs/sources` | as-is | 200 |
| `GET knowledge/search`, `reference-docs/search` | `?q=probe` | 200 |
| `GET aggregate-reports/latest` | as-is | {200, 404} ("No report yet" on a fresh stack) |
| `GET` investigation-scoped item routes (`/{id}`, `/conclusion`, `/evidence`, `/findings`, `/hypotheses`, `/knowledge`, `/timeline`), `GET jobs/{id}`, `GET clusters/{id}`, `GET batch-intake/{id}`, `GET chat/.../messages` | F | 404 |
| `GET .../evidence/{evidence_id}` | F (fabricated investigation id) | 404 (lookup fails before the evidence id is parsed) |
| `GET reference-docs/status` | `?source_id=<fabricated>` | 404 |

**WRITE_BOUNDED probes:**

| Route | Probe | Expect |
|---|---|---|
| `POST /investigations` | valid body (`application_id=TEST_API_APPLICATION_ID`) | 201 (creates a row on the scratch tenant — capped, C6) |
| `POST /chat/sessions` | `{}` + K | 201 |
| `DELETE /chat/sessions/{id}` | F | 404 |
| `POST inv/{id}/cancel` | F → 404; on flow-created id → 202, body `status == "CANCELLING"` | {202, 404} |
| `POST inv/{id}/retry` | fresh investigation | 409 (gate: only `FAILED` → 202; 202 path is disposable-mode only) |
| `POST .../fulfill` | no key → 400; K on fresh investigation → 409 (not awaiting input) | {400, 409} |
| `POST jobs/{id}/cancel` | F | 404 |
| `POST batch/{id}/cancel` | F | 404 |
| `DELETE batch/{id}` | F | 409 ("missing, active, or within retention") |
| `DELETE aggregate-reports` | as-is | {202, 409} (409 when legal hold) |
| `POST reference-docs/rollback` | fabricated `source_id` | 404 |
| `DELETE reference-docs/sources/{id}` | fabricated | 200 with `rows_removed == 0` (no-op) |

**WORKFLOW_EXPENSIVE / STREAMING probes** — each verified to stop before any
workflow, LLM, or embedding call:

| Route | Probe | Expect | Why safe |
|---|---|---|---|
| `POST inv/{id}/start` | F | 404 | lookup fails before dispatch (`investigations.py`) |
| `POST clusters/run` | `{"limit": 2001}` (constraint violation), no key | 422 | body validation precedes everything |
| `POST aggregate-reports` | valid body, **no** key | 400 | key check precedes job creation |
| `POST reference-docs/reindex` | `{}` | 422 | `source_id` required; validation first |
| `POST batch-intake` | `{}` | 400 | "records or csv" check precedes intake |
| `POST intake/errors` | valid body | 403 | service-principal gate precedes intake (success path is disposable-mode only) |
| `POST inv/{id}/pause`, `/resume` | real id from flow fixture | {502, 503} | no running workflow: 502 signal failure, or 503 client unwired |
| `POST inv/{id}/approve-action` | real id + `?action_id=x&approved=true` | {502, 503} | same signal path |
| `POST batch/{id}/retry` | K + F | 409 ("no failed records"); no key → 400 | fails before any workflow start |
| `POST chat/.../messages` | `{}` | 422 | `content` required; validation precedes LLM/SSE |

The draft's blanket "invalid payload → 422" is replaced by this table. No probe
sends a valid id + valid payload + required key to a workflow-coupled route in
the default mode; promoting those classes via `TEST_API_FUZZ_CLASSES` requires
a disposable stack.

### 5.3 Intensity control

| Class | Default behavior |
|-------|------------------|
| `READ` | Examples + bounded random fuzz (`TEST_API_MAX_EXAMPLES`) |
| `WRITE_BOUNDED` | Examples only, required headers injected; `POST /investigations` additionally capped |
| `WORKFLOW_EXPENSIVE` | §5.2 probe only |
| `STREAMING` | §5.2 probe only |

Fixture-id injection for the fuzz corpus uses a `filter_case`/`map_case`-style
override of `case.path_parameters` (investigation id, chat session id only).
Routes whose ids are unattainable without a workflow run are excluded from fuzz
and covered by their §5.2 probe (C3).

---

## 6. Contract conformance (`test_contract_conformance.py`)

Two tiers — no shared injected error set (C4):

1. **Fuzz corpus over the raw spec.** Load `OPENAPI_URL` via
   `schemathesis.openapi.from_dict(...)` (fetch-then-load, so the suite can pin
   timeouts/retries), subtract WORKFLOW_EXPENSIVE/STREAMING via
   `schema.exclude(operation_id=...)`, and run `@schema.parametrize()` with
   checks **`not_a_server_error`, `status_code_conformance`,
   `response_schema_conformance`**. On the fully-wired stack (§4.3), anything
   outside the declared responses is genuinely suspicious — which is the point.
   `ignored_auth` is disabled (no security schemes declared);
   `response_headers_conformance` is vacuous (the platform declares no response
   headers) and is not relied upon; `negative_data_rejection` /
   `positive_data_acceptance` are not promised in v1.0 (phase/mode-dependent;
   revisit after observing a run). Stateful checks never fire (no `links`).
2. **Registry probes over plain `httpx`.** One test per scenario asserting the
   §5.2 probe lands in `expected_statuses`. `status_code_conformance` is not
   applied here — the registry *is* the expectation.
3. **Bounds.** `@settings(max_examples=TEST_API_MAX_EXAMPLES, deadline=None)`,
   per-request timeout `TEST_API_REQUEST_TIMEOUT`.

Illustrative value (not a code change): `get_evidence` parses `evidence_id`
with an uncaught `UUID(...)` (`evidence.py`), so a malformed `evidence_id`
against a *real* investigation id is a 500 candidate the fuzzer should surface
as a `not_a_server_error` failure for the service team to triage.

---

## 7. Route coverage guarantee (`test_route_coverage.py`)

Assert `set(registry) == set(raw spec operations)`. Conformance exclusions are a
runtime concern only — coverage is always computed against the **raw** spec.
Anchor both sides: compare the live `OPENAPI_URL` document against the registry
*and* cross-check the live document's operation set against
`create_app().openapi()` generated import-time (no network), so a route removed
from the server cannot silently vanish from both sides of the equality.

---

## 8. Stateful use-case flows (`test_usecase_flows.py`)

Hand-written deterministic sequences with the sync `httpx` client (no `links`
in the spec, so `as_state_machine()` cannot infer sequences). All mutation uses
`TEST_TENANT_ID` (scratch tenant, C6). Fresh `X-Idempotency-Key` per creation
call.

### 8.1 Investigation lifecycle (default)

```
POST /api/v1/investigations             → 201  (body: application_id=TEST_API_APPLICATION_ID; extract id)
GET  /api/v1/investigations/{id}        → 200  (verify read)
POST /api/v1/investigations/{id}/cancel → 202  (pin body status == "CANCELLING")
GET  /api/v1/investigations/{id}        → 200  (pin state == CANCELLED)
```

(`cancel` persists `CANCELLED` synchronously while the 202 body reports
`"CANCELLING"` — both assertions are pinned so a behavioral change is caught.)
No investigation `DELETE` exists; rows accumulate on the scratch tenant by
design (C6). Pause/retry probes on a fresh investigation yield 409/502/503 and
are not part of this flow.

### 8.2 Chat session lifecycle (default)

```
POST   /api/v1/chat/sessions               → 201  (extract session_id)
GET    /api/v1/chat/sessions/{id}/messages → 200  (verify read)
DELETE /api/v1/chat/sessions/{id}          → 200  (assert {200, 409}; 409 = legal hold)
GET    /api/v1/chat/sessions/{id}/messages → 404  (verify deleted; only when DELETE was 200)
```

The template's update step maps to `POST …/messages`, which is SSE + LLM-backed
and stays probe-only (§5.2).

### 8.3 Batch-intake job lifecycle (opt-in via `TEST_API_FLOWS`)

```
POST   /api/v1/batch-intake                → 202  (extract job_id; spawns children — disposable-stack preferred)
GET    /api/v1/batch-intake/{job_id}       → 200
POST   /api/v1/batch-intake/{job_id}/cancel→ 202  (pin body status == "CANCELLING")
DELETE /api/v1/batch-intake/{job_id}       → 200  (pin status == "PURGED") | 409 (active or within retention)
GET    /api/v1/batch-intake/{job_id}       → 404  (only when purge returned 200; otherwise still 200)
```

Purge is retention-gated (`purge_batch`: terminal + past retention, else 409),
so the delete→404 tail is conditional, not unconditional — the flow encodes
both branches.

---

## 9. Dependencies

`pyproject.toml` (dev group):

- `schemathesis>=4.31,<5` (new)
- `httpx` (new declaration; currently transitive-only at 0.28.1 — confirm the
  resolved version constraint before pinning)

`pytest-asyncio` is already declared; flows stay synchronous so no async
plugin configuration is needed.

---

## 10. Running

Prerequisites: a live stack (`scripts/start-platform.sh` — compose infra +
Postgres + API on `:8000`), `/openapi.json` served (dev or
`IAP_ENABLE_DOCS=true`), and the `TEST_API_APPLICATION_ID` profile seeded for
`TEST_TENANT_ID`.

```bash
# System/API suite (contract + coverage + flows)
IAP_LIVE_TESTS=1 TEST_TARGET_URL=http://127.0.0.1:8000 \
  uv run pytest tests/system_api -v

# Direct Schemathesis CLI spot-check (raw spec; fails on first undocumented
# 401/404 by design — diagnostic aid, not a pass/fail gate)
uv run schemathesis run http://127.0.0.1:8000/openapi.json \
  -H "X-Tenant-ID: system-api-suite"
```

---

## 11. Risks and non-goals

- **Live stack is mandatory.** No in-process shortcut is viable (DB/Temporal
  lifespan). Mirrors the existing live suite.
- **Side-effect containment** rests on §5.2 (every probe verified
  side-effect-free) + the scratch tenant (§4.1) + the disposable-stack rule for
  promoted classes. Investigation rows are evergreen by API design.
- **Non-goals:** OpenAPI `links`-driven stateful testing (no links declared);
  object-level authorization checks (no security schemes declared); websocket
  routes (not in OpenAPI); load/latency testing.

---

## 12. Disposition of review feedback

| Item | Resolution in v1.0 |
|---|---|
| C1 (probe→422 unachievable; `start`/`aggregate-reports` have no safe probe) | Replaced with per-route probe table (§5.2). Correction to the review itself: safe probes *do* exist for both — `start` + fabricated UUID → 404 (lookup precedes dispatch), `aggregate-reports` valid body *without* key → 400 (key check precedes job creation). `ProbeSpec` field added; `COVERAGE_ONLY` documented as the escape hatch for future routes with no safe probe (zero current members). |
| C2 (WRITE_BOUNDED not bounded; missing `headers`) | `intake/errors`, `pause`/`resume`/`approve-action`, `batch retry` → WORKFLOW_EXPENSIVE (§5.1). `headers` field added for `X-Idempotency-Key` injection. State gates documented (`retry` only from `FAILED`; `fulfill` 409 unless `AWAITING_INPUT`). |
| C3 (resolve_params over-claim) | `resolve_params` restricted to workflow-free ids (investigation, chat session); all other parameterized routes probe with fabricated UUIDs expecting 404 (§5, §5.2). |
| C4 (blanket error injection masks regressions; omits 502/500) | Injection removed. Two tiers: raw-spec `status_code_conformance` for the fuzz corpus on the fully-wired stack; per-route `expected_statuses` for probes/flows (§6). |
| C5 (live-gate insufficient; 503 wall) | Gate on `/health/ready` == 200/`READY`; fuzz corpus assumes the gated fully-wired state (§4.3). |
| C6 (evergreen mutation on shared tenant) | Scratch tenant default `system-api-suite` (§4.1); `POST /investigations` capped; no-DELETE accumulation documented (§8.1, §11). |
| M1 (coverage-vs-schema ambiguity) | Coverage always vs raw spec; exclusions runtime-only; live spec cross-checked against import-time `create_app().openapi()` (§7). |
| M2 (gating semantics) | Presence in `os.environ` (§4.2). |
| M3 (`response_headers_conformance`) | Dropped from the kept set as vacuous (§6). |
| M4/M5 (positive/negative check value + phase dependence) | Not promised in v1.0; revisit after observing a run (§6). |
| M6 (timeout) | `TEST_API_REQUEST_TIMEOUT` (default 10 s) alongside `deadline=None` (§4.1, §6). |
| M7 (cancel `CANCELLING` vs `CANCELLED`) | Both pinned (§8.1). |
| M8 (chat delete 409) | `{200, 409}` with conditional 404 tail (§8.2). |
| M9 (httpx/async deps) | `httpx` confirmed undeclared → new declaration; flows stay sync (§2.7, §9). |
| M10 (CLI caveat) | Documented as diagnostic aid, not gate (§10). |

## 13. Open question (carried)

- Whether usage lives in a short `tests/system_api/README.md` or in
  `conftest.py` docstrings only — decide at implementation.
