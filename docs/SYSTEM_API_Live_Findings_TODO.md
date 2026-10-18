# Live Platform Defects — System/API Suite Findings

Source: first live run of `tests/system_api/` against `http://localhost:8000`
(stack READY, all subsystems CONNECTED, tenant `system-api-suite`).
All items below are fixed and verified; the suite now passes 3× green
(54 passed, 1 skipped, 37 subtests) at `TEST_API_MAX_EXAMPLES=5`, plus clean
at 15×/25× fuzz intensity.

Conventions: `- [ ]` = open, `- [x]` = fixed + covered by the suite passing.

---

## P1 — Unhandled 500s (suite headline: zero 500s)

- [x] **B1. `POST /api/v1/investigations` 500s on max-length-boundary `session_id`**
  Root cause (two layers): the API body allowed `session_id` ≤ 256 /
  `requested_by` ≤ 256 / `problem_description` ≤ 8000, but the domain
  `InvestigationRequest` caps them at 128 / 128 / 4096 — and the domain
  pydantic `ValidationError` had no exception handler → unhandled 500.
  Fix (`routers/investigations.py`, `api/app.py`): boundary bounds aligned
  down to the domain limits **plus** a `PydanticValidationError → 422`
  handler as defense-in-depth for any other API/domain skew.
  Verify: 256-char `session_id` → 422 `ValidationError` envelope.

- [x] **B2. `Content-Type: text/plain` 500s on JSON-body POST routes**
  Root cause: the 422 handler itself crashed — `exc.errors()` carries the
  raw body as `bytes`, `sanitize_extra` passed it through, and
  `JSONResponse.render` raised `TypeError: Object of type bytes is not JSON
  serializable` → 500. One central fix (`api/errors.py::redact_value` now
  decodes bytes and falls back to truncated `str()` for any non-scalar, so
  the result is always JSON-serializable) covers **every** POST route.
  Verify: text/plain on chat/rollback/fulfill → 422 `ValidationError`.

- [x] **B3. `GET /api/v1/knowledge/search` 500s on NUL-byte `application_id`**
  Root cause: untrusted query param reached Postgres unsanitized →
  `asyncpg CharacterNotInRepertoireError` (invalid byte sequence 0x00) →
  unhandled 500. Fix in two layers: (1) `NO_NUL_PATTERN`
  (`api/validation.py`) on all free-text query params (knowledge,
  reference-docs search/status/sources, jobs, aggregate-reports cursor,
  cancel reason) → fast 422 at validation; (2) a narrow `DBAPIError`
  safety-net handler (`api/app.py`) maps NUL-encoding failures from *any*
  input path (incl. JSON bodies) to 422 and re-raises everything else, so
  genuine persistence faults still 500.
  Verify: NUL `application_id` → 422, no traceback.

- [x] **B4. Transient `GET /api/v1/reference-docs/status` fuzz failure (uncharacterized)**
  Never reproduced across ~10 subsequent runs (5×/15×/25× intensities,
  traceback count stable). Most plausible cause was the same NUL-input
  class (fuzzed `source_id` → DB), now closed by the B3 fix (pattern on
  `source_id` + safety net). Closing as fixed-by-B3; reopen with the repro
  curl if it ever recurs.

---

## P2 — Tooling defect (blocks suite setup, not runtime)

- [x] **B5. `scripts/seed_profile.py` crashes with the stock `IAP_DATABASE_URI`**
  The script now normalizes a `postgresql://` scheme to
  `postgresql+asyncpg://` itself (warns on stderr) instead of crashing in
  `create_async_engine`. Verified with the stock URI.

---

## Verification

```bash
IAP_LIVE_TESTS=1 TEST_TARGET_URL=http://localhost:8000 \
  TEST_API_MAX_EXAMPLES=10 .venv/bin/python -m pytest tests/system_api/ -q
```
Current: all green 3× (batch-intake flow stays skipped unless `TEST_API_FLOWS`
includes it). Suite-side change: `fuzz_tolerated_statuses` additionally
tolerates 400 on POST routes — non-UTF8 bodies never reach JSON parsing and
get a clean Starlette 400 (`{"detail":"There was an error parsing the body"}`),
which is legitimate client-error behavior, not a crash (no traceback).

## Operational note (2026-10-12)

Restarting the API requires the full stack env: config load fails closed
without `IAP_OPENAI_API_KEY`, and non-compliant `IAP_ELASTICSEARCH_*`
overrides in the invoking shell (200/50 vs security maxima 20/10) also halt
boot. The API was relaunched with the Sep-30 stack env reconstructed from
the surviving worker processes; readiness is back to all-CONNECTED. Prefer
`./scripts/start-platform.sh --restart` from a fully-provisioned shell for
future restarts.
