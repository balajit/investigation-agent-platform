# IAP Implementation Part 9: Operations Guide v1.0

Operator and deployment documentation for the hardened Elastic evidence layer and the MCP investigation service. Design background: `docs/IAP-implementation-part9-knowledge-v1.0.md`. Execution record: `docs/IAP-implementation-part9-plan-todo.md`. Verified against code 2026-09-28.

## 1. Index naming and scope

Part 10: index scope comes from the request's resolved `ObservabilitySourceMapping` (`config/observability-mappings/*.yaml`). Legacy generic-ECS sources synthesize `logs-{tenant}-*-{environment}-*` (both `tenant` and `environment` validated before interpolation: `^[A-Za-z0-9][A-Za-z0-9._-]*$`, lengths 128/64; anything else fails — values are never stripped and continued). Real clusters use their own conventions (e.g. `logz-gen-verbose-prod-es-*`); those patterns are operator-authored per mapping, charset-validated, comma-joined at query time. Search and direct fetch resolve the same mapping, so both carry identical scope — the fetch additionally validates the stored index against the mapping's patterns (`fnmatch`; legacy keeps the tenant-prefix check). There is no arbitrary index selection anywhere in the request path.

## 2. ECS and provider field contract

Part 10: the consumed field set is per-mapping (`mapping.top_level_allowlist`); the ECS list below is the generic-ECS baseline only. Real sources declare their own fields (e.g. `appName`, `traceId`, `error` boolean, epoch-millis timestamps), an explicit severity derivation (field, error-flag, or unsupported-with-rejection), and identifier vocabularies. Request bodies carry `_source.includes` scoped to the allowlist, so unlisted fields never cross the wire. Required for identity: Elasticsearch `_id` and `_index`; records missing either are skipped with an explicit partial-result signal, never silently and never page-failing.

Generic-ECS baseline fields: `@timestamp`, `message`, `error.message`, `service.name`, `log.level`, `log.origin.*`, `code.*` (function/filepath/lineno/file), `db.statement`, `db.dialect`/`db.system`, `labels.*` (allowlisted keys only: `trace_id`, `span_id`, `service_name`, `user_id`, `session_id`, `container_id`), `trace`/`span`/`transaction`/`parent` correlation objects, plus `event`, `host`, `container`, `process`, `tags`, `user`. Everything else never enters the domain object. Flat dotted variants of these namespaces are folded into nested shape.

## 3. Isolation model

Tenant identity comes from verified JWT (`api/tenant.py`; `X-Tenant-ID` is dev-only convenience, refused in production). Investigation scope is verified per call against the investigation store — unknown or cross-tenant investigations fail closed (`FORBIDDEN` for broken context, `EVIDENCE_NOT_FOUND` with no disclosure for record misses). Mappings with `tenant_scope.strategy = field` force-append a non-overridable tenant term to every query; `none_required` sources log distinctly at startup and must be an explicit reviewed choice. Pagination cursors bind tenant, investigation, mapping, sort spec, and the exact normalized query fingerprint; reuse across queries, tenants, investigations, or mappings is rejected. Direct evidence fetch resolves platform IDs through the persisted identity mapping only; raw Elasticsearch IDs are never accepted as input.

## 4. Cursor versioning and invalidation notice

Cursors are versioned (`v: 1`), HMAC-signed with `IAP_ELASTIC_CURSOR_SIGNING_KEY`, and expire after `IAP_ELASTIC_CURSOR_TTL_SECONDS` (default 3600). **Deploying Part 9 invalidates all pre-existing cursors**: unversioned legacy tokens are rejected as `INVALID_CURSOR`, never reinterpreted. Agents must restart pagination without a cursor after the deploy. Future format changes bump the version and reject older tokens the same way. **Deploying Part 10 likewise invalidates outstanding cursors**: the fingerprint now binds `mapping_source_id` and the resolved sort spec, so pre-Part-10 tokens fail the query-binding check — same fail-closed behavior, same agent recovery (restart without a cursor).

## 5. Evidence-ID lifecycle

Evidence IDs are platform UUIDs minted when an Elasticsearch hit qualifies as evidence (`uuid5("elastic:{index}:{_id}")`) and persisted with the full record (`evidence_key`, unique per tenant). `search → evidence_id → get_evidence` round-trips through that mapping. Records predating the mapping (or from another provider) correctly return `EVIDENCE_NOT_FOUND`. Observation time unknown is stored as `observed_at = NULL` (migration `008`) with a data-quality flag — never fabricated.

## 6. Pagination consistency

Pagination uses `search_after` over the mapping's sort (mapping sort field + `_id` tiebreak — always exactly two keys; any other arity fails the page loudly rather than looping or truncating silently) with **best-effort consistency over a changing dataset**: arrivals or deletions between pages can repeat or skip records. No point-in-time snapshot is used (evaluated, deferred on operational cost). `has_more` means another page exists; it does not imply truncation by error.

## 7. Completeness vocabulary

Every bounded result carries `{complete, truncated, reason, returned_count, examined_count}`. Reasons: `pagination_available` (more pages, not an error), `provider_limit` / `application_limit` (a bound truncated the view), `metadata_incomplete` (malformed records skipped), `provider_failure`, `input_bound`. `complete: true` always pairs with `truncated: false` and `reason: null`. An incomplete trace must never be read as "nothing happened".

