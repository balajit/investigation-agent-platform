# Code Review Report

## Summary

- **Repository:** `investigation-agent-platform` — Agentic investigation platform (FastAPI + Temporal + Postgres + Elastic + Oracle + pyGit2 + tree-sitter + LLM gateway). Python 3.13, Pydantic, SQLAlchemy async, Temporal, FastStream/Kafka, OTel.
- **Scope reviewed:** `src/` (117 files), `tests/` (4 files, 8 tests), `pyproject.toml`, `config/`, `migrations/`, `docker/`, `main.py`, `bootstrap/`, `api/` contracts.
- **Applicable standards:** Project-local `pyproject.toml` (`ruff target py313 line 100`, `mypy strict=true python 3.13`, `pytest testpaths tests pythonpath src`) + `standards.md` hierarchy (project instructions > enforced tooling > architectural contracts). No `CONTRIBUTING.md`/`AGENTS.md` found; `README.md` empty.
- **Tools detected & executed:**
  - `mypy` (`uv run mypy src`): **Success — 117 files, 0 errors** (with `overrides.ignore_missing_imports` for `opentelemetry.exporter.otlp.proto.grpc.trace_exporter`)
  - `ruff` (`uv run ruff check src`): **72 errors** (42 `BLE001` blind `except Exception`, `I001` import sorting) — 2 auto-fixed, 42 remain; not used as defect source per `standards.md` unless material
  - `pytest` (`uv run pytest`): **8 passed** (3 files: `test_tenancy_rls`, `test_llm_gateway`, `test_agent_loop_elastic_oracle`)
  - `cargo`/`npm`/`go` etc. — not applicable
- **Build/CI:** No CI workflows found (`.github/` absent); `uv.lock` pinned, `hatchling` build.
- **Limitations:** No Postgres/Elastic/Temporal/Kafka containers running; no credentials for LLM (`IAP_LLM_API_KEY` required by `config.py`); `migrations/001_add_rls.py` not applied; integration tests use in-memory `AppContext` only (`testcontainers` absent per `tools.md` gap). Some review findings verified via static inspection + targeted `mypy`/`ruff` runs; runtime paths (Temporal replay, Kafka idempotence, OTEL export) flagged as `UNVERIFIED` where infrastructure unavailable.

## Findings

### CR-001 — Postgres RLS Hard Requirement Scaffolded But Not Enforced
Severity: CRITICAL
Category: DATA
Confidence: CONFIRMED
Location: `src/investigation_agent_platform/infrastructure/persistence/rls.py:13`, `migrations/001_add_rls.py:1`, `src/investigation_agent_platform/infrastructure/persistence/{hypothesis,timeline,profile}_repository.py:1`, `src/investigation_agent_platform/infrastructure/persistence/models.py:248 HypothesisORM / 232 TimelineEventORM / 418 ApplicationProfileORM`
Evidence: `rls.py:13` defines `rls_session(factory, tenant_id)` with `SET LOCAL app.tenant_id` but grep shows **0 callers** — all SQL repos use `async with self._session_factory() as session:` directly (`investigation_repository.py:95`, `evidence_repository.py:45`, `timeline_repository.py:57`, `hypothesis_repository.py:70`). `migrations/001_add_rls.py` exists but was never `alembic upgrade`'d (directory created Sep 22, no `alembic_version` state). `HypothesisORM:248` and `TimelineEventORM:232` and `ApplicationProfileORM:418` have **no `tenant_id` column** (`Mapped[str]` absent), so even if `SET LOCAL` were set, `CREATE POLICY tenant_isolation USING (tenant_id = current_setting(...))` has no column to filter — policy is vacuously unenforceable. `hypothesis_repository.py:51` `_from_orm` hardcodes `tenant_id="unknown"` and `investigation_id or row.id`.
Failure scenario: Tenant A creates `Hypothesis` `id=uuid1`; Tenant B calls `get_by_id(tenant_id="tenant-b", hypothesis_id=uuid1)` — `hypothesis_repository.py:96` filters only `id == hypothesis_id` (no tenant predicate) → B reads A's hypothesis. RLS GUC set via `rls_session` (if it were used) never checked because missing column.
Impact: Cross-tenant data leak across hypotheses, timelines, profiles, and correlation graph — violates hard prod requirement `REVIEW_GAPS` Gap 4 and `standards.md` security.
Recommendation: Add `tenant_id` columns to `HypothesisORM`/`TimelineEventORM`/`ApplicationProfileORM` (and `InvestigationFactORM`), run migration 001, replace every `self._session_factory() as session` with `async with rls_session(self._session_factory, tenant_id) as session` (already scaffolded but unused), add `tests/integration/test_tenancy_rls.py` asserting tenant-B SELECT returns 0 rows on real Postgres `testcontainers`.

