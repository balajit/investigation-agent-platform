# IAP Part 11: Operations Guide v1.0

Runbook for the Part 11 extension surface (registries, jobs, schedules,
artifacts, vectors, retention, quotas, credentials, upgrades). Companion to
`IAP-implementation-part11-knowledge-v1.1.md` (architecture) and
`IAP-implementation-part11-plan-todo-v1.1.md` (build plan). Verified against
the live stack via `scripts/golden_part11.py` (38 checks, all passing).

## 1. Plugin and capability discovery

- `GET /api/v1/capabilities` (authenticated) lists every registered plugin
  manifest and MCP tool: capability id, contract version, availability,
  modes, safe limits. It never exposes endpoints, secrets, env names,
  object keys, or raw configuration.
- Registries accept only platform-installed implementations
  (`application/extensions/registries.py`); tenant profiles select by id.
- After adding a plugin, confirm it appears in discovery and in
  `scripts/golden_part11.py`-style smoke before declaring it available.

## 2. Task queues and worker sizing

Three queues, one process, three `Worker` instances
(`bootstrap/worker.py::run_temporal_worker`):

| Queue | Workflows | Default config |
|---|---|---|
| `investigation-tasks` (`IAP_TEMPORAL_TASK_QUEUE`) | investigation, retention, janitor, **bulk intake parent** | latency-sensitive |
| `analytics-tasks` (`IAP_TEMPORAL_ANALYTICS_QUEUE`) | clustering, aggregate reports | CPU-heavy batch |
| `indexing-tasks` (`IAP_TEMPORAL_INDEXING_QUEUE`) | reference reindex | I/O + embedding-heavy |

- Concurrency: `IAP_TEMPORAL_MAX_ACTIVITIES` (default 100) /
  `IAP_TEMPORAL_MAX_WORKFLOWS` (default 50) apply per worker. Size analytics
  workers for LLM throughput, indexing workers for fetch/embed bandwidth.
- Every workflow/activity must appear in the canonical `WORKFLOWS` /
  `ACTIVITIES` (`ANALYTICS_*`, `INDEXING_*`) lists — a launch-time audit
  (`scripts/golden_part11.py` does not cover this; the 12.b conformance
  suite does) fails the build on drift. Past incident: three investigation
  activities missing from `ACTIVITIES` broke every child execution at
  runtime with `NotFoundError`; audit before every release.
- Temporal workflow sandbox: workflow code may not lazy-import pydantic
  models (`uuid.uuid4` default factories trip `RestrictedWorkflowAccessError`)
  — hoist domain imports under `workflow.unsafe.imports_passed_through()`,
  keep child ids string-based, and never call sync `.result()` on handles
  (use `execute_child_workflow` tasks). Past incidents, all caught live —
  see `application/worker/workflows.py` header notes.

## 3. Schedule reconciliation

- `TemporalScheduleReconciler` (`infrastructure/scheduling/reconciler.py`)
  converges Temporal schedules to `ScheduleDescriptor` desired state:
  stable ids (`finding-clustering-{tenant}`), `SKIP` overlap, 300s jitter,
  no backfill, pause/unpause, update-or-create per descriptor with
  per-descriptor error collection (one bad schedule never blocks the rest).
- Tenant enumeration is explicit: `IAP_SCHEDULING_TENANTS` (comma-separated).
  Empty means skip (on-demand runs stay available). Reconciliation runs at
  worker boot and is non-fatal by design — scheduler outages must never
  take down execution.
- Operator use: build descriptors via `finding_clustering_schedules()` and
  call `reconcile()` directly for pause/unpause/delete outside deploys.

## 4. Kafka subscriber / fan-out topology

- One pattern subscriber per API replica
  (`build_job_pattern_consumer`, regex `^{prefix}-jobs\..*`), started in the
  API lifespan via `ensure_job_messaging()` (idempotent, 20s bounded start).
  Replica-unique consumer group `{base}-{hostname}` (`replica_consumer_group`;
  `IAP_REPLICA_ID` overrides): shared groups are prohibited — partitions
  would load-balance away from local sockets.
- Local dev requires resolvable advertised listeners:
  `KAFKA_CFG_ADVERTISED_LISTENERS=PLAINTEXT://localhost:9093` in
  `docker/docker-compose.yml` (in-network `kafka:9092` is unresolvable from
  the host and wedges `broker.start()`; startup is timeout-bounded and
  degrades to DB snapshots + local hub).
- Publishing: `maybe_publish_job_progress()` is called best-effort from all
  five job-update activities (RUNNING/DONE/FAILED) and all five dispatch
  endpoints (QUEUED). Missing broker or Kafka outages skip silently (warning
  only) — Postgres rows stay authoritative.
- Clients: WS receives a DB snapshot first, then live events; reconnect
  re-reads the row. Duplicate delivery across replicas is intentional;
  event ids make local fan-out idempotent.

## 5. Artifact storage and signed URLs

- `ArtifactStorePort`: S3-compatible adapter in production, resolved-root
  local adapter (`data/artifacts`) in dev. Adapters own key construction;
  callers pass logical keys only.
- Clients receive metadata + short-lived signed URLs (3600s), never raw
  storage keys. Report layout: `aggregate-reports/latest/{scope}/...` and
  `.../history/{scope}/{job_id}/...` with opaque scope digests (no tenant
  ids in keys).