## 8. Classification

Provider-wide default is `INTERNAL` (platform convention, shared with Oracle/Git/MCP adapters). An explicit recognized level in `labels.data_classification`, `labels.classification`, or `event.classification` overrides per record; unknown values keep the default. Secret-bearing key names are redacted with a manifest entry (correlation IDs such as `session_id` are deliberately excluded); secret-shaped values are scrubbed by the gateway sanitizer. Raw SQL is literal-redacted; unparseable statements yield size placeholders, never raw text.

## 9. Configuration reference (Part 9 additions)

| Variable | Default | Notes |
| --- | --- | --- |
| `IAP_ELASTIC_CURSOR_SIGNING_KEY` | (empty) | **Required in production** — boot halts without it. Test-only values never ship. |
| `IAP_ELASTIC_CURSOR_TTL_SECONDS` | `3600` | 60–86400. Rotation invalidates outstanding cursors. |
| `IAP_ELASTICSEARCH_QUERY_TIMEOUT` | `25.0` | Server-side query timeout; must stay below `IAP_ELASTICSEARCH_TIMEOUT`. |
| `IAP_ELASTICSEARCH_MAX_HITS` | `200` | Operators may lower, never raise (same for the three below). |
| `IAP_ELASTICSEARCH_MAX_TIME_WINDOW_SECONDS` | `604800` | 7 days. |
| `IAP_ELASTICSEARCH_MAX_KEYWORD_TERMS` | `20` | |
| `IAP_ELASTICSEARCH_MAX_SERVICES` | `10` | MCP tier accepts up to 20; 11–20 fail as `INVALID_REQUEST` at the domain tier. |
| `IAP_MCP_ENABLED` | `false` | Master switch for serving MCP. |
| `IAP_MCP_TRANSPORT` | `stdio` | `stdio` or `http` (`streamable-http` alias). |
| `IAP_MCP_HOST` / `IAP_MCP_PORT` | `127.0.0.1` / `8899` | Bind stdio N/A; HTTP serve address. Never expose unauthenticated. |
| `IAP_MCP_TENANT_HEADER` / `IAP_MCP_INVESTIGATION_HEADER` | `X-Tenant-ID` / `X-Investigation-ID` | Identity header names for HTTP. |
| `IAP_MCP_TENANT_ID` / `IAP_MCP_INVESTIGATION_ID` | (empty) | stdio scope binding; stdio refuses to start without both. |
| `IAP_MCP_ACTOR_ID` | `local-operator` | stdio actor label. |
| `IAP_MAPPING_PROFILES_PATH` | `config/observability-mappings` | Directory of `ObservabilitySourceMapping` YAMLs. Missing dir = generic-ECS only. Malformed/duplicate/overlapping documents abort boot. |

## 10. Running

**Tests:** `uv run pytest tests` (784 passed; container/cognee-live tests opt-in). Quality: `uv run ruff check src tests migrations`, `uv run ruff format --check <touched files>`, `uv run mypy src` (6 pre-existing errors at HEAD, zero new).

**stdio (local agent):** set `IAP_MCP_ENABLED=true`, `IAP_MCP_TRANSPORT=stdio`, `IAP_MCP_TENANT_ID`, `IAP_MCP_INVESTIGATION_ID`, plus the standard boot env (`IAP_DATABASE_URI`, `IAP_LLM_API_KEY`, `IAP_ELASTIC_CURSOR_SIGNING_KEY`, …), then `uv run python -m investigation_agent_platform.mcp.server`. The instance serves exactly that investigation.

**HTTP:** `IAP_MCP_TRANSPORT=http` serves the authenticated app via `main()` on `IAP_MCP_HOST:PORT`, or mount it inside the platform API with `mount_mcp(app, services, mcp_config)` at `/mcp`. Clients authenticate per request (`Authorization: Bearer …`, `X-Investigation-ID`). If fronted by the platform API, add `X-Investigation-ID` to the CORS allow-list. Requires TLS termination in front; never bind `0.0.0.0` without authentication terminating correctly.

## 11. Known limitations and residual risks

- Best-effort pagination (no snapshot); long paginations over hot indices can repeat/skip.
- Trace evidence beyond the request budget reports `truncated` — agents must narrow the window, not conclude absence.
- `SEARCH_LOGS` capability must be granted in tenant profiles or gateway search rejects.
- Gateway search does not yet propagate `correlation_id` to the provider span (detail/trace paths do).
- Naïve (timezone-less) agent timestamps are interpreted as UTC by Elasticsearch; mixed naive/aware ranges are rejected.
- Observability profiles select field mappings via `mapping_source_id` (default `generic-ecs`); the legacy singular `*Field` attributes remain accepted but unenforced on the Elastic path.
- Pre-mapping evidence rows are unresolvable by ID by design.
- `query_string` on `RuntimeEvidenceRequest` is reserved legacy: accepted and sanitized, never executed by the Elastic provider (only forwarded to third-party tools by the outbound MCP adapter).