---

### CR-002 — In-Memory Dev Repositories Ignore Tenant (IDOR in CI/Dev)
Severity: HIGH
Category: SECURITY
Confidence: CONFIRMED
Location: `src/investigation_agent_platform/api/dependencies.py:91,105,113,130,142,181`
Evidence: `InMemoryInvestigationRepository.get_by_id(tenant_id, investigation_id)` at `:91` does `return self._store.get(investigation_id)` — `tenant_id` unused. Same for `exists:105`, `save:94`, `delete:102`; `InMemoryApplicationProfileRepository:113` `return self._profiles.get(application_id)`; `InMemoryEvidenceRepository:142` `get_by_id` ignores tenant; `InMemoryHypothesisRepository:198` only `find_by_investigation_and_tenant` filters, other methods ignore. `AppContext:216` defaults to `InMemory*` when `IAP_ENVIRONMENT != production`.
Failure scenario: `pytest` or local `docker-compose` dev run uses `AppContext()`; attacker with `X-Tenant-ID: tenant-b` creates investigation, then `GET /api/v1/investigations/{id}/evidence` with `id` from tenant-a — router secondary `getattr(investigation,"tenant_id") != x_tenant_id → 403` masks defect currently, but any endpoint that omits that check (e.g., direct `evidence_repo.get_by_id` call from `evidence.py:53` after fix still validates investigation ownership, but future caller may not) leaks cross-tenant evidence. CI `test_tenancy_rls.py:11` passes only because it asserts router-level check, not repo isolation.
Impact: False sense of tenant isolation in dev/CI; regression if secondary 403 check ever refactored away.
Recommendation: Partition in-memory stores by `(tenant_id, id)` key (`self._store: dict[tuple[str, UUID], Investigation]`) or assert `stored.tenant_id == tenant_id` on read.

---

### CR-003 — Activities Swallow Failures and Return `success=True` (Silent Data Loss)
Severity: HIGH
Category: RELIABILITY
Confidence: CONFIRMED
Location: `src/investigation_agent_platform/application/worker/activities.py:137,163,254,341,456,500`
Evidence: `retrieve_evidence_activity:152` `except Exception as exc: return GenericActivityResult(success=True, data={"items_count":0,"error":str(exc)})`; `reason_activity:242` `except Exception: return GenericActivityResult(success=True, conclusion_readiness=0.0)`; `execute_action_activity:292` `except Exception: pass` on `save_batch` and `record_action`; `verify_root_cause_activity:365,372` `except Exception: pass`; `conclude_investigation_activity:490` catches `InvalidLifecycleTransitionException` and logs `warning` then returns `success=True:494`; `publish_event_activity:543` swallows publisher missing as success. `workflows.py:174` then marks `final_status="COMPLETED"` even when verification never ran.
Failure scenario: Elastic 503 during `retrieve_evidence_activity` → activity returns `success=True` with 0 items → workflow advances to `verify_root_cause_activity` with empty evidence → `verify` returns `verified=False` → loop breaks at `max_iterations` → `conclude` transitions `CREATED→COMPLETED` (invalid, see CR-007) but swallowed → DB stays `CREATED`, Kafka event never published, caller sees `202 Accepted` workflow completion with no evidence.
Impact: Investigation silently concludes with `INSUFFICIENT_EVIDENCE` but status `COMPLETED`; hidden provider outages never surfaced to operator or tenant.
Recommendation: Make infrastructure failures raise `temporalio.exceptions.ApplicationFailure(..., non_retryable=False/True per domain retryable)` instead of `success=True`; add `workflows.py` `non_retryable_error_types=["SecurityPolicyViolationException","DomainValidationException"]` and let Temporal retry only retryable `ExecutionError`.

