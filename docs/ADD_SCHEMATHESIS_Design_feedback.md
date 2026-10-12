# Critical Review Feedback — ADD_SCHEMATHESIS_Design.md

**Review method.** Claims in the design were checked against the actual router
implementations in `src/investigation_agent_platform/api/` (the 48-operation
OpenAPI inventory, `investigations.py`, `chat.py`, `events.py`,
`aggregate_reports.py`, `clusters.py`, `batch_intake.py`, `reference_docs.py`,
`tenant.py`, `application/investigation/services.py`) and against the
Schemathesis 4.x reference docs (`schema.exclude`, `schema.include`,
`openapi.from_dict`, checks). This is a design-stage review; nothing was
changed.

**Verdict.** The architecture (registry-driven coverage, per-route modularity,
4.x idioms) is sound and worth implementing. But the design makes three
blanket promises that the platform cannot honor — "presence probe → 422",
"resolve_params ⇒ reads hit 200", and "WRITE_BOUNDED is safe to fuzz" — and one
background assumption ("the target stack is live-ready in a way that obeys the
raw spec") that is likely false on a typical dev box. Section 1 lists the fixes
that must land before implementation; Section 2 records the rest.

---

## Severity legend

| Severity | Meaning |
|----------|---------|
| **BLOCKER** | Design premise is factually wrong or unimplementable as written; throws the approach or the safety guarantees into doubt. |
| **MAJOR** | Works for some routes but fails or misleads for others; needs an explicit mechanism, not just wording. |
| **MINOR** | Correctness/robustness/consistency nit; low risk but should be settled. |

---

## 1. Blockers and majors (must be fixed in the design)

### C1 — BLOCKER: "One probe with a payload guaranteed to fail validation → expect 422" is not achievable for several WORKFLOW_EXPENSIVE routes and can trigger real work

The design (§5.2) promises a safe presence probe per WORKFLOW_EXPENSIVE/STREAMING
route. Verified per route:

| Route | Body | Can a probe get 422 without side effects? | Reality |
|---|---|---|---|
| `POST /investigations/{id}/start` | **no body** | **No.** There is no 422 input. | Valid UUID → *actually starts the Temporal workflow* (202) or 502/503; random UUID → 404; malformed → 400. `investigations.py:140-170`. |
| `POST /aggregate-reports` | `legal_hold` has a default | **No.** Every field is defaulted; `{}` + `X-Idempotency-Key` creates a job and starts `AggregateReportWorkflow`. Only missing-idempotency-key gives 400. | `aggregate_reports.py:56-62, 63-173`. |
| `POST /clusters/run` | `limit`/`batch_size` defaulted | **Only with a constraint-violating body** (e.g. `{"limit": 2001}` → 422). A `{}` probe *starts the clustering workflow*. | `clusters.py:31-34, 94-211`. |
| `POST /batch-intake` | `records`/`csv` both optional | `{}` returns **400**, not 422 ("Provide either records or csv"). A valid request spawns child investigations. | `batch_intake.py:100-111, 114-173`. |
| `POST /reference-docs/reindex` | `source_id` required | Yes — `{}` → 422 reliably. | `reference_docs.py:57-59`. |
| `POST /chat/sessions/{id}/messages` | `content` required | Yes — `{}` → 422 before any LLM call. | `chat.py:48-49, 145-156`. |

**Required change.** Replace the blanket "invalid payload → 422" with a
**per-route negative-probe spec**: exactly what request is sent and which status
is expected, verified against the schema. For `start` and `aggregate-reports`
POST there is *no safe probe*: classify them as **COVERAGE_ONLY** (registered,
asserted in the coverage test, never called against a live server) or accept
that touching them requires the disposable-stack mode with controlled idempotency
keys. A generic `RouteScenario` cannot encode this without a `probe` field or a
`safety = COVERAGE_ONLY` class — the design has neither.

### C2 — MAJOR: WRITE_BOUNDED is not "bounded" for several routes, and the scenario dataclass lacks a `headers` field

Verified, all in `src/investigation_agent_platform/api/v1/routers/`:

- **`POST /intake/errors` is neither bounded nor reachable by the test principal.** It is fail-closed to **403** unless the principal is in `IAP_INTAKE_SERVICE_PRINCIPALS` (unconfigured ⇒ 403 by default), and on success it dispatches a Temporal workflow (`investigations.py:281-344`). It belongs in WORKFLOW_EXPENSIVE with an auth-gated probe whose `expected_statuses` includes 403. The design lists it in WRITE_BOUNDED — wrong on both counts.
- **`pause`, `resume`, `approve-action` are workflow-coupled, not bounded.** They signal `wf-investigation-{id}` via `_signal_workflow`; with no running workflow they return **502** (signal delivery failure) or **503** (client not wired) — e.g. `events.py:37-70, 104, 166, 246`. On a *cancelled* investigation they 409 (terminal status guard, `events.py:94-102, 160-164`). Light fuzzing of these produces only error traffic. They behave like WORKFLOW_EXPENSIVE and should be classified/probed accordingly.
- **`retry` only succeeds from `FAILED`** — a bounded probe on a fresh investigation returns **409** (`investigations.py:691-695`). Must be in that route's `expected_statuses`.
- **Idempotency keys are mandatory** (`X-Idempotency-Key` required ⇒ 400 if absent) on `POST /chat/sessions`, `fulfill`, `POST /aggregate-reports`, `POST /clusters/run`, `POST /batch-intake` (`chat.py:97-101`, `investigations.py:412-416`, `aggregate_reports.py:71-75`, `clusters.py:110-114`, `batch_intake.py:122-126`). Schemathesis will not generate this header (it is undeclared in the OpenAPI), so **fuzzing these routes without header overrides produces uniform 400s and covers nothing.**

**Required changes.**
1. Add a `headers` field to `RouteScenario` (e.g. an `X-Idempotency-Key` factory) so registry-driven fuzz/flows can supply required headers.
2. Reclassify: `intake/errors` → WORKFLOW_EXPENSIVE (with 403 auth probe); `pause`/`resume`/`approve-action` → workflow-coupled (probing expects 502/503 or requires a started workflow in disposable mode).
3. Add a `bounded_status_gates` notion (e.g. `retry` only from `FAILED`) so probes know the expected 409 context, or drop these from fuzz and cover via flows.

### C3 — MAJOR: "resolve_params maps … so parameterized reads hit 200, not a 404" is over-claimed

Several path parameters **cannot be produced without a full workflow run**, which
the design forbids (§6.4, "no real workflows"):

- `evidence_id` — no evidence exists for a freshly-created / cancelled investigation.
- `requirement_id` — only exists when an investigation reaches `AWAITING_INPUT` (requires a run).
- `cluster_id` — taxonomy only exists after a clustering run.
- `job_id` (`/jobs/{id}`, `/batch-intake/{id}`) — only after a job is created.
- Even a fresh investigation's `/evidence`, `/findings`, `/hypotheses`, `/timeline` return 200-but-empty *only if* their repos are wired; with a 404-able sub-id these routes will legitimately 404.

**Required change.** The registry must encode per-route **attainable expectations**:
"needs existing id → probe with fabricated UUID, expect 404 (declared)"; "repo
may be absent → expect 503 or skip". Drop the blanket "200, not 404" promise and
encode the real attainable status per route, as the design already proposes for
`expected_statuses` — just don't claim 200 for routes whose IDs cannot be
obtained in the constrained mode.

### C4 — MAJOR: injecting a tolerated-error set into every operation undermines the very "zero 500 / contract" guarantees the suite exists to give

- Injecting `503` into *every* operation means partial wiring (chat store,
  cluster repo, reference repo, artifact store all 503 when not on the app
  context) is silently accepted — masking regressions rather than surfacing
  them, and directly contradicting the "not_a_server_error" headline on a
  typical partially-wired dev stack.
- The tolerated set omits codes the platform genuinely returns: **502** (workflow
  start/signal failure, e.g. `investigations.py:238`, `events.py:68`) and **500**
  (`intake_error`, `investigations.py:325`).
- Injected errors carry no JSON schema, so `response_schema_conformance`
  validates nothing on them — the injection only quietens
  `status_code_conformance`, which is the check most worth running honestly.

**Required change.** Prefer the design's own fallback (§11): disable global
`status_code_conformance` and assert **per-route `expected_statuses` from the
registry** for the probe/flows, but keep `status_code_conformance` active over
the *raw* spec for the fuzz corpus where "anything not declared is suspicious".
If the injection is kept, scope it **per route** (only the statuses that route
can legitimately return) instead of one shared set.

### C5 — MAJOR: `.health/live` is the wrong readiness gate; the suite needs a "fully wired" gate

`/health/live` only proves the process is up. The routes under test 503 whenever
a subsystem is not on `get_app_context()` — chat (`chat.py:103-107, 153-157`),
clusters (`clusters.py:42-44, 67-72`), reference docs (`reference_docs.py:44-50`),
artifact store (`aggregate_reports.py:182-187, 209-213`), Temporal
(`investigations.py:183-192`, `events.py:46-51`, `aggregate_reports.py:108-113`,
`clusters.py:145-150`), input store (`investigations.py:463-468`). On a dev box
that has not run the full `scripts/start-platform.sh` (Temporal especially), the
suite will emit a wall of 503 failures that are environmental, not contractual.

**Required change.** Gate on **`/health/ready`** (or an explicit env flag
`TEST_API_REQUIRE_FULLY_WIRED=1`) so a partially-wired stack fails fast with a
clear message, and document that the fuzz corpus assumes full wiring. This also
resolves the C4 tension for status_code_conformance in the "fully wired" case.

### C6 — MAJOR: fuzzing WRITE_BOUNDED mutates a live store, and investigations cannot be cleaned up

- `POST /investigations` (valid body + an existing application profile) creates a
  durable row **with no DELETE endpoint anywhere in the API**. Repeated fuzz on
  the primary tenant accumulates evergreen rows; `list_investigations` only
  returns *open* investigations, so cancelled/terminal rows are invisible
  garbage.
- `POST /chat/sessions`, `DELETE /chat/sessions/{id}`, `intake` flows and the
  batch-intake opt-in all mutate tenant-scoped state.

**Required change.** State that the default fuzz/flows run against a **scratch
tenant** (`TEST_TENANT_ID` dedicated to the suite, e.g. `system-api-suite`),
that the suite owns (and ideally resets) that tenant's data, and that `POST
/investigations` fuzzing is capped (or moves to the disposable-stack mode). This
should be a documented constraint, not an accident of `tenant-a`.

---

## 2. Minors

1. **Coverage-vs-schema ambiguity (§6/§7).** The coverage test compares the
   registry to the live spec, while conformance runs against an *excluded*
   schema (`schema.exclude(...)`). State explicitly which variant the coverage
   assertion uses (recommend: the raw spec; conformance exclusions are a runtime
   concern only). Also note the coverage check is only as honest as the served
   spec: a route removed from the server vanishes from both. Consider generating
   the registry from `create_app().openapi()` (import-time, no network) so the two
   sides of the equality are anchored.
2. **Env gating semantics (§4.2).** `TEST_TARGET_URL` has a default, so "or
   `TEST_TARGET_URL` is set" must mean `os.environ` *presence*, not the resolved
   value. Spell that out.
3. **`response_headers_conformance` sentence (§6).** The platform declares no
   response headers, so this check is vacuous whether kept or not — the sentence
   "retained through the default check set" overstates it. Either keep it
   explicitly and say it's vacuous, or drop the sentence.
4. **`positive_data_acceptance` is near-vacuous here.** Its default
   `expected-statuses` already tolerates 401/403/404/409/429/5xx, so on a
   tenant-scoped API it rarely fires meaningfully. Fine as a guard; say so.
5. **`negative_data_rejection` / `positive_data_acceptance` phase dependency.**
   These fire only in the relevant test modes/coverage phases; a plain
   `@schema.parametrize()` fuzz run may not produce negative cases. Don't promise
   their coverage without confirming the mode/phase wiring in 4.x.
6. **`deadline=None` vs request timeout.** Fine for excluded SSE/workflow routes,
   but set an explicit per-request timeout (the docs' `--request-timeout` default
   is 10 s) so slow first Hypothesis warmup doesn't yield wall-clock flakes.
7. **Cancel semantics (§8.1).** `cancel_investigation` persists `CANCELLED`
   synchronously but its 202 body says `"status": "CANCELLING"`
   (`investigations.py:729-730`; `services.py:196-224`). The follow-up GET
   assertion (`CANCELLED`) is correct; the design should also pin the POST body
   assertion so a future behavioral change is caught. Pause/retry probes on a
   fresh investigation yield 409/502/503 — flows must not assume 202.
8. **Chat delete legal hold (§8.2).** `DELETE /chat/sessions/{id}` returns **409**
   when legal hold is set (`chat.py:246-250`); assert `200|409` and handle the
   toggle, or run against a tenant with no holds.
9. **Duplicate dependency.** `httpx 0.28.1` is already a transitive dependency;
   confirm whether it is declared in `pyproject.toml` before "pinning explicitly"
   to avoid a no-op or a constraint conflict. `pytest-asyncio` is only needed if
   any flow goes async — the design says sync; keep it sync or add the dep.
10. **Schemathesis CLI command (§10).** `schemathesis run` works, but the
    `-H "X-Tenant-ID: tenant-a"` header flag form is correct as shown; the design
    should also note that the raw spec (no injected statuses) is what the CLI
    tests, so it will fail on the first undocumented 404/401 — useful as a
    *spot check*, not a pass/fail gate.

---

## 3. What is solid

- Schemathesis 4.x mechanics: `openapi.from_dict`, `schema.exclude/include`, and
  `@schema.parametrize()`/`case.call_and_validate()` all exist as used.
- `X-Tenant-ID`-as-identity in development and the *verified-JWT-with-tenant-
  claim* contract in production match `tenant.py` exactly, including the 403 on
  tenant mismatch.
- The chat-session lifecycle is the best CRUD analogue and the delete→404
  sequence is verified in code (`chat.py:229-251` + `201-218`).
- The 48-operation inventory and the 25/17/5/1 class counts match the generated
  OpenAPI.
- "Cover everything, control intensity per module" is the right primary
  strategy for a workflow-heavy API.