# IAP Implementation Part 11: Plan TODO — Gap-Closure Components

Implements `docs/IAP-implementation-part11-knowledge-v1.0.md`. Style follows Parts 1–10: small coherent phases, tests with each security/tenancy-sensitive change, full gate (`pytest`, `ruff check`, `ruff format`, `mypy src`) at the end of every phase. No phase starts before its stated dependencies are green. Every new table is tenant-scoped + RLS unless explicitly noted as tenant-free (mirroring the `code_issue_index` precedent from Part 6).

Conventions: "gate" = `uv run pytest tests -q -p no:cacheprovider && ruff check src tests && ruff format --check src tests && mypy src`. New workflows follow the `RunInvestigationWorkflow` activity-dispatch-loop convention (typed retry taxonomy, no bare `except Exception` swallowing business exceptions).

---

## Phase 11.0 — Shared foundations (no dependents can start before this)

- [ ] `domain/common/background_job.py`: `BackgroundJobStatus`, `BackgroundJob` (see knowledge doc §8.3).
- [ ] `ports/persistence/repositories.py` addition: `BackgroundJobRepository` (`create`, `update_progress`, `get_by_id`, `list`).
- [ ] Migration `009_background_jobs.py`: `background_jobs` table, tenant-scoped RLS, indexes on `(tenant_id, kind, status)`.
- [ ] `infrastructure/persistence/background_job_repository.py` (SQLAlchemy) + in-memory counterpart in `api/dependencies.py` (same dual-impl convention as every other repository).
- [ ] Kafka topic convention documented: `iap-jobs.{tenant_id}` (reuses existing `KafkaEventPublisher`, no new broker wiring).
- [ ] Tests: repository CRUD + tenant isolation (cross-tenant read returns empty, matching `InvestigationRepository` precedent).
- [ ] Gate.

## Phase 11.1 — Gap 7: Generic async job API (depends: 11.0)

- [ ] New router `api/v1/routers/jobs.py`: `GET /jobs/{id}`, `GET /jobs?kind=&status=`, `WS /jobs/{id}/stream` (subscribes to the tenant's Kafka job topic; falls back to a single poll-then-close if broker unavailable — never hangs the socket, mirrors `health.py`'s `NOT_REQUIRED` posture for optional infra).
- [ ] `AppContext` gains `background_job_repo` (both in-memory + SQLAlchemy paths, wired in `bootstrap/__init__.py` alongside the other Part 6 repos).
- [ ] Tests: poll returns 404 for unknown/cross-tenant id; WS smoke test with fixture broker; list filters by kind/status.
- [ ] Gate.

## Phase 11.2 — Gap 1: Structured input-required interrupt (depends: 11.0)

- [ ] `domain/investigation/models.py`: `InputRequirement`, `InvestigationStatus.AWAITING_INPUT` (additive enum member — verify every existing `match`/`if status ==` site handles unknown-value gracefully or gains an explicit branch; do not leave a silent fallthrough).
- [ ] `domain/common/exceptions.py`: `InputRequiredError(DomainException)` carrying an `InputRequirement` — non-retryable, distinct error code `INPUT_REQUIRED`.
- [ ] `application/worker/workflows.py::RunInvestigationWorkflow`: `provide_input` signal, `_require_input` helper, activity-dispatch loop catches `InputRequiredError` specifically (never bare `Exception`) and transitions to `AWAITING_INPUT` via the existing transition-repo/outbox convention.
- [ ] `api/v1/routers/investigations.py`: `POST /investigations/{id}/provide-input` (body validated against the persisted `InputRequirement.fields` keys before signaling; 422 on mismatch, 409 if not `AWAITING_INPUT`).
- [ ] `GET /investigations/{id}` response includes `pending_requirement` when status is `AWAITING_INPUT`.
- [ ] Tests: activity raises `InputRequiredError` → workflow reaches `AWAITING_INPUT` → `provide_input` with wrong keys rejected (422) → correct keys resumes → workflow completes. Cross-tenant `provide-input` call rejected before signal dispatch.
- [ ] Gate.

## Phase 11.3 — Gap 8: Outbound credential provider (independent — no dependencies)

