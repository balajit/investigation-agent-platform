# IAP Implementation Part 10: Plan TODO — Dynamic Field Mapping

Implements `docs/IAP-implementation-part10-dynamic-mapping-design-v1.0.md` (§7 review amendments are binding). Style follows Parts 1–9: small coherent phases, tests with each security-sensitive change, full gate at the end. No phase starts before its dependencies are green.

Conventions: `mapping` = a resolved `ObservabilitySourceMapping`; `mapping_source_id` = its string key (`None` = built-in generic-ECS, i.e. today's behavior exactly).

## Phase 10.0 — Real-data fixtures (no source changes) — DONE 2026-09-29

- [ ] Add `tests/fixtures/elastic_mappings/` with one JSON file per sampled stream parsed from `docs/scratchpad/elastic_info_sample.txt`: `gen_verbose_prod.json`, `fault_alerts.json`, `gen_root_cause_prod.json`, `ui_buyflow_err_grp.json`, `buyflow_order_details.json` (mapping `properties` objects only, keyed by index name in a small loader helper).
- [ ] Verify by parsing: script asserts each fixture loads, each has ≥1 top-level property, and the five files' top-level key sets differ (guards against copy-paste fixture rot).
- [ ] Tests: fixture loader test only. Gate: `pytest tests/unit/test_part10_phase0.py`, `ruff`.

## Phase 10.1 — Domain models (additive only; tree stays green) — DONE 2026-09-29

Notes: `SortSpec`/`sort`/`legacy_index_synthesis` included now (needed by 10.9; additive, unused until then). Generic-ECS snapshot verified field-for-field against current literals.

- [ ] New `src/investigation_agent_platform/domain/observability/mapping.py`: `FieldValueType`, `FieldMapping`, `SeverityStrategy`, `SeverityDerivation`, `TenantScopeStrategy`, `TenantScope`, `ObservabilitySourceMapping`, `CodeLocationMapping`, plus `generic_ecs_mapping() -> ObservabilitySourceMapping` reproducing today's literals exactly (`@timestamp`, `message`/`error.message`, `service.name`, `log.level`, `labels.{trace_id,span_id,service_name,user_id,session_id,container_id}`, `code.*`, `db.statement`, ECS allowlist, `severity.strategy = FIELD` on `log.level`, `tenant_scope = INDEX_PATTERN`).
- [ ] `ObservabilityProfile`: add `mapping_source_id: str = "generic-ecs"` (default preserves all existing YAML/tests). Leave the existing singular `*Field` mappings untouched (§7 amendment 8.6).
- [ ] `TraceTelemetry`: add `code_location_supported: bool = True`, `sql_supported: bool = True` (defaults preserve current behavior; gen mappings will resolve them to `False`).
- [ ] `RuntimeEvidenceRequest` + `TraceTelemetryRequest`: add internal `mapping_source_id: str | None = None` (documented internal: MCP handlers build requests field-by-field and never set it from agent input). Do NOT remove the static identifier validator yet (that moves in 10.4 together with its replacement).
- [ ] Tests: model validation (bad strategy combos reject, e.g. `ERROR_FLAG` without `error_field`; `generic_ecs_mapping()` snapshot test pinning today's literals so drift is deliberate).

## Phase 10.2 — Config, registry, shipped YAMLs — DONE 2026-09-29

Notes: mapping models hardened with `extra="forbid"` (YAML typos fail closed); `legacy_index_synthesis: true` rejected in file-loaded docs (builtin-only); shipped `gen-verbose.yaml` + `gen-root-cause.yaml` validated against real fixtures.

- [ ] New `infrastructure/configuration/mapping_registry.py`: `load_mapping_profiles(dir) -> MappingProfileRegistry` (pydantic-validated, fail-closed on: malformed entries, duplicate `source_id`, exact-duplicate `index_patterns` across profiles with a comment noting glob-subsumption as a known limitation). Registry API: `resolve(source_id: str | None) -> ObservabilitySourceMapping` (`None` → built-in generic-ECS; unknown id → `PlatformConfigurationError`).
- [ ] New `config/observability-mappings/`: `gen-verbose.yaml`, `gen-root-cause.yaml` per design §3.3 (both `tenant_scope.strategy = none_required`, explicit). `fault-alerts`/`buyflow` families deferred per design §7 — note as follow-up, not this phase.
- [ ] `EvidenceConfig`: add `mapping_profiles_path: str = "config/observability-mappings"` (env-overridable `IAP_MAPPING_PROFILES_PATH`, same precedent as `oracle_mcp_config`).
- [ ] Existing `config/profiles/*.yaml` left untouched (they inherit `mapping_source_id: generic-ecs` default — no existing profile breaks).
- [ ] Tests: registry tests (malformed/duplicate/overlap rejection, unknown-id fail-closed, `None` → generic-ECS, gen YAMLs load and validate).

## Phase 10.3 — FieldResolver (pure, independently testable) — DONE 2026-09-29

- [ ] New `infrastructure/evidence/runtime/field_resolver.py`: `resolve_value` (first-present-candidate, dotted-path traversal reusing Phase-4 defensive guards; hierarchical-vs-flat ambiguity resolved nested-first, matching `trace_hop.py` precedent), `resolve_timestamp` (dispatch on `FieldValueType`: ISO parse as today; epoch-millis/seconds via `fromtimestamp(tz=UTC)`; never fabricates `now()`; document the millis-vs-seconds config-authoring risk for `long`-typed fields), `resolve_severity` (`FIELD` → enum lookup, `ERROR_FLAG` → boolean mapping, `UNSUPPORTED` → `None`), `resolve_identifier_field` (physical field name, keyword-subfield-qualified when the candidate's `value_type` is `text`).
- [ ] Tests: candidate fallback order, all timestamp variants incl. the `root_cause` epoch-millis case built from the real fixture, both severity strategies, dotted-vs-flat shapes.

## Phase 10.4 — Query path (the security-critical phase) — DONE 2026-09-29

Notes: `_source.includes` shipped here with the body builder (exact allowlist = wire-minimal); tenant FIELD clause enforced; ERROR_FLAG predicate per §7 amendment 8.2 with honest match-none; static validator + constant removed atomically with boundary replacement; adversarial identifier test relocated to integration (INVALID_REQUEST + zero ES I/O verified).

- [ ] `query.py::normalize_request` gains a `mapping` parameter; `build_query_body(normalized, search_after, query_timeout, mapping)` resolves every physical field name through it: identifiers via `resolve_identifier_field`, services via `mapping.service`, keywords via `mapping.message` multi_match fields, time range via mapping timestamp field. `SeverityStrategy.UNSUPPORTED` + caller-supplied severities → `DomainValidationException` (keeps `INVALID_REQUEST`, §7 amendment 8.3). `ERROR_FLAG` predicate per §7 amendment 8.2 (include `error:true` iff `true_severity ∈ S`, `error:false` iff `false_severity ∈ S`, else `match_none`). Keyword filter with `mapping.message = None` → `DomainValidationException`; service filter with `mapping.service = None` → same. Tenant `FIELD` strategy appends the mandatory, non-overridable term clause (§3.6). Fingerprint canonical form gains `mapping_source_id`.
- [ ] `validation.py`: `build_index_pattern` replaced by `resolve_index_pattern(mapping)` over `mapping.index_patterns` (comma-joined, each charset-validated); `validate_identifier_items` checks keys against `mapping.identifiers` and raises `DomainValidationException` on mismatch (contract preservation, §7 amendment 8.3). Tenant/environment shape validation unchanged (`SecurityPolicyViolationException`, still enforced — needed today for charset safety and tomorrow for `FIELD` strategy).
- [ ] `domain/evidence/requests.py`: remove the static identifier-key validator and `ALLOWED_IDENTIFIER_KEYS`; update `elastic.py`'s alias import and its test. (Do both removals in this phase together with the boundary replacement — never land one without the other.)
- [ ] `elastic.py`: constructor gains optional `mapping_registry` (default `None` = generic-ECS-only; keeps all existing adapter tests passing); per-request resolve flow (`request.mapping_source_id` → registry → mapping object threaded into normalize/query/project); unknown id fails closed before any provider I/O.
- [ ] Tests: boundary tests relocated from Phase 1 (generic-ECS mapping, `DomainValidationException` codes), per-mapping query-body assertions against real-fixture-shaped documents, tenant `FIELD` clause injection test with a synthetic mapping (ahead of need, per design §7.3), fingerprint sensitivity to `mapping_source_id`, cursor from mapping A rejected under mapping B.

## Phase 10.5 — Projection path — DONE 2026-09-29

Notes: mapping-driven allowlist/timestamp/classification/message/service; snippet severity keeps raw-string fallback for unrecognized levels (generic-ECS fidelity); core fallback unified to short-scalar path rebuilding (preserves nested service/log/labels shapes); empty-string timestamps stay INVALID (never fall through); `_source.includes` shipped with the body builder (exact allowlist = wire-minimal).

- [ ] `projection.py`: `ALLOWED_TOP_LEVEL_FIELDS` → `mapping.top_level_allowlist`; `_parse_observed` dispatches on mapping timestamp (`FieldValueType`); `_derive_classification` reads `mapping.classification` (null → documented `INTERNAL` default, as today); message/service/severity/snippet builders read resolved fields (`_nested_str` paths for `service.name`/`log.level`/`labels.trace_id` become resolver calls; dotted-fold namespaces stay mechanical and unchanged).
- [ ] Projector methods gain the `mapping` parameter (same threading pattern as 10.4; single helper `project_with_mapping` keeps call sites small).
- [ ] Tests: per-mapping allowlist enforcement using real sampled documents (prove `gen-verbose` allowlist admits a real verbose document fully and nothing else), epoch-millis timestamps map to correct `observed_at`, `gen-root-cause` documents (no `@timestamp`) yield `TIMESTAMP_MISSING` not `TIMESTAMP_INVALID`.

## Phase 10.6 — Trace extraction, bridge, MCP propagation — DONE 2026-09-29

Notes: mapped extraction reproduces ECS behavior under generic-ECS (incl. `log.origin.*` container fallback via extended candidates); empty-string SQL stays absent (not malformed); corrupt shapes flag even when siblings resolve; legacy `_extract_code_fields` kept as thin delegation shim; builtin id resolves registry-free everywhere (reserved against shadowing); MCP output carries both support flags (contract updated).

- [ ] `trace_telemetry.py`: `_extract_code_fields`/`_extract_sql_field` gated on `mapping.code_location`/`mapping.sql_statement` non-null (skip explicitly when null); populate the new `code_location_supported`/`sql_supported` flags; `extract_from_items` gains `mapping` (same threading; `extract` resolves it from the request exactly like the adapter path). `TraceTelemetryExtractor` constructor gains the same optional registry pattern as the adapter (bridge passes it through).
- [ ] `elastic_bridge.py`: stamp `mapping_source_id` from its `ObservabilityProfile` parameter onto the built `TraceTelemetryRequest` (no signature change beyond what already exists).
- [ ] `mcp/schemas/trace.py`: propagate the two support flags to the agent-facing output; update MCP contract tests (exact-shape assertions must list the new fields — deliberate schema evolution, not silent).
- [ ] Tests: `code_location_supported=False`/`sql_supported=False` for gen-shaped fixtures; flags distinguishable from empty-but-supported; bridge stamps mapping id (assert via request capture).

## Phase 10.7 — Services, bootstrap, environment consistency — DONE 2026-09-29

Notes: env-mismatch rejects before provider I/O in both search paths; mapping id stamped from profile; derivation receives mapping id; bootstrap builds registry fail-closed with source-id + NONE_REQUIRED logging; builtin id needs no registry anywhere.

- [ ] `RuntimeEvidenceService.search` + `TraceInvestigationService.investigate_trace`: after loading the investigation's profile, reject `request.environment != profile.environment` with `DomainValidationException` (design amendment 8.4 — environment now selects the mapping, so mismatch is agent error, caught before provider I/O); stamp `request.mapping_source_id` from `profile.observability_configuration.mapping_source_id` via `model_copy`.
- [ ] `EvidenceDetailService.get`: verify current environment sourcing (already resolves `profile.environment` for the fetch — keep; no agent input involved, no change expected beyond verification).
- [ ] Bootstrap: build `MappingProfileRegistry` from `EvidenceConfig.mapping_profiles_path` inside `_wire_evidence_gateway` (fail-closed aborts boot, F-001 precedent); pass registry into adapter and extractor constructors; log each loaded `source_id` plus distinct `NONE_REQUIRED` notices (§3.6).
- [ ] `build_investigation_services`: no signature change (mapping flows via requests, derivation port unchanged apart from the `mapping` pass-through both sides already accept).
- [ ] Tests: env-mismatch rejection (service-level, gateway uncalled), stamping assertions (mapping id reaches the adapter), bootstrap wiring test with a temp mappings dir, `NONE_REQUIRED` startup log assertion.

## Phase 10.9 — Sort contract + cursor arity + fail-loud — DONE 2026-09-29

Notes: sort arity is structurally constant 2 (mapping sort + `_id` tiebreak) across all mappings; encode/decode enforce exact arity (`ValueError` server-side, `InvalidCursorException` agent-side); `has_more` without issuable sort fails loud (`ExecutionError`, non-retryable) per confirmed judgement call; fingerprint already bound mapping+sort from 10.4 (cross-mapping rejection tested); root_cause epoch sort emits no date format assumptions.

## Phase 10.10 — Get-path mapping scoping — DONE 2026-09-29

Notes: port gains optional `mapping_source_id` (backward-compatible); stored index validated against mapping patterns via fnmatch (legacy keeps prefix check); fetch fingerprint binds mapping id; detail service threads profile mapping id; legacy-omitted path verified intact.

- [ ] `ports/evidence/runtime.py`: `get_runtime_evidence` gains optional `mapping_source_id: str | None = None` (default preserves current callers; protocol backward-compatible).
- [ ] `elastic.py` get path: resolve mapping (generic-ECS default when omitted); replace the `logs-{tenant}-` prefix check with `fnmatch` validation of the stored index against `mapping.index_patterns` (legacy-synthesis mappings keep the prefix check); bind `mapping_source_id` in the fetch fingerprint.
- [ ] `EvidenceDetailService.get`: thread `mapping_source_id` from the already-loaded profile into the provider call.
- [ ] Tests: stored `logz-gen-*` row resolves under its mapping; cross-pattern stored index → `EvidenceNotFoundException` with zero provider I/O; omitted param → legacy path intact; fingerprint differs across mappings for the same doc.

## Phase 10.11 — Wire efficiency + downstream consumers — DONE 2026-09-29

Notes: `_source.includes` shipped in 10.4; trace_hop audit found exactly one real consumer (others generic-safe); hop resolution mapping-aware via optional `TraceHopMapping` section (constructor-level, port untouched); stamping flows through the same `mapping_source_id` channel; fail-None contract preserved; consumer-audit test pins the reader inventory.

## Phase 10.12 — Dead-contract removal, fingerprint docs, ops-doc updates, full gate — DONE 2026-09-29

- [x] `query_string`: inventoried (live consumer: outbound `McpEvidenceAdapter`) → deprecated explicitly as reserved (accepted, sanitized, never executed by our provider), with contract test.
- [x] Fingerprint documentation: cross-mapping fingerprint test (10.9) + doc-identity comment at the projection fingerprint site.
- [x] Contract additions: vocabulary mechanism test + trace support flags in MCP output shape (contract updated deliberately).
- [x] Docs: ops guide updated (index-naming reality, mapping config reference incl. `IAP_MAPPING_PROFILES_PATH`, Part 10 cursor-invalidation deploy note, sort/arity rules, `query_string` reserved status).
- [x] Gate: `ruff check` clean, `ruff format` clean on touched files, `mypy src` zero new errors (6 pre-existing), full `pytest tests` green (784 passed, 9 env-skipped), no production secrets, no live-cluster access in CI (fixtures only).

## Explicitly out of scope

- `fault-alerts` / `buyflow` mapping YAMLs (deferred; registry + generic fallback cover their absence).
- Consolidating the legacy singular `ObservabilityProfile.*Field` mappings (§7 amendment 8.6).
- Glob-subsumption detection in overlap checks (exact-duplicate rejection only; documented limitation).
- Point-in-time pagination, gateway `correlation_id` plumbing, and all other Part 9 residuals — unchanged by this work.
