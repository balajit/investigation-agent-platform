# IAP Implementation Part 11: Task Tracker v1.1

Sequential, dependency-ordered build plan for the remaining Part 11 work.
Companion to `docs/IAP-implementation-part11-knowledge-v1.1.md` (architecture)
and `docs/IAP-implementation-part11-plan-todo-v1.1.md` (phase specifications).

## Conventions

- Strictly sequential, one phase at a time, in the order below. Each phase
  splits into **two trackable units**: `N.a` (source) then `N.b` (tests).
- `N.a` gate: `ruff check` + `ruff format --check` + `mypy src` only.
  No `pytest` in `.a` tasks.
- `N.b` gate: full `uv run pytest tests -q -p no:cacheprovider` plus the
  `.a` gate. Zero regressions against baseline plus all new tests.
- `mypy src` ceiling: zero *new* errors beyond the documented pre-existing
  baseline. Verify attribution per error (untouched lines only).
- New Alembic migration per persistence phase; update the chain tests
  (`test_knowledge_coverage.py`, `test_remediation_coverage.py`) in the
  source task; convert the prior phase's single-head test to an ancestor
  assertion; add the dedicated new-head test in the `.b` task.
- Every new workflow/activity registers in the canonical
  `WORKFLOWS`/`ACTIVITIES` (or `ANALYTICS_*`) lists in
  `bootstrap/worker.py`. Every new repo gets SQL + in-memory impls,
  `AppContext` + bootstrap wiring, and buffered-dev entries where durable.
- Checkpoint rule: stop after each `.a` for confirmation before `.b`.

## Status Legend

- `[x]` done · `[ ]` pending. Token spend is logged per completed unit.

## Completed

- [x] **Task 0.a** — Source: Platform correctness (~1 unit)
- [x] **Task 0.b** — Tests: Platform correctness (~1 unit)
- [x] **Task 1.a** — Source: Extension contracts (~1 unit)
- [x] **Task 1.b** — Tests: Extension contracts (~1 unit)
- [x] **Task 2.a** — Source: Persistence completion + profile revisions (~1 unit)
- [x] **Task 2.b** — Tests: Persistence completion + profile revisions (~1 unit)
- [x] **Task 3.a** — Source: Shared operational foundations (~2 units)
- [x] **Task 3.b** — Tests: Shared operational foundations (~2 units)
- [x] **Task 4.a** — Source: Security foundation + outbound credentials (~1 unit)
- [x] **Task 4.b** — Tests: Security foundation + outbound credentials (~1 unit)
- [x] **Task 5.a** — Source: Structured input-required suspension (~2 units)
- [x] **Task 5.b** — Tests: Structured input-required suspension (~1 unit)
- [x] **Task 6.a** — Source: Finding clustering (~2 units)
- [x] **Task 6.b** — Tests: Finding clustering
- [x] **Task 7.a** — Source: Aggregate reporting
- [x] **Task 7.b** — Tests: Aggregate reporting
- [x] **Task 8.a** — Source: Conversational investigation chat
- [x] **Task 8.b** — Tests: Conversational investigation chat
- [x] **Task 9.a** — Source: Defensive LLM JSON recovery
- [x] **Task 9.b** — Tests: Defensive LLM JSON recovery
- [x] **Task 10.a** — Source: Reference-document indexing + search
- [x] **Task 10.b** — Tests: Reference-document indexing + search
- [x] **Task 11.a** — Source: Bulk investigation intake
- [x] **Task 11.b** — Tests: Bulk investigation intake
- [x] **Task 12.a** — Source: Integration + operations

Running total: **~17 units**.

## Remaining (in execution order)

### Task 6.b — Tests: Finding clustering

Depends: 6.a. Done: `tests/unit/test_part11_phase7.py` (22 tests).

- [x] Incremental run processes only unassigned findings (seed assigned +
  unassigned, assert only unassigned touched).
- [x] Semantic-candidate cap enforced (≤12 candidates passed to LLM).
- [x] New-cluster creation persists row + assignment with history.
- [x] Malformed LLM output (missing array, unknown ids, duplicates) →
  explicit `UNASSIGNED`, nothing silently dropped.
- [x] Assignment history preserved across re-runs (valid_from/valid_to).
- [x] Tenant isolation on cluster read/list/assign.
- [x] Embedding model/version migration never mixes spaces.
- [x] API tests: taxonomy list, cluster detail + members, run trigger
  (idempotent replay, 400/404/409/502/503 mapping).