---

### CR-004 — Retry/Timeout Misconfigured Across Gateway, Temporal, and LLM
Severity: HIGH
Category: RELIABILITY
Confidence: CONFIRMED
Location: `src/investigation_agent_platform/application/evidence/gateway.py:105`, `src/investigation_agent_platform/application/worker/workflows.py:66`, `src/investigation_agent_platform/infrastructure/reasoning/{openai,anthropic}_adapter.py:54,48`
Evidence: `gateway.py:105` `@retry(stop_after_attempt(3), wait_exponential(min=2,max=10))` decorates **only** `search_runtime_evidence` and has **no `retry_if_exception` predicate** — retries `SecurityPolicyViolationException(retryable=False)` and `ValueError` provider-not-found 3×, amplifying load. Other 5 gateway methods (`get_application_state:149`, `search_code:183`, `get_source:230`, `find_call_graph:247`, `get_code_history:269`) have **no retry and no `asyncio.wait_for` timeout** (only `search_runtime_evidence:130` has `timeout=30`). `workflows.py:66` reuses single `RetryPolicy(maximum_attempts=3)` for all 8 activities, ignoring domain `retryable` flag (`domain/common/exceptions.py:134 False` vs `158 True`). `openai_adapter.py:54` `await client.chat.completions.create(**kwargs)` and `anthropic_adapter.py:48` `await client.messages.create(**kwargs)` have **no timeout, no tenacity, no 429 handling** — workflow `reason_activity` 120s `start_to_close_timeout:105` is the only guard, counted as retry burning budget.
Failure scenario: `X-Tenant-ID` missing → `QuerySafetyPolicy` raises `SecurityPolicyViolationException(retryable=False)` → gateway retries 10s → Temporal retries activity 3× → 30s wasted + 3 duplicate Elastic queries; LLM 429 from OpenAI hangs activity until Temporal kills it, budget `record_usage` never called.
Impact: Retry storms, hanging investigations, cost overrun.
Recommendation: Add `retry_if_exception(lambda e: getattr(e,"retryable", False))` to gateway tenacity, add `asyncio.wait_for(..., timeout=self._timeout_seconds)` to all provider calls, configure httpx `timeout=30` in LLM adapters, set `RetryPolicy(non_retryable_error_types=["SecurityPolicyViolationException","DomainValidationException","InvalidHypothesisException"])`.

---

### CR-005 — Idempotency Store Is In-Memory, Raced, Non-Durable; Evidence Dedup Declared Never Enforced
Severity: HIGH
Category: RELIABILITY
Confidence: CONFIRMED
Location: `src/investigation_agent_platform/api/dependencies.py:284`, `src/investigation_agent_platform/api/v1/routers/investigations.py:54`, `src/investigation_agent_platform/ports/evidence/store.py:9`, `src/investigation_agent_platform/domain/evidence/models.py:106`
Evidence: `_InMemoryIdempotencyStore:284` is `dict` on global `AppContext` (`get_app_context:346` no lock). `investigations.py:54` does `get → execute → set` — TOCTOU race: two concurrent `POST /investigations` with same `X-Idempotency-Key` create two DB rows, second overwrites cached response. No TTL → unbounded memory; restart wipes; multi-replica has no shared store. Evidence side: `Evidence.fingerprint` required, `EvidenceStorePort:9` exists, but `InMemoryEvidenceRepository.save:130` appends blindly, `SqlAlchemyEvidenceRepository.save_batch:75` `on_conflict_do_update` only handles `evidence_key` (UUID string) not `fingerprint` deduplication; `gateway.py` never calls deduplicator; `docs/TODO_LLM_001` notes `EvidenceDeduplicator` absent.
Failure scenario: Client retries `POST /investigations` on 5s timeout → duplicate investigations billed; concurrent Elastic pagination writes duplicate `Evidence` rows with same `_id` fingerprint → storage blowup, double-counted in `total_count`.
Impact: Duplicate billing/investigations; evidence store unbounded.
Recommendation: Replace store with Postgres `idempotency_keys(tenant_id, key UNIQUE, response_json, expires_at)` + transactional `INSERT ... ON CONFLICT DO NOTHING` + `SELECT` under `rls_session`; implement `ON CONFLICT(fingerprint)` or `EvidenceDeduplicator` at gateway.

