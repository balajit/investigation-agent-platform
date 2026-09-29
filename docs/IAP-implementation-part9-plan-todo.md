# IAP Implementation Part 9: Plan TODO (Elastic Hardening + MCP Investigation Service)

Parent knowledge: `docs/IAP-implementation-part9-knowledge-v1.0.md`. Design inputs in `docs/scratchpad/` (remediation prompt, target architecture, coding-agent prompt). Check off items as they land; do not start a phase before its dependencies are green.

## Phase 1 — Config + domain contracts (blocks everything) — DONE 2026-09-28

- [x] `infrastructure/configuration/config.py`: extend `EvidenceConfig` — `cursor_signing_key: SecretStr` (`IAP_ELASTIC_CURSOR_SIGNING_KEY`, fail-closed in production, explicit test key otherwise), `cursor_ttl_seconds`, `elastic_query_timeout`, provider ceilings with immutable security maxima; thread through `_evidence_config_from_env()` and both `load_application_config_*` loaders.
- [x] `domain/evidence/models.py`: `observed_at: datetime | None` on `Evidence` + `EvidenceFreshness`; `EvidencePage.next_cursor` 256 → 4096. New `LogSeverity` closed enum.
- [x] `domain/evidence/requests.py`: `limit le=200`, keywords max 20 × 200 chars, services max 10, strict severity enum, identifier key allowlist + value length cap, environment pattern, `cursor max_length=4096`. Canonical `ALLOWED_IDENTIFIER_KEYS`; `elastic.py` imports it (single source of truth).
- [x] `domain/common/exceptions.py`: add `InvalidCursorException` (`INVALID_CURSOR`, 400, non-retryable) + provider timeout/unavailable variants.
- [x] New `domain/evidence/completeness.py`: `EvidenceCompleteness(complete, truncated, reason, returned_count, examined_count)`.
- [x] Alembic migration `008_evidence_observed_at_nullable` + `EvidenceORM.observed_at` nullable.
- [x] Persist path confirmed: `application/worker/activities.py` saves result batches with `investigation_id` via `save_batch` — Q2 round-trip has its write path; Phase 5 service must do the same. Worker call sites clamped to the new domain bounds (limit/lists) to preserve provider-`min()` behavior.
- [x] Tests: `tests/unit/test_part9_phase1.py` (19 tests); existing config tests gained the test signing key; migration-chain tests extended to 008. Gate: 542 passed, ruff clean, mypy zero new errors (6 pre-existing at HEAD, verified identical via worktree).

## Phase 2 — Elastic security boundary (needs Phase 1) — DONE 2026-09-28

Phase 2 implementation notes (see knowledge doc §Facts for the threat model):

- Cursor codec: injected key (`ElasticAdapterSettings`, fail-closed construction), versioned payload (`v/tenant/investigation/query_fingerprint/sort/issued/expires`), TTL expiry, legacy unversioned tokens rejected; invalid cursors raise `InvalidCursorException` (never page-one fallback); narrowed exception handling; token never logged.
- `NormalizedRuntimeQuery` + SHA-256 fingerprint binds cursors and populates `normalized_query_hash` (SEARCH and GET paths); order-insensitive canonicalization; `SORT_VERSION` part of the fingerprint.
- `build_index_pattern()` validates tenant + environment (reject, never strip); shared by search and fetch; fetch additionally takes explicit `environment` (over-broad `logs-{tenant}-*-*` gone).
- `get_runtime_evidence(tenant_id, investigation_id, evidence_id, environment, mapping_lookup, ...)`: mapping resolution + ownership check + tenant-prefix guard + exact-index re-fetch; real investigation ID in provenance (synthetic `uuid5` fetch context gone); unknown/cross-scope/non-Elastic → `EVIDENCE_NOT_FOUND` without disclosure; corrupt refs → non-retryable `ExecutionError`. New `EvidenceMappingLookup` port (`ports/evidence/mapping.py`); protocol extended; zero production callers existed so no migration needed.
- Provider-boundary enforcement independent of domain (counts, key exact-match, value lengths, window vs configured ceilings; size via `min()`); severities pass through as validated enum values (regex pre-filter removed); `multi_match` operator pinned to explicit `OR` as the recorded contract.
- Bootstrap wires `ElasticAdapterSettings.from_evidence_config`; tests: `tests/unit/test_part9_phase2.py` (41 tests) + reworked `TestElasticGetParity`. Gate: 584 passed, ruff clean, mypy zero new errors.
- Deferred to Phase 3 per plan: timestamp/projection/classification mapping fixes, timeout trio, `has_more`/`total_count` contract, file split.