- [x] Migration 012 chain + single-head test; convert 011-head test to
  ancestor assertion.
- [x] Full gate.

### Task 7.a — Source: Aggregate reporting (Phase 11.8)

Depends: 6.b (consumes cluster taxonomy).

- [ ] `AggregateReport` / `AggregateReportRow` domain models (versioned,
  bound to exact taxonomy/assignment generations + renderer version +
  input digests + provenance; no independent regrouping logic).
- [ ] `ReportRendererPort` + `ReportRendererRegistry` (reuse 11.1 registry
  pattern); Jinja2 `HtmlAggregateReportRenderer` (autoescape + CSP; add
  `jinja2` to `pyproject.toml` only after verifying it is not already a
  transitive dependency).
- [ ] `InvestigationAggregateReportService.build` (consumes
  `FindingClusterRepository`).
- [ ] Artifact storage via `ArtifactStorePort`: `latest/{tenant}/...` +
  `history/{tenant}/{run_id}/...` (opaque internal scope IDs; authorized
  proxy or short-lived signed URL, never raw keys).
- [ ] `aggregate_report` background-job descriptor on the analytics queue;
  bounded workflow inputs + explicit history threshold (partition or
  Continue-As-New if a run can exceed it); retention/purge + idempotent
  regeneration semantics.
- [ ] API `api/v1/routers/aggregate_reports.py`: `POST
  /api/v1/aggregate-reports` (202, idempotent), `GET .../latest`, `GET
  .../history` (paginated), tenant-scoped async delete/purge respecting
  retention/legal hold.
- [ ] Register workflow + activities in canonical lists; wire router in
  `api/app.py`.
- [ ] Source gate (ruff + format + mypy, no pytest).

### Task 7.b — Tests: Aggregate reporting

Depends: 7.a. Done: `tests/unit/test_part11_phase8.py` (23 tests).

- [x] Exact-generation reproducibility (same taxonomy snapshot → identical
  report bytes).
- [x] Tenant isolation + authorized artifact access (cross-tenant 404, no
  key leakage).
- [x] Stored-XSS + CSP tests (malicious finding/cluster text escaped).
- [x] Empty-taxonomy renders a clear "no data yet" state.
- [x] Retention, legal hold, cascade cleanup.
- [x] Full gate.

### Task 8.a — Source: Conversational investigation chat (Phase 11.10)

Depends: 11.3 foundations (Task 3).

- [ ] `domain/investigation/chat.py`: `ChatMessage`, `InvestigationChatSession`
  (durable, versioned).
- [ ] Separate `StreamingLLMGateway` protocol + registry — do NOT alter the
  structural `LLMGateway` requirement.
- [ ] `ChatContextBuilderPort` reusing existing knowledge retrieval for
  investigation-bound sessions; session-scoped data-policy envelope for
  pre-investigation chat (do not weaken investigation identity).
- [ ] Versioned SSE protocol: `token` / `metadata` / `final` / `error`;
  `final` carries the complete schema-validated response + typed suggested
  actions (advisory only; execution via normal authorized/idempotent APIs).
- [ ] Disconnect cancels/closes provider stream; backpressure, timeout, max
  stream duration, redaction, token/cost accounting, no-retry-after-emission.
- [ ] Migration: `chat_sessions` + `chat_messages` (tenant RLS, retention,
  purge); SQL + in-memory repos; `AppContext` + bootstrap + buffered wiring.
- [ ] API `api/v1/routers/chat.py`: `POST /api/v1/chat/sessions`, `POST
  .../{id}/messages` (SSE), `GET .../{id}/messages` (paginated), `DELETE
  .../{id}`. No keyword-sniffing triggers.
- [ ] Source gate (ruff + format + mypy, no pytest).

### Task 8.b — Tests: Conversational investigation chat

Depends: 8.a. Done: `tests/unit/test_part11_phase10.py` (24 tests).

- [x] Persistence across restart (durable SQL round-trip on real
  PostgreSQL when reachable, skipped otherwise — not in-process).
- [x] Cross-tenant session access rejected.
- [x] Stream event ordering + final-schema validation.
- [x] Disconnect cancellation, no duplicated text on provider failure.
- [x] Suggested action cannot bypass normal authorization/idempotency.
- [x] Retention/purge + quota tests.
- [x] Migration chain test (013 head; full linear chain); full gate.

### Task 9.a — Source: Defensive LLM JSON recovery (Phase 11.11)

Depends: 11.1 contracts (Task 1).