---

### CR-006 — Path Traversal Checks Inconsistent (Encoded Bypass)
Severity: MEDIUM
Category: SECURITY
Confidence: CONFIRMED
Location: `src/investigation_agent_platform/application/investigation/validator.py:26`, `src/investigation_agent_platform/infrastructure/evidence/security.py:151`, `src/investigation_agent_platform/domain/evidence/requests.py:77`
Evidence: `validator._has_traversal:26` correctly does `unquote` loop + `\\→/` + `..` segment check. `security.py:151-171` (`validate_code_search_request` etc.) only checks literal `".." in file_path` — miss `"%2e%2e%2f"` or `"%252e%252e%2f"` double-encoding; `requests.py:77,106,136` same literal check. Centralized helper exists (`validator._has_traversal`) but not reused.
Failure scenario: `GET_CODE file_path="%252e%252e%2fetc%2fpasswd"` passes `security.py:161` then fails only if `validator` layer runs; direct gateway call (not via `validator`) would allow reading any file in repo outside `sourceRoots` (git tree traversal contained but `sourceRoots` bypass).
Impact: Repository file exfiltration beyond `sourceRoots` via encoded traversal.
Recommendation: Normalize via `unquote` until stable + reject backslash at all layers; centralize to `validator._has_traversal` and import in `security.py`/`requests.py`; make `allowed_roots == []` fail-closed (`validator.py:37` currently `return True`).

---

### CR-007 — Investigation State Machine Incomplete + Terminal Handling
Severity: MEDIUM
Category: CORRECTNESS
Confidence: CONFIRMED
Location: `src/investigation_agent_platform/domain/investigation/models.py:276`, `src/investigation_agent_platform/application/worker/workflows.py:65`, `src/investigation_agent_platform/application/worker/activities.py:486`
Evidence: `valid_transitions[CREATED]` only allows `CONTEXTUALIZING/CANCELLED/FAILED` at `:276`, but `conclude_investigation_activity:486` attempts `CREATED→COMPLETED` directly from current DB row (usually `CREATED` because workflow never calls `transition_to` for `CONTEXTUALIZING/INVESTIGATING...`) → `InvalidLifecycleTransitionException` caught at `:490` `logger.warning` and returned `success=True` → DB never leaves `CREATED`, workflow returns `COMPLETED` to Temporal. `workflows.py:65-195` drives `reason/execute/retrieve/verify` loop without ever transitioning through `INVESTIGATING/CORRELATING/HYPOTHESIZING/VERIFYING`. `delete:153` sets status `"ARCHIVED"` which is not a member of `InvestigationStatus` StrEnum → later `InvestigationStatus(row.status)` at `investigation_repository.py:81` raises `ValueError` on archived row read-after-delete.
Impact: Investigations never progress beyond `CREATED`; archived rows crash reads; no audit of `started_at/completed_at` invariant when never contextualized.
Recommendation: Either make workflow drive full state machine (`CREATED→CONTEXTUALIZING→INVESTIGATING→...→CONCLUDING→COMPLETED`) or widen `CREATED→COMPLETED` edge explicitly; fix `delete` to use `CANCELLED` + `InvestigationStatus` member; add idempotent terminal check before `transition_to(CANCELLED)`.

---

### CR-008 — Observability Correlation and Meter Misuse
Severity: MEDIUM
Category: RELIABILITY
Confidence: CONFIRMED
Location: `src/investigation_agent_platform/api/app.py:77`, `src/investigation_agent_platform/application/worker/workflows.py:32`, `src/investigation_agent_platform/infrastructure/observability/telemetry.py:50`, `src/investigation_agent_platform/ports/observability/telemetry.py:49`
Evidence: `app.py:77` `X-Correlation-ID` set on `request.state` + response header but never injected into `RunInvestigationInput:32` (no `correlation_id` field), `EventEnvelope:14`, Elastic/Oracle queries (`elastic.py:76` no correlation_id), LLM adapters (`openai_adapter.py:35` no header). `publish_event_activity:538` fabricates `correlation_id=investigation_id`. `telemetry.py:50` `self._meter.create_counter("agent.tool.executions")` per `record_tool_execution` call — duplicates instruments (OTel spec requires singleton); only `OpenTelemetryObservabilityAdapter` sets `TracerProvider:32`, but `application/*` use `trace.get_tracer(__name__)` with default no-op provider. `openai/anthropic_adapter:78` sets `estimated_cost_usd=0.0` always; `telemetry.record_llm_call:56` increments token counters, never cost.
Impact: Blind debugging across HTTP→Temporal→Kafka→LLM; cost/budget not metered.
Recommendation: Propagate `X-Correlation-ID` via Temporal memo + Kafka headers + OTel baggage; bind `structlog.contextvars` in `app.py` middleware; create meters once in `__init__` and reuse.