- [ ] `ports/security/outbound_credentials.py`: `OutboundCredentialProvider` protocol.
- [ ] `infrastructure/security/oauth2_client_credentials.py`: `OAuth2ClientCredentialsProvider` (RFC 6749 grant, `asyncio.Lock`-guarded per-`provider_id` cache, proactive refresh at 80% TTL).
- [ ] `infrastructure/security/static_token_provider.py`: `StaticTokenProvider` (env-var-backed, dev/test).
- [ ] `EvidenceConfig` addition: `outbound_credentials: dict[str, OutboundCredentialSpec]` (env-loaded, `IAP_OUTBOUND_CRED_{ID}_*` convention matching existing `IAP_ORACLE_*`/`IAP_ELASTICSEARCH_*` prefixing).
- [ ] `bootstrap/__init__.py`: evidence-gateway wiring resolves declared `credential_provider_id` before constructing any MCP-based evidence adapter that declares one (additive — adapters without the field behave exactly as today).
- [ ] Tests: token cache hit avoids re-fetch; expiry triggers refresh; concurrent callers don't double-fetch (lock contention test); `StaticTokenProvider` round-trip.
- [ ] Gate.

## Phase 11.4 — Gap 9: Resilient LLM JSON extraction (independent — no dependencies)

- [ ] `infrastructure/reasoning/json_extraction.py`: `resilient_json_extract`, `JSONExtractionError` (pure functions, no I/O, ported *pattern* only — fence-strip, direct parse, outermost-object scan, brace/bracket-balance truncation repair).
- [ ] Wire into `openai_adapter.py`/`anthropic_adapter.py`: fallback path only when `response_schema is None` or when a strict-schema response still fails to parse (logged as a distinct telemetry event from primary-success, per knowledge doc §10.3 — never silently conflated).
- [ ] Tests: fence-stripped JSON, trailing-prose JSON, truncated-mid-object JSON (all three prism-agent-identified failure shapes), unrecoverable input raises `JSONExtractionError` with truncated content in the message (never the full payload, avoid log-bloat/leakage of sensitive prompt content beyond 200 chars per knowledge doc).
- [ ] Gate.

## Phase 11.5 — Gap 4: Reference-document semantic search (depends: 11.1 for reindex-as-job)

- [ ] `domain/knowledge/reference_docs.py`: `ReferenceDocumentSource`, `ReferenceDocumentFetchSpec` (git/local_path/s3 variants), `ReferenceDocumentHit`, `IndexRunSummary`, `IndexStatus`.
- [ ] `ports/knowledge/reference_docs.py`: `ReferenceDocumentPort`.
- [ ] Migration `010_reference_documents.py`: `reference_document_chunks` table (pgvector column, content-hash unique constraint per `(source_id, file_path)` for incremental reindex no-ops), tenant-scoped RLS.
- [ ] `infrastructure/knowledge/reference_docs_pgvector.py`: fetch (git via `pygit2` shallow clone / local_path / s3), markdown-aware chunker, same embedder config as Mem0 (`IAP_KNOWLEDGE_MEM0_EMBEDDER`), content-hash-keyed upsert.
- [ ] `ApplicationProfile` addition: `ReferenceDocsProfile` (sibling to `ObservabilityProfile`, optional, additive — profiles without it behave exactly as today).
- [ ] `application/evidence/gateway.py`: optional `reference_docs: ReferenceDocumentPort | None` capability, exposed through the same tool-call shape as other evidence providers.
- [ ] `mcp/tools/reference_docs.py`: `search_reference_docs(query, kinds?)` registered in `mcp/registry.py` alongside existing tools.
- [ ] `api/v1/routers/reference_docs.py`: `POST /reference-docs/{source_id}/reindex` (202, dispatches a `BackgroundJob` of kind `reference_docs_reindex`), `GET /reference-docs/{source_id}/status`.
- [ ] Tests: fetch-spec validation (bad git ref, missing token_env fails closed), chunker on real markdown fixtures, content-hash reindex skips unchanged files, cross-tenant search returns nothing, MCP tool contract test (mirrors `test_mcp_contract.py` pattern).
- [ ] Gate.