- [ ] Pure `resilient_json_extract` utility + typed error (fence-strip →
  direct parse → outermost-object scan → brace/bracket-balance truncation
  repair). No I/O, no logging of payloads.
- [ ] Use restricted to: known schema + provider-confirmed truncation (or
  explicit call-site permission) + full schema/domain validation +
  non-sensitive/non-destructive use. Never a replacement for F-027 strict
  `response_format: json_schema` (remains primary).
- [ ] Provenance fields: finish reason, parse mode, repair operations,
  schema version, content digest. Errors contain no raw response excerpt.
- [ ] Parse success/failure/repair metrics (no sensitive content,
  bounded-cardinality labels).
- [ ] Source gate (ruff + format + mypy, no pytest).

### Task 9.b — Tests: Defensive LLM JSON recovery

Depends: 9.a. Done: `tests/unit/test_part11_phase11.py` (31 tests).

- [x] Code fences, trailing prose, confirmed-truncation repair + validate.
- [x] Missing required fields, duplicate keys, excessive nesting, altered
  tool arguments, unterminated strings, domain-invalid values → all fail
  closed (extraction fail-closed + downstream schema/domain rejection).
- [x] Authorization/tool-execution/credential call sites reject repaired
  output (sensitive/destructive permission pattern).
- [x] Bounded property-based/fuzz tests (seeded 500-input fuzz + truncation
  prefix sweep).
- [x] Full gate.

### Task 10.a — Source: Reference-document indexing + search (Phase 11.6)

Depends: 11.4 credentials (Task 4, for git/S3 fetch auth).

- [ ] Versioned discriminated `ReferenceDocumentSource` + fetch specs
  (`git` via `pygit2`, confined `local_path`, S3 via `ArtifactStorePort`).
- [ ] `ReferenceDocumentPort` (scope-aware search/index/status, cursor
  pages); dedicated application service — do NOT fold into the runtime
  evidence gateway.
- [ ] Platform-owned `reference_document_chunks` + index-generation tables
  (typed scope, content hash, source revision, lifecycle, provenance,
  lexical `tsvector`, fixed-dim vector, model/version); B-tree + GIN +
  HNSW; hybrid bounded lexical/vector retrieval with reciprocal-rank
  fusion; tombstones excluded from both branches.
- [ ] Generation reconciliation (activate only successful generations;
  tombstone unseen files; failed runs keep prior generation active);
  blue/green re-embedding workflow + rollback window; indexing workflow
  history/event thresholds + bounded batches + resumable cursor +
  Continue-As-New.
- [ ] Security: endpoint allowlists/SSRF, resolved-root/symlink
  protections, clone/object/file/chunk/time/count limits, immutable source
  revision provenance. Reuse 11.4 `EndpointPolicy`/`validate_endpoint`.
- [ ] `ReferenceDocsProfile` (optional, versioned) on application-profile
  revisions; `reference_docs_reindex` background-job kind on the indexing
  queue; REST status/reindex/search endpoints; investigation-scoped MCP
  `search_reference_docs` tool + strict registry/schema/contract update.
  Pre-investigation MCP search stays out of scope.
- [ ] Migration 013 (check current head first — do not hardcode).
- [ ] Source gate (ruff + format + mypy, no pytest).

### Task 10.b — Tests: Reference-document indexing + search

Depends: 10.a. Done: `tests/unit/test_part11_phase6.py` (32 tests).

- [x] Fetcher conformance suite (git/local/S3 specs).
- [x] Unchanged-file reindex no-op; changed/deleted-file reconciliation;
  failed generation leaves prior generation active.
- [x] Tenant/application isolation with identical source IDs.
- [x] Hybrid retrieval + embedding-version routing; recall benchmark
  (recall@1 == 1.0) against exact retrieval on a golden query set.
- [x] SSRF, traversal, symlink, oversized-input, secret-redaction tests.
- [x] REST + investigation-scoped MCP tool + strict contract updates;
  migration chain test (014 head); full gate.

### Task 11.a — Source: Bulk investigation intake (Phase 11.9)

Depends: 11.5 structured input (Task 5, for per-record interrupts).

- [ ] Versioned `BatchIntakeRequest` / `BatchIntakeRecord` /
  `BatchIntakeResult` + batch status/read model (durable repo or
  background-job linkage — prefer reusing `BackgroundJob` with kind
  `batch-intake` over a new table; decide during implementation and
  document).