---

## Test Gaps

1. **Failure-path coverage 0%:** No tests for `retryable=False` fast-fail (`test_gateway_retries_only_retryable`), timeout cancellation, Elastic 503 → `InvestigationLimitations` vs RCA, activity `success=False` propagation.
2. **Idempotency & dedup:** No `test_idempotency_concurrent_duplicate_creates_one`, `test_idempotency_survives_restart`, `test_evidence_fingerprint_dedup`, `test_save_batch_idempotent` (see `tools.md` proportional testing guidance).
3. **Security:** No `test_elastic_label_injection_blocked`, `test_oracle_template_enforces_select_only`, `test_code_path_traversal_blocked` (encoded `%2e%2e`), `test_password_json_redaction`.
4. **Concurrency:** No OCC conflict test (`test_investigation_occ_conflict_raises_concurrency_error`), no `rustworkx` parallel expand, no `AppContext` singleton race.
5. **Budget loop:** No `test_budget_exceeded_breaks_workflow_loop`, no token/cost enforcement.
6. **External infra:** No `testcontainers` Postgres/Kafka/Elastic harness; 8 tests are all in-memory or signature checks.
7. **Test harness:** `pytest-asyncio` now present, but `pytest-cov` not run; coverage of normal/invalid/boundary/failure/authorization not yet achieved per `standards.md:104`.

## Unverified Areas

* **Temporal replay, Kafka idempotent producer, OTEL export:** No Temporal dev server, Kafka, Postgres RLS, or OTel collector running during review; workflow replay determinism, `enable_idempotence=True`, and `BatchSpanProcessor` export not runtime-verified — flagged as `UNVERIFIED`.
* **LLM structured output:** `TODO(LLM-001)` stub ships; OpenAI `response_format: json_object` vs Anthropic `messages.create` real JSON schema validation not exercised (requires `IAP_LLM_API_KEY`).
* **Tree-sitter parsers:** `TreeSitterCodeIntelligenceProvider` scaffolds via `tree_sitter_language_pack.get_parser` but `tree-sitter` compiled language availability per platform not verified.

## Recommendations

1. **P0:** Fix RLS E2E: add missing `tenant_id` columns, apply `001_add_rls`, replace all `self._session_factory() as session` with `rls_session`, and add `test_tenancy_rls` on `testcontainers` Postgres.
2. **P0:** Make activities fail-closed (`ApplicationFailure(non_retryable=...)`) and set `RetryPolicy(non_retryable_error_types=[SecurityPolicyViolationException, DomainValidationException, InvalidHypothesisException])`; add `retry_if_exception(retryable)` predicate and `asyncio.wait_for` timeouts to every gateway/LLM call.
3. **P0:** Replace `_InMemoryIdempotencyStore` with Postgres `idempotency_keys` + `ON CONFLICT` under `rls_session`; implement evidence dedup on `fingerprint` at gateway.
4. **P1:** Partition in-memory stores by `(tenant_id, id)` and add JWT verification to `require_tenant` before prod.
5. **P1:** Centralize traversal decoding to `validator._has_traversal` and make `allowed_roots == []` fail-closed.
6. **P1:** Wire full state machine in workflow or explicitly allow `CREATED→COMPLETED`; fix `delete → CANCELLED` and terminal idempotency guard.
7. **P2:** Create OTel instruments once, propagate `X-Correlation-ID` via Temporal memo/Kafka headers/baggage, and configure `structlog.contextvars.bind_contextvars` middleware.