- Dev note: API and worker are separate processes — the dev filesystem
  store is what makes worker-written artifacts readable by the API
  (in-memory stores are process-local by design).

## 6. Vector index maintenance and recall

- pgvector HNSW indexes require declared dimensions: finding embeddings
  and reference chunks are pinned to 1536 (`FINDING_EMBEDDING_DIMS`,
  `REFERENCE_EMBEDDING_DIMS`). A different-dimension model needs a DDL
  migration — same-dimension upgrades flow through generations.
- Spaces are isolated by `(model, version)` at query time in both SQL and
  in-memory implementations; vectors of wrong dimensionality are rejected
  fail-closed at the repository boundary.
- Blue/green: new generations carry the new model/version; reads use
  `MAX(generation) WHERE status=ACTIVE`; rollback reactivates the prior
  generation. Retrieval blends bounded lexical (GIN/`ts_rank`) and vector
  (HNSW cosine) candidates with reciprocal-rank fusion; tombstones are
  excluded in both branches *and* by active-generation scoping (a past
  incident leaked superseded generations into search).
- Recall checks: golden query sets live in `tests/unit/test_part11_phase6.py`
  (recall@1 == 1.0); re-run after any re-embedding. Vacuum/reindex triggers:
  after large purges run `VACUUM ANALYZE` on chunk tables and `REINDEX`
  the HNSW indexes during low-traffic windows.

## 7. Retention, legal hold, purge, audit

| Resource | Retention | Purge path |
|---|---|---|
| Reports | 90d (`REPORT_HISTORY_RETENTION_DAYS`) | `DELETE /aggregate-reports` (hold → 409) |
| Chat sessions/messages | 90d (`CHAT_RETENTION_DAYS`) | service purge; `DELETE` per session (hold → 409) |
| Batch records | 90d (`BATCH_RETENTION_DAYS`) | `DELETE /batch-intake/{id}` (terminal + expired only) |
| Background jobs | per `retention_class` | async purge job (terminal rows) |
| Idempotency records | TTL (store default 24h) | automatic expiry |

- Legal hold is a per-record/session flag that refuses deletion with 409.
- Purge helpers are fail-safe: they report success only when zero rows
  remain. Orphaned artifacts (owning job gone) are treated as expired;
  unattributable keys are never deleted.
- Security-sensitive operations (credential acquisition/use, artifact
  access/deletion, input fulfillment, action execution, config activation)
  emit immutable audit records distinct from diagnostic logs.

## 8. Quotas and cost controls

- `QuotaPolicy` resolves per scope; checks are atomic pre-dispatch with
  `429 + Retry-After` on rejection and `413` on oversized payloads.
- Batch: 500 records / 1MB payload caps plus per-record `batch.record`
  checks. Chat: session/stream caps. Reports: history caps.
- LLM cost: every call records model, tokens, pricing version, and
  estimated cost (`LLMCallMetadata`); chat turns account prompt + completion
  tokens via tiktoken (char fallback). Rates are data (`IAP_PRICING_JSON`
  overrides without code changes); estimates never gate execution.
- Parent workflows bound active children (batch: 10 held start→completion);
  oversized imports partition into multiple jobs — never raise the cap to
  fit one import.

## 9. Credential refresh failures and safe diagnostics

- Outbound tokens cache by `(tenant, application, provider)` with per-key
  refresh locks and proactive pre-expiry refresh; terminal auth failures
  invalidate the entry.
- Never log tokens, secrets, auth responses, or secret references — only
  provider ids, expiry timestamps, and truncated digests.
- `SecretReference` values resolve only against explicit allowlists
  (`resolve_secret(..., allowed_env_names=...)`); arbitrary env names are
  rejected. Diagnose refresh failures via provider status + last-refresh
  timestamps, never by echoing credentials.

## 10. Contract / plugin upgrade and rollback

- All persisted polymorphic payloads, events, plugin configs, and
  artifacts carry `contract_version`/`schema_version`; upcasters migrate
  old rows on read. Unknown enum values surface as `UNKNOWN`, never
  silently coerced.
- Plugin compatibility: manifests declare `contract_version`;
  `CompatibilityRange` gates registration fail-closed.
- Rollback windows: generations (reference/cluster embeddings) and report
  history keep prior actives reactivatable (`rollback_generation`,
  retained report runs). Migration downgrades are tested per phase
  (`downgrade()` in every revision; chain tests pin linearity).
- API/MCP/Kafka contracts are pinned by fixtures
  (`tests/contract/`, `TOOL_ARGUMENT_PROPERTIES`, `EXPECTED_TOOLS`):
  additive changes only under `/api/v1`; breaking changes require a new
  contract version.

## 11. Golden scenarios and release checklist

- `scripts/run-golden-scenario.sh --part11 [--only a,b,...]` runs
  `scripts/golden_part11.py` (38 checks): input, refdocs, clustering,
  batch, chat, jobs-WS — every scenario asserts durable DB/artifact state
  (row counts, generations, digests, signed URLs), not HTTP shapes.
- Release gate (12.b): full `pytest` (zero regressions), `ruff check` +
  `format --check` on `src tests scripts`, `mypy src` at the documented
  10-error baseline, migration upgrade + downgrade from the production
  head, golden core + part11 green, plan doc marked DONE with counts.