- [ ] Record `parameters` validated against the resolved
  application-profile schema; duplicate external keys rejected; payload /
  record quotas enforced pre-dispatch.
- [ ] Optional CSV adapter with explicit field mapping (workflow input
  stays structured JSON).
- [ ] `BulkIntakeWorkflow`: deterministic opaque child workflow IDs,
  permit-held-until-completion bounding (NOT start-only semaphore),
  parent close policy, child timeouts/retries, partial retries,
  `AWAITING_INPUT` child handling, Continue-As-New only with zero active
  children (carry dispatch cursor/results/idempotency state), 500-record
  cap (larger imports partition into multiple jobs).
- [ ] API: `POST /api/v1/batch-intake` (202, idempotent), `GET
  /api/v1/batch-intake/{id}` (paginated children), cancellation endpoint
  propagating per the declared parent-close policy; retention/purge for
  batch records, child mappings, progress events, idempotency records.
- [ ] Register workflow + activities; wire router.
- [ ] Source gate (ruff + format + mypy, no pytest).

### Task 11.b — Tests: Bulk investigation intake

Depends: 11.a. Done: `tests/unit/test_part11_phase9.py` (30 tests).

- [x] Maximum concurrently *active* child executions (bound pinned at 10;
  permit-held-until-completion windowed dispatch; full execution in 12.a
  golden scenarios).
- [x] Deterministic replay, collision-free IDs across concurrent batches.
- [x] Partial failure, cancellation, waiting-input child, restart,
  Continue-As-New predicate, quota, cross-tenant status rejection.
- [x] Full gate.

### Task 12.a — Source: Integration + operations (Phase 11.12)

Depends: all of 6.b, 7.b, 8.b, 9.b, 10.b, 11.b. Done (worker/queue/schedule/Kafka fixes verified live; golden 38/38 green).

- [x] Register every new workflow/activity in each supported worker
  launcher; verify analytics + indexing queue coverage.
- [x] Finalize task-queue/concurrency config; verify Phase 11.3 schedule
  reconciliation end-to-end (stable IDs, overlap, jitter, pause/unpause,
  catch-up/backfill, update/delete).
- [x] Extend `scripts/run-golden-scenario.sh` with the six Part 11
  scenarios (input suspend/restart/fulfill/resume; source
  index/search/delete/reindex; finding → clustering → report artifact;
  batch partial failure + bounded concurrency; chat stream + action
  authorization; job snapshot + Kafka/WebSocket progress + reconnect).
  Scenarios assert durable database/artifact state, not just HTTP shapes.
- [x] Write Part 11 operations documentation (plugin/capability discovery,
  task queues + worker sizing, schedule reconciliation, Kafka
  subscriber/fan-out topology, artifact storage + signed URLs, vector
  index maintenance + recall checks + re-embedding + vacuum/reindex
  triggers, retention + legal hold + purge + audit, quotas + cost
  controls, credential refresh failures + safe diagnostics, contract /
  plugin upgrade + rollback).
- [x] Source gate (ruff + format + mypy, no pytest; golden scenarios run
  separately, not part of this gate).

### Task 12.b — Tests: Integration + operations (final)

Depends: 12.a.

- [ ] Shared plugin conformance suite (manifest/version negotiation,
  config/secret redaction, lifecycle, scope isolation, idempotency,
  cancellation, restart, quotas, provenance, cleanup).
- [ ] Full security suite (SSRF, traversal, symlink, XSS, cursor
  tampering, WS auth, quota races, cross-tenant identifier collisions).
- [ ] Migration upgrade + downgrade from current production head.
- [ ] Performance baselines (vector search, clustering batch, bulk child
  concurrency, report generation, event fan-out).
- [ ] Full test suite: zero regressions; Ruff + format clean; no new mypy
  errors; API/MCP/Kafka contract fixtures versioned and pinned.
- [ ] Mark `docs/IAP-implementation-part11-plan-todo-v1.1.md` DONE with
  date + actual test counts.

## Explicitly Deferred (do not build unless triggers fire)

- [ ] Bundled operator UI — needs a committed frontend owner + workflows.
- [ ] Runtime third-party code loading — needs independent plugin
  deployment across teams.
- [ ] Dedicated vector database — needs pgvector failing 10x-scale targets.
- [ ] Microservice split — needs task-queue/worker isolation proven
  insufficient.
- [ ] Outbound MCP connection pooling — needs measured connection-churn
  bottleneck.
- [ ] Multi-region deployment — needs latency/sovereignty/recovery mandate.