Original checklist (all satisfied):



- [ ] Cursor: injected key, versioned payload (`v, tenant, investigation, query_fingerprint, sort, issued/expires_at`); unversioned legacy tokens → `INVALID_CURSOR`.
- [ ] Canonical `NormalizedRuntimeQuery` + SHA-256 fingerprint bound into cursor; invalid/tampered/mismatched cursor raises (never page-one fallback); narrowed exception handling; fingerprint-only logging.
- [ ] Single `build_index_pattern()` (tenant + environment validated) used by search and fetch; fetch takes explicit environment (remove `logs-{tenant}-*-*`).
- [ ] `get_runtime_evidence(tenant_id, investigation_id, evidence_id, environment)`: mapping lookup via `get_by_id` + investigation-ownership check + scoped provider re-fetch; propagate `correlation_id`.
- [ ] Reject (don't silently truncate/filter): oversized keyword/service lists, invalid severities, non-allowlisted identifier keys.
- [ ] Tests: cursor round-trip/rotation/tamper/cross-query replay, tenant/environment injection (`*`, `/`, quotes, control chars), invalid-cursor structured failure, fetch round-trip + cross-investigation `EVIDENCE_NOT_FOUND`.

## Phase 3 — Evidence correctness (needs Phase 2) — DONE 2026-09-28

Implementation notes: tz-aware timestamps required (missing/invalid/naive → `observed_at=None` + `EvidenceDataQuality` flag; new `Evidence.data_quality` field, no migration). Projection is layer 1 of a documented 3-layer chain (projector structure → gateway value redaction → MCP field selection): 18-field allowlist, secret key-name redaction with manifest (correlation IDs excluded), string/depth/key/list caps + 16KB scalar-core fallback, normalized summary/snippet, no `tenant_id` injection, documented INTERNAL default with source-metadata override, index-qualified `SourceLocation`, `fingerprint` as document identity. Resilience: envelope validation, per-hit skip with explicit `partial_result` + error entries, typed provider errors (safe messages), `CancelledError` propagation, timeout trio enforced at construction. Split into `cursor.py`/`query.py`/`validation.py`/`projection.py` + thin `elastic.py` (compat re-exports kept). `total_count`/`has_more` documented as bounded/best-effort (PIT deferred). Tests: `tests/unit/test_part9_phase3.py` (30 tests). Gate: 614 passed, ruff clean, mypy zero new errors.

- [x] Timestamps: timezone-aware required; missing/invalid → `observed_at=None` + data-quality signal; `retrieved_at=now` always.
- [x] Provenance: `normalized_query_hash` = canonical query fingerprint; `index+_id` stays in `SourceLocation`/provider fields; separate content fingerprint only if needed.
- [x] Projection: allowlisted, redacted, size-bounded attributes (no `{**src}`); bounded pre-serialization snippet (no `json.dumps(full_src)[:4000]`); classification policy documented (no silent `INTERNAL`).
- [x] Resilience: response-shape validation, typed provider errors (cancellation propagates, never `ExecutionError`), coherent timeout trio, explicit `has_more`/`total_count` contract.
- [x] Split `elastic.py` → `cursor.py`, `query.py`, `validation.py`, `projection.py` (thin adapter remains).
- [x] Tests: timestamp matrix (`Z`/offsets/naive/malformed), fingerprint stability, redaction fixtures, malformed-hit handling, timeout/cancellation, generated-ES-body assertions (index scope, allowed clauses only, `search_after` only from valid cursors).

## Phase 4 — Trace extraction (needs Phases 2–3) — DONE 2026-09-28

Implementation notes: new domain `domain/evidence/telemetry.py` (`CodeLocation`, `TableReference` with `schema` alias, `SqlStatement`, `TraceTelemetry` with counts + `EvidenceCompleteness`) and `TraceTelemetryRequest` (pattern-validated trace ID, budget 1–100). `SqlEvidenceExtractor`: whole-field `sqlglot.parse` first, heuristic `;`-split fallback with PARTIAL status, FAILED chunks carry size placeholders (raw secrets never leak), AST literal redaction, 20K bound + truncation flags, qualification/case preserved, never executes. `TraceTelemetryExtractor`: budget-bound retrieval via the adapter, deduped/sorted locations and tables with sorted `evidence_ids`, validated lines (invalid → None), over-long values degrade with malformed flags, malformed containers counted (not hidden), per-field SQL stats, completeness (`application_limit` vs `provider_limit` vs `metadata_incomplete`). `elastic_bridge.py` is a thin facade returning the new contract (zero repo callers existed). Deterministic ordering throughout. Tests: `tests/unit/test_part9_phase4.py` (22 tests). Gate: 636 passed, ruff clean, mypy zero new errors.

- [x] New `logs/trace_telemetry.py`: `TraceTelemetryExtractor` + `SqlEvidenceExtractor`; bounded retrieval with explicit `Completeness` (replaces silent `limit=100`); keep `elastic_bridge.py` as thin shim.
- [x] Defensive ECS traversal; deduped/sorted code locations with `evidence_ids`; validated line numbers; bounded field lengths.
- [x] Multi-statement dialect-aware SQL parsing with per-statement `parse_status`; literal redaction; length bound + truncation flag; qualified `TableReference(catalog, schema, table)`; deterministic ordering; parse-failure counters.
- [x] Tests: empty/1/100/>100 events, malformed ECS, duplicate locations, multi-statement + dialect SQL, qualified/quoted tables, truncation flags, secret-bearing literals.

## Phase 5 — Application services (needs Phases 1–4) — DONE 2026-09-28

Implementation notes: trusted scope is `InvestigationScope` (renamed from the blueprint's `InvestigationContext` to avoid the established domain model of that name), built only from verified identity fields; `require_investigation` fails closed on unknown/cross-tenant scope. `RuntimeEvidenceService` merges context, resolves app/profile, delegates to the gateway, persists hits for the Q2 round-trip, and normalizes completeness (`pagination_available`/`metadata_incomplete`). `EvidenceDetailService` verifies mapping ownership with zero provider I/O before it, then re-fetches fresh via the provider port + mapping adapter and re-sanitizes (unknown/cross-scope → `EVIDENCE_NOT_FOUND`, no disclosure). `TraceInvestigationService` enforces the profile evidence budget and delegates through the new `TraceTelemetryPort` (extractor retyped to the runtime provider port + `correlation_id`; factory-injected so application imports no infrastructure). Composition via `build_investigation_services`, wired in bootstrap (`ctx.investigation_services`). Gateway-path enablers fixed (load-bearing latent defects): selector default-provider fallback routing, adapter `profile` attribute (documented §61 non-enforcement), protocol `correlation_id`. Gateway→provider correlation plumbing deferred to Phase 6. Tests: `tests/unit/test_part9_phase5.py` (18 tests incl. AST import-hygiene check). Gate: 654 passed, ruff clean, mypy zero new errors.

- [x] `application/investigation/runtime_evidence.py` (`RuntimeEvidenceService.search`), `evidence_detail.py` (`EvidenceDetailService.get`), `trace_investigation.py` (`TraceInvestigationService.investigate_trace`); frozen `InvestigationScope` from verified identity fields; no MCP imports.
- [x] `InvestigationServices` composition root wired in bootstrap (`ctx.investigation_services`).
- [x] Tests: trusted-context merge, ownership enforcement, completeness normalization, delegation (no direct ES access).

## Phase 6 — MCP server (needs Phase 5) — DONE 2026-09-28

Implementation notes: new top-level `mcp/` package (no clash with the outbound `infrastructure/evidence/mcp.py` client). Schemas are SDK-derived from annotated signatures (single source, no drift) with strict output models (all fields required per blueprint). Handlers are thin `transport context → core → service → projection` closures over `InvestigationServices`; union returns yield the SDK's `{"result": {...}}` wire wrapper (documented deviation, contract-tested). Auth: pure-ASGI middleware (JWT via shared `resolve_identity_from_headers` + validated investigation header; 401/400/403 before dispatch) for HTTP; env-bound single scope for stdio. Strict-arguments middleware rejects unknown properties at protocol tier (`-32602`); semantic failures return the `{"error": {...}}` envelope (9-code taxonomy). `McpConfig` added (`IAP_MCP_*`). Tests: unit (18), contract (catalog/schemas + 8 adversarial), integration (9 full-stack + 4 HTTP incl. handshake). Gate: 702+ passed, ruff clean, mypy zero new errors. Residuals: production HTTP deployment wiring (mount helper + `main()` provided), CORS allowlist for `X-Investigation-ID`, `SEARCH_LOGS` profile capability, gateway search correlation plumbing, stdio single-investigation scope.

Original checklist (all satisfied):

- [x] `src/investigation_agent_platform/mcp/`: `server.py` (stdio via `run_stdio_async` + Streamable HTTP app + mount helper), `registry.py`, `context.py`, `errors.py`, `schemas/`, `tools/` — thin `transport context → service → projection` handlers.
- [x] Exactly 3 tools (`search_runtime_evidence`, `get_evidence`, `investigate_trace`) with SDK-derived 2020-12 schemas matching the blueprint property contracts, descriptions, and `readOnly/idempotent` annotations; error map `{INVALID_REQUEST, INVALID_CURSOR, CURSOR_EXPIRED, FORBIDDEN, EVIDENCE_NOT_FOUND, PROVIDER_TIMEOUT, PROVIDER_UNAVAILABLE, INCOMPLETE_EVIDENCE, INTERNAL_ERROR}`; OTel + structured logging without raw payloads.
- [x] Tests: schema validation (valid + rejection cases), trusted-context propagation, error mapping, serialization.

## Phase 7 — Contract, adversarial, integration + docs (final gate)

Test layers below landed during Phase 6 (flat-file layout per repo convention: no `__init__.py` anywhere):

- [x] `tests/contract/test_mcp_contract.py`: tool catalog (exact 3 names, no admin tools), 2020-12 schema validity, error shape, pagination fields.
- [x] `tests/contract/test_mcp_adversarial.py`: tenant/investigation swap, cross-query cursor replay, `*`/DSL/script/aggregation injection, oversized inputs, secret-bearing SQL, raw-ES-`_id`-as-`evidence_id` probing (malformed provider hits covered in `test_part9_phase3.py`).
- [x] `tests/integration/test_mcp_investigation.py` + `test_mcp_http_transport.py`: tool → service → gateway → mocked adapter; generated-ES-body assertions live in `test_part9_phase2.py`.
- [x] Docs: `docs/IAP-implementation-part9-operations-v1.0.md` covers index naming, ECS contract, isolation, cursor versioning + invalidation notice, evidence-ID lifecycle, pagination consistency, completeness reasons, classification, config vars, stdio + HTTP run instructions.
- [x] Gate: `ruff` clean, `mypy --strict` zero new errors (6 pre-existing at HEAD), full `pytest` green (712 passed, 9 env-skipped), no production secrets committed, deploy notes call out cursor invalidation + unmapped-evidence `EVIDENCE_NOT_FOUND` behavior.

## Done when — MET 2026-09-28

Every box above is checked. Remediation §99 and target-arch §53 hold modulo explicitly recorded items: PIT snapshot pagination (deferred on cost, documented), gateway search `correlation_id` plumbing (detail/trace paths carry it), profile-driven provider scoping (accepted-for-compatibility, documented), production HTTP deployment wiring (mount helper + `main()` provided; TLS/CORS are deploy-time concerns). Final report: this file (per-phase implementation notes), the knowledge doc (decisions + Phase 6 record), the operations doc (operator contract + residuals). Test evidence: 712 passed / 9 env-skipped; `ruff check` clean; `mypy` zero new errors. No unrelated architectural rewrites; pre-existing working-tree changes untouched.