## Phase 11.6 — Gap 3: Cross-investigation finding clustering (depends: 11.1 for run-as-job)

- [ ] `domain/finding/clustering.py`: `FindingCluster`.
- [ ] `ports/persistence/repositories.py` addition: `FindingClusterRepository`.
- [ ] `ports/knowledge/ports.py` addition: `ClusterSemanticIndexPort` (`top_candidates(taxonomy, batch, k) -> list[FindingCluster]`).
- [ ] Migration `011_finding_clusters.py`: `finding_clusters` + `finding_cluster_assignments` tables, tenant-scoped RLS, pgvector column on `finding_clusters` for semantic candidate retrieval (reuses Mem0's embedder, no new embedding dependency).
- [ ] `infrastructure/persistence/finding_cluster_repository.py` (SQLAlchemy) + in-memory counterpart.
- [ ] `application/finding/clustering_service.py`: `FindingClusterService.run_incremental` (batch size configurable, default 25 matching prism-agent's proven batch size; LLM call uses `response_schema` strict structured output — F-027 — not the resilient-extraction fallback, since this is a new call site with full schema control).
- [ ] Temporal: `FindingClusteringWorkflow` (native Temporal Schedule, not APScheduler, per knowledge doc §11), `IAP_CLUSTERING_INTERVAL_HOURS` config; `POST /clusters/run` API trigger dispatches a `BackgroundJob`-wrapped one-shot execution.
- [ ] `api/v1/routers/clusters.py`: `GET /clusters`, `GET /clusters/{cluster_id}`.
- [ ] Tests: incremental run only processes unassigned findings, semantic-candidate cap (≤12 per design), new-cluster creation, fail-closed unassigned bucket on malformed LLM output (never silently dropped), tenant isolation on cluster read/list.
- [ ] Gate.

## Phase 11.7 — Gap 5: Cross-investigation aggregate reporting (depends: 11.1, 11.6)

- [ ] `domain/investigation/aggregate_report.py`: `AggregateReport`, `AggregateReportRow`.
- [ ] `ports/reasoning/report_renderer.py`: `ReportRendererPort`.
- [ ] `infrastructure/reasoning/html_aggregate_report_renderer.py`: Jinja2-based `HtmlAggregateReportRenderer` (add `jinja2` to `pyproject.toml` dependencies — check it isn't already a transitive dep of `fastapi`/other before adding explicitly).
- [ ] `application/investigation/aggregate_report_service.py`: `InvestigationAggregateReportService.build` (consumes `FindingClusterRepository` from 11.6 — no independent grouping logic, per knowledge doc §6.3).
- [ ] Artifact storage: reuse `ObjectStorageConfig`/S3-compatible client already wired in `bootstrap/__init__.py`; key convention `latest/{tenant_id}/aggregate-report.html` + `history/{tenant_id}/{run_id}/aggregate-report.html`.
- [ ] `api/v1/routers/aggregate_reports.py`: `POST /aggregate-reports/generate` (202, `BackgroundJob`-wrapped), `GET /aggregate-reports/latest`, `GET /aggregate-reports/history`, `DELETE /aggregate-reports` (tenant-scoped only — never a global wipe; explicit test for this boundary).
- [ ] Tests: report build reflects cluster taxonomy correctly, artifact round-trips through storage client, tenant-scoped delete never touches another tenant's artifacts, empty-taxonomy case renders a clear "no data yet" state rather than an empty/broken template.
- [ ] Gate.

## Phase 11.8 — Gap 2: Bulk/batch intake + fan-out (depends: 11.2's AWAITING_INPUT for per-record interrupt handling, but can proceed without it — child investigations without transcripts simply reach AWAITING_INPUT individually once 11.2 lands; note the ordering flexibility)

- [ ] `domain/investigation/batch.py`: `BatchIntakeRecord`, `BatchIntakeRequest`, `BatchIntakeResult`.
- [ ] `application/investigation/batch_parsing.py`: `parse_csv_records(content, field_mapping)` — optional convenience helper, field-name-remapping table instead of hardcoded columns (generalizes prism-agent's `transcripts_from_csv_row`).
- [ ] `application/worker/workflows.py::BulkIntakeWorkflow`: child-workflow fan-out (`workflow.start_child_workflow(RunInvestigationWorkflow.run, ...)`), `asyncio.Semaphore`-bounded parallelism (`IAP_BATCH_MAX_PARALLEL`, default mirrors prism-agent's `max_parallel_accounts=4`), `asyncio.gather` collection into completed/failed lists.
- [ ] `api/v1/routers/batch.py`: `POST /batch-intake` (202, validates `records` bounds — max 500 matching the domain model's `max_length`), `GET /batch-intake/{batch_id}`.
- [ ] Tests: fan-out dispatches N child workflows with correctly-scoped `workflow_id`s (collision-free across concurrent batches), partial-failure collection (some children fail, batch still reports completed+failed correctly), max-parallel bound respected (no more than N concurrent child starts observed), cross-tenant batch status read rejected.
- [ ] Gate.

## Phase 11.9 — Gap 6: Conversational investigation chat (depends: 11.2 for context builder parity, Part 6 knowledge retrieval)

- [ ] `domain/investigation/chat.py`: `ChatMessage`, `InvestigationChatSession`.
- [ ] `ports/reasoning/llm_gateway.py` addition: optional `stream(tenant_id, request) -> AsyncIterator[str]` method on `LLMGateway` protocol (additive — existing `complete` unaffected; adapters implement via each provider's native streaming API).
- [ ] `ports/investigation/chat_context.py`: `ChatContextBuilderPort` (default impl calls `KnowledgeRetrievalPort.retrieve_for_reasoning` when `investigation_id` is set — Part 6 reuse, no bespoke context flattening).
- [ ] Migration `012_chat_sessions.py`: `chat_sessions` + `chat_messages` tables, tenant-scoped RLS.
- [ ] `infrastructure/persistence/chat_repository.py` (SQLAlchemy) + in-memory counterpart.
- [ ] Chat response schema: structured `suggested_actions: list[ChatAction]` via `response_schema` (F-027 strict mode) — no keyword-sniffing on free text (explicit design rejection of prism-agent's `"generate report" in message.lower()` pattern, per knowledge doc §7.3).
- [ ] `api/v1/routers/chat.py`: `POST /chat/sessions`, `POST /chat/sessions/{id}/messages` (SSE via the new gateway `stream()` method), `GET /chat/sessions/{id}`.
- [ ] Tests: session persists across process restart (proves it's not an in-process dict), suggested-actions schema validated, streaming smoke test, cross-tenant session access rejected.
- [ ] Gate.

## Phase 11.10 — Follow-up hardening (optional, no new port): shared outbound MCP-connection pooling

- [ ] Review `infrastructure/evidence/runtime/elastic.py` / MCP evidence adapter for a shared-connection-cache opportunity analogous to prism-agent's `fetch_elk.py` double-checked-locking pattern (§9.4 of knowledge doc) — connection reuse across requests within a process, drop-detection + single-retry-reconnect.
- [ ] Only pursue this phase if a real perf/connection-churn problem is observed in practice — knowledge doc explicitly marks this as a "noted, not required" follow-up, not a hard gap.

## Final Gate (after all phases land)

- [ ] Full suite: `uv run pytest tests -q -p no:cacheprovider` (target: zero regressions against the Part 10 baseline of 784 passed).
- [ ] `ruff check src tests`, `ruff format --check src tests`.
- [ ] `mypy src` — zero *new* errors beyond the documented pre-existing baseline (6 errors, 4 files, per Part 9/10 precedent).
- [ ] Update `docs/IAP-implementation-part9-operations-v1.0.md` (or a new `part11-operations` doc if scope warrants) with: new env vars (`IAP_BATCH_MAX_PARALLEL`, `IAP_CLUSTERING_INTERVAL_HOURS`, `IAP_OUTBOUND_CRED_*`, reference-docs source config), new tables, new MCP tool (`search_reference_docs`), new Kafka topic convention (`iap-jobs.{tenant_id}`).
- [ ] Mark this plan DONE with a date, matching the Part 9/10 convention.
