# TODO — Pending Items for Next Phase (Tracked)

> Generated from code-review report `.opencode/.code-review/work/report.md` + `uv run pytest --cov` (4122 stmts, 921 miss, 78% → target ≥85%) + manual audit Sep 2025. All items verified via `src/` read + `mypy strict 0 errors` + `ruff 64 BLE001` + `pytest 192 passed`.

## P0 — Data / RLS (Hard Prod Gate)

- [ ] **P0-001 Apply migration 001** — `migrations/001_add_rls.py` exists but not `alembic upgrade`'d. Backfill `tenant_id` on existing rows. Verify `psql \d hypotheses` shows `tenant_id`. Blocks RLS enforcement.
- [ ] **P0-002 Wire `rls_session` E2E** — `infrastructure/persistence/rls.py:13` helper exists but 0 repo callers grep. Replace every `async with self._session_factory() as session:` in `investigation_repository.py:95`, `evidence_repository.py:45`, `timeline_repository.py:57`, `hypothesis_repository.py:70`, `profile_repository.py:32` with `async with rls_session(self._session_factory, tenant_id) as session:`. Current `WHERE tenant_id` filters mask missing RLS; without `SET LOCAL`, `FORCE RLS` would block owner.
- [ ] **P0-003 Add `testcontainers` harness** — No Postgres/Elastic/Temporal/Kafka containers running during review. Add `tests/integration/test_tenancy_rls.py` on real `postgres:16` asserting tenant-B `SELECT` returns 0 rows (currently in-memory `AppContext` only).

## P1 — Resilience / External Services

- [ ] **P1-001 Activities fail-closed** — `application/worker/activities.py:137,163,254,341,456` swallow `Exception` and return `GenericActivityResult(success=True)`. Change to `raise ApplicationFailure(non_retryable= not retryable)` per `domain/common/exceptions.py:134,158`. Pending for 3 activities still `success=False` not `raise`.
- [ ] **P1-002 Retry/Timeout predicates** — `application/evidence/gateway.py:105` tenacity retries `SecurityPolicyViolationException(retryable=False)` (auth). Add `retry_if_exception(lambda e: getattr(e,"retryable",False))` to all gateway methods; `infrastructure/reasoning/{openai,anthropic}_adapter.py:54` `await asyncio.wait_for(..., timeout=30)` + `tenacity` 429 vs 401 split; `application/worker/workflows.py:66` `RetryPolicy(non_retryable_error_types=[...])`.
- [ ] **P1-003 Idempotency dedup** — `_InMemoryIdempotencyStore:284` `dict` TOCTOU `get→execute→set` race + unbounded + multi-replica unsafe. Replace with Postgres `idempotency_keys(tenant_id, key UNIQUE, response_json)` + `ON CONFLICT DO NOTHING` under `rls_session`. Evidence `fingerprint` dedup (`ports/evidence/store.py:9` declared never enforced).
- [ ] **P1-004 In-memory tenant partition** — `api/dependencies.py:91,113` `InMemory*Repository` keyed by `UUID` only (`self._store.get(id)` ignores `tenant_id`). Partition by `(tenant_id, UUID)` for `tests/integration` IDOR coverage.

## P1 — Security

- [ ] **P1-005 Tenant auth** — `api/tenant.py:7` `require_tenant` is header-only `X-Tenant-ID` (comment `Future: validate JWT`). Before prod, add `Authorization: Bearer` JWT verify and bind `app.tenant_id` via `structlog.contextvars`.
- [ ] **P1-006 Traversal decoding** — `security.py:151`/`requests.py:77` check literal `".."`. Centralize to `validator._has_traversal:26` (unquote loop until stable, `\`, `\0`, `..` segment). Already fixed in `validator.py:37` fail-closed `[]→False`, but 2 callers still literal.
- [ ] **P1-007 API validation** — `api/v1/routers/investigations.py:25` `CreateInvestigationBody` lacks `max_length`/`priority` Enum/`parameters` size bound (bypasses `InvestigationRequest:161` `max_length=4096` domain guard). Add `Field(max_length=...)`.

## P2 — Correctness / Observability / Coverage

- [ ] **P2-001 State machine** — `domain/investigation/models.py:276` `valid_transitions[CREATED]` only → `CONTEXTUALIZING/CANCELLED/FAILED`; `workflows.py:65` never drives `INVESTIGATING/CORRELATING`; `conclude:486` `CREATED→COMPLETED` swallowed. Fix `delete:153` literal `"ARCHIVED"` (not in `InvestigationStatus`).
- [ ] **P2-002 Observability** — `api/app.py:77` `X-Correlation-ID` not propagated to `RunInvestigationInput:32`/`EventEnvelope:14`/Elastic/Oracle/LLM; `telemetry.py:50` `create_counter` per call duplicates instruments; `openai_adapter.py:78` `estimated_cost_usd=0.0` always.
- [ ] **P2-003 Coverage 78% → ≥85%** — Missing 921 stmts concentrated in:
  - `bootstrap/worker.py:33 0%`, `main.py:67 57%`, `profile_repository.py:38 42%`, `investigation_repository.py:65 63%`, `hypothesis_repository.py:43 37%` — add `testcontainers` Postgres/Temporal/Kafka tests.
  - Failure-path tests absent: `test_gateway_retries_only_retryable`, `test_elastic_503_as_limitation`, `test_budget_exceeded_breaks_loop`, `test_correlation_parallel_expand`.
  - Security tests: `test_code_path_traversal_blocked` (encoded `%2e%2e`), `test_password_json_redaction` (`password:` colon vs `=`).
- [ ] **P2-004 CI** — No `.github/workflows` (reproducible build, `mypy`/`ruff`/`pytest --cov --cov-fail-under=85`, `deptry` scanning per `pyproject.toml:32`). Add.

## Verification for Done

- `uv run alembic upgrade head` + `psql \d+ hypotheses` shows `tenant_id`
- `grep -r "rls_session" src/ | wc -l` ≥5
- `uv run mypy src` → `0 errors, 117 files`
- `uv run pytest --cov --cov-fail-under=85` → `≥85%` and `192 → ~230` tests (new failure-path + testcontainers)
- `temporal workflow list` + `GET /health/ready` shows real Temporal/Kafka probes (not hardcoded `CONNECTED`)
- `IAP_LLM_PROVIDER=anthropic` ↔ `openai` switch verified via `tests/integration/test_llm_gateway.py`

