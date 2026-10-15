# IAP Application Profile 1.0 — Content Model, Lifecycle API, Platform Adoption & Spring Derivation

Status: FINAL DESIGN (no code). Version 1.0. Supersedes `tmp/IAP-implementation-appl-profile-v0.1.md` (rev 2) and incorporates all items from `tmp/IAP-implementation-appl-profile-base.md` that v0.1 did not consider (concrete schema blocks, extended enums, structured query templates, per-kind datasource specs, migration specifics, phased plan, file appendix — see §14).

## 1. Purpose, scope & goals

The application profile is the platform's only tenant-authored description of *what it is investigating*: the business domain, the planes (backend / frontend / workers), the services, their telemetry, their code, and their data. Today the profile is a flat, mostly static document (`domain/profile/models.py`, 6 sections) that several subsystems read partially — and several fields nothing reads at all (`indices`, `correlation.fields`, `allowed_capabilities`, `owner`, `status`). Meanwhile the hardest wiring problems (which log index for which service, which repo for which trace hop, which database connection for which query, all per tenant) are solved with process-global config (env DSNs, static MCP YAML, `IAP_CODE_REPO_BASE`), which cannot scale past one application.

Goals:

- Comprehensive capture of application topology (multi-service), data sources (current Oracle plus PostgreSQL, S3, DynamoDB, Redis, MongoDB, Kafka and future kinds), telemetry, code, dependencies, on-call/runbooks, SLOs, data classification, domains/planes, and capabilities — everything the platform needs to investigate and find evidence.
- Extensibility for new datasource kinds without breaking changes (kind union + per-kind spec models).
- Spring Config derivation producing DRAFT profiles (never auto-promoting secrets or inferred values to ACTIVE).
- All consumers read from the profile (not globals); backward compatibility with v1 profiles via derivation.
- Security-first: no secrets in profile JSON, `connection_ref` + secret resolver, strict API response sanitization, fail-closed readers.
- Immutable revisions, tenant-scoped, RLS preserved.

This design proposes: (a) a richer, service-oriented profile content model with concrete schema; (b) full lifecycle management (create / get / update routes + immutable-revision persistence); (c) a consumer-by-consumer adoption plan so every part of the platform reads the profile instead of globals; (d) dynamic profile instantiation from a Spring Config Server (§12). Design-only: no implementation, no migration performed.

## 2. Current state (verified against source)

### 2.1 Content model (`domain/profile/models.py`)

`ApplicationProfile{id, tenant_id, name, description, environment, version, status, owner, allowed_capabilities}` plus aliased sections: `observability` (provider, indices, *Field mappings, `mappingSourceId`), `state` (provider, database, schema, tables, identifiers, state/timestamp fields, `queryTemplates` with DDL/DML validator), `code` (provider, repository, branch, language, roots, build, `repositoryId?`, `serviceName?`), `correlation` (`fields[]`), `investigation` (time window, per-query caps, durations, `enabledEvidenceTypes`, depth, max hypotheses, `parametersSchema?`), `referenceDocs?` (sources, hosts, roots, embedding model). Missing: multi-service shape, multi-datasource (S3/DynamoDB/Postgres beyond Oracle), per-service telemetry, dependencies, domains/planes, on-call/runbooks, SLOs, data classification, owners list, enforced capabilities, connection references, per-datasource MCP mapping, knowledge config.

### 2.2 Persistence

`application_profiles` table stores immutable revisions keyed `(tenant_id, id, version)` (`uq_app_profile_tenant_id_version`, established by migration `009_profile_revisions.py`); `save()` is insert-only with `on_conflict_do_nothing` (idempotent retry); reads resolve latest-or-pinned revision, never silently fall back (`infrastructure/persistence/profile_repository.py`). RLS tenant-scoped. Status/owner live *inside* `profile_json`, not as columns. File loader exists (`load_application_profile_from_file`, `config/profiles/*.yaml`); `scripts/seed_profile.py` + `scripts/provision_tenant.py` (DRAFT-from-manifest) are the only writers; `_DEFAULT_PROFILE` seeds `tenant-a/example-app`.

### 2.3 Routes

Only `GET /profiles` (latest per app, sanitized: strips `state.queryTemplates`/`connectionUrl`). No create, no detail, no update. Investigations pin `metadata.profile_version` at create; nothing re-validates later revisions.

### 2.4 Consumers (condensed; full map in §7)

Gateway composes authorizer/capability/registry from the repo; authorizer gates on `enabledEvidenceTypes` (`allowed_capabilities` informational only); elastic path uses `mappingSourceId→mapping.index_patterns` (`indices` unread); state providers are global (single Oracle DSN, static MCP YAML, **no Postgres provider at all** despite the seed claiming `postgres`); code paths read single `repository`/`defaultBranch`/`sourceRoots` plus global `IAP_CODE_REPO_BASE`; topology reverse-maps OTel `service.name` via `code.serviceName`; validator enforces templates/roots/caps; worker bounds queries by time-window/caps; knowledge/chat/capabilities routers read no profile fields.

### 2.5 Spring Config reference (`tmp/sourcecode/`)

Reference provides `OAuth2ClientCredentials` (token caching with early renewal, HTTPS-only), `SpringConfigClient` (`GET {server}/{app}/{profile}[/{label}]`, allowlists before I/O, precedence-ordered `propertySources` merge, allowlist + redaction, typed errors: authentication / authorization / not-found / rate-limit / server / transport), count-only logging. Package layout prescribed as `integrations/spring_config/{__init__,client,models,security,exceptions}.py` with tests under `tests/integrations/spring_config/`.

## 3. Proposed content model (v2)

Design principles: (i) every field names its consumer (§7 traces each one); (ii) no secrets in the profile — connection descriptors are *references* resolved server-side; (iii) additive and back-compatible — v1 documents validate as v2 with derived defaults (§8). All models frozen (`ConfigDict(frozen=True)`); top-level scalars stay snake_case, sub-blocks camelCase aliases (existing convention).

### 3.1 Core identity

```python
class ProfileStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    ARCHIVED = "ARCHIVED"

class ApplicationProfile(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str = Field(..., max_length=128)
    tenant_id: str = Field(..., max_length=128)
    name: str = Field(..., max_length=256)
    description: str = Field(..., max_length=2048)
    environment: str = Field(..., max_length=64)
    environments: list[str] = Field(default_factory=list, max_length=10)
    version: int = Field(default=1, ge=1)
    status: ProfileStatus = ProfileStatus.DRAFT
    owner: str = Field(..., max_length=128)
    owners: list[str] = Field(default_factory=list, max_length=20)
    domain: str | None = Field(default=None, max_length=128)
    planes: list[str] = Field(default_factory=list, max_length=10)
    data_classification: str | None = Field(default=None, max_length=32)
    allowed_capabilities: list[str] = Field(default_factory=list, max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=50)
    source: ProfileSource | None = Field(default=None)
    derivation: DerivationReport | None = Field(default=None)
    services: list[Service] = Field(default_factory=list, max_length=50)
    datasources: list[Datasource] = Field(default_factory=list, max_length=100)
    observability: ObservabilityProfile | None = None  # legacy, derived (§8)
    state: StateProfile | None = None                  # legacy, derived (§8)
    code: CodeProfile | None = None                    # legacy, derived (§8)
    correlation: CorrelationConfig | None = None
    investigation: InvestigationConfig | None = None
    knowledge: KnowledgeConfig | None = None
    reference_docs: ReferenceDocsProfile | None = Field(default=None, alias="referenceDocs")
    slos: list[SLO] = Field(default_factory=list, max_length=20)
    runbooks: list[Runbook] = Field(default_factory=list, max_length=20)
    oncall: dict[str, Any] | None = None
```

Semantics: `domain` (free-text business domain) feeds reason prompts and knowledge-reuse scoping; `planes ⊆ {backend, frontend, mobile, workers, data-pipeline}` declares which evidence families are meaningful so the planner skips inapplicable actions instead of failing; `environments` covers pre-prod twins while `environment` stays the canonical production name used in index synthesis; `owners[]` extends `owner` (primary) for team ownership; `tags[]` is free-form operator indexing; `oncall{team, channel, escalation_policy_ref}` surfaces in investigation context (never auto-paged — no actuator in 1.0); `data_classification ∈ {PUBLIC, INTERNAL, CONFIDENTIAL, RESTRICTED}` upper-bounds what chat/knowledge may quote; new profiles default to `DRAFT` (promotion is explicit, §4); `source` records provenance (`authored` | `manifest` | `spring-config` + binding block, §3.7/§12) with section-level authored-vs-derived tracking on operator edit.

### 3.2 Services (the core of the model)

```python
class ServiceTelemetry(BaseModel):
    model_config = ConfigDict(frozen=True)
    log_indices: list[str] = Field(default_factory=list, max_length=50)
    service_names: list[str] = Field(default_factory=list, max_length=50)
    trace_backend: str | None = Field(default=None, max_length=64)
    metrics_backend: str | None = Field(default=None, max_length=64)
    default_lookback_s: int | None = Field(default=None, ge=60, le=7 * 86400)

class ServiceCode(BaseModel):
    model_config = ConfigDict(frozen=True)
    org: str | None = Field(default=None, max_length=128)
    repos: list[str] = Field(default_factory=list, max_length=20)
    default_branch: str = Field(default="main", max_length=128)
    language: str | None = Field(default=None, max_length=64)
    source_roots: list[str] = Field(default_factory=list, max_length=50)
    build_system: str | None = Field(default=None, max_length=128)
    module_structure: str | None = Field(default=None, max_length=256)
    paths: list[str] = Field(default_factory=list, max_length=100)

class ServiceDependency(BaseModel):
    model_config = ConfigDict(frozen=True)
    service: str = Field(..., max_length=128)
    type: str = Field(default="internal", max_length=32)
    critical: bool = False

class Service(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str = Field(..., max_length=128)  # canonical; matches OTel service.name
    role: str | None = Field(default=None, max_length=32)  # edge | dependency | store
    description: str | None = Field(default=None, max_length=512)
    telemetry: ServiceTelemetry = Field(default_factory=ServiceTelemetry)
    code: ServiceCode = Field(default_factory=ServiceCode)
    dependencies: list[ServiceDependency] = Field(default_factory=list, max_length=50)
    endpoints: list[str] = Field(default_factory=list, max_length=50)
    repository_id: str | None = Field(default=None, max_length=128)  # topology identity
```

Rationale per field: `service_names` fixes the observed live failure where the model guessed service filters and matched nothing; `org/repos` replaces `IAP_CODE_REPO_BASE` + singular `repository` so trace hops and code search resolve per service; `dependencies{service,type,critical}` feeds topology traversal order, correlation breadth, and critical-path weighting; `telemetry.log_indices` lets one profile span index families (replacing the single unread `indices` list); `default_lookback_s` replaces magic windows in activities; `endpoints[]` scopes trace/validation to declared API surfaces.

### 3.3 Datasources — typed, reference-based, multi-connection

```python
class DatasourceKind(StrEnum):
    ORACLE = "oracle"; POSTGRES = "postgres"; MYSQL = "mysql"; SQLSERVER = "sqlserver"
    S3 = "s3"; DYNAMODB = "dynamodb"; REDIS = "redis"; MONGO = "mongo"
    CASSANDRA = "cassandra"; ELASTICSEARCH = "elasticsearch"; KAFKA = "kafka"
    RABBITMQ = "rabbitmq"; MCP = "mcp"; OTHER = "other"

class DatasourceRole(StrEnum):
    STATE = "state"; READONLY = "readonly"; ANALYTICS = "analytics"; CACHE = "cache"
    QUEUE = "queue"; OBJECT_STORE = "object_store"; SEARCH = "search"
    METRICS = "metrics"; LOGS = "logs"; TELEMETRY = "telemetry"

class QueryTemplate(BaseModel):
    model_config = ConfigDict(frozen=True)
    description: str | None = Field(default=None, max_length=256)
    sql: str = Field(..., min_length=1, max_length=8192)
    read_only: bool = True
    allowed_tables: list[str] = Field(default_factory=list, max_length=50)
    timeout_ms: int | None = Field(default=None, ge=100, le=300000)

class S3DatasourceSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    bucket: str; prefix: str | None = None
    region: str | None = None; endpoint_url: str | None = None

class DynamoDBDatasourceSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    table: str; region: str | None = None; endpoint_url: str | None = None

class KafkaDatasourceSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    bootstrap_servers: list[str] = Field(default_factory=list, max_length=10)
    topics: list[str] = Field(default_factory=list, max_length=50)
    group_id: str | None = None

class RedisDatasourceSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    host: str | None = None; port: int | None = None; db: int | None = None

class Datasource(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str = Field(..., max_length=128)  # stable; templates namespace as {id}.{name}
    name: str = Field(..., max_length=256)
    kind: DatasourceKind
    role: DatasourceRole
    description: str | None = None
    connection_ref: str | None = Field(default=None, max_length=512)  # secret:// or env://
    read_only: bool = True
    allowed_tables: list[str] = Field(default_factory=list, max_length=200)
    query_templates: dict[str, QueryTemplate] = Field(default_factory=dict, max_length=100)
    mcp_server: str | None = Field(default=None, max_length=128)
    s3: S3DatasourceSpec | None = None
    dynamodb: DynamoDBDatasourceSpec | None = None
    kafka: KafkaDatasourceSpec | None = None
    redis: RedisDatasourceSpec | None = None
    tenants: list[str] = Field(default_factory=list, max_length=20)  # empty = declaring tenant only
```

Rules: secrets (passwords, keys, credential-bearing DSNs) are *never* valid profile content — validation rejects `://[^@]+@` URLs and `AKIA|BEGIN .*PRIVATE KEY|password` patterns. Existing `StateProfile.query_templates` (plain `dict[str, str]`) migrate into structured `QueryTemplate` entries with the same DDL/DML validator applied to `sql` plus per-template `allowed_tables`/`timeout_ms`. `tenants[]` supports platform-shared datasources (empty = declaring tenant only; non-empty is allowlist-reviewed, never a bypass of RLS — rows stay tenant-scoped). Future kinds extend the union + an optional spec model without touching the envelope.

### 3.4 Investigation / correlation / knowledge / SLOs / runbooks

```python
class InvestigationConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    default_time_window: int = Field(default=3600, ge=60, le=86400)
    max_evidence_per_query: int = Field(default=50, ge=1, le=500)
    max_investigation_duration: int = Field(default=7200, ge=60, le=86400)
    enabled_evidence_types: list[str] = Field(
        default_factory=lambda: ["LOG", "TRACE", "DATABASE_STATE", "COMMIT_HISTORY"],
        max_length=30)
    correlation_depth: int = Field(default=2, ge=1, le=10)
    max_hypotheses: int = Field(default=5, ge=1, le=50)
    evidence_budget: dict[str, int] | None = None  # wires per-app F-054 ledger caps

class CorrelationConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    fields: list[str]
    priority: list[str] = Field(default_factory=list)

class KnowledgeConfig(BaseModel):
    model_config = ConfigDict(frozen=True)
    reuse_scope: str = "tenant_app"
    retention_days: int = 30
    min_confidence: float = 0.7
    enable_cross_tenant_fingerprints: bool = False

class SLO(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str; target: float; window: str = "30d"; objective: str | None = None

class Runbook(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str; url: str | None = None; path: str | None = None
    type: str = "doc"; ref: str | None = None
```

`correlation.priority` orders `fields` for traversal (currently neither is read — both activate in §7.6). `evidence_budget` replaces process-global `InvestigationLimits` per app. `KnowledgeConfig` replaces magic constants in capture/reuse; cross-tenant fingerprints stay redacted per D15 even when enabled. SLOs/runbooks are schema-present in 1.0, consumer-deferred (gate readiness context + chat suggestions in later phases).

### 3.5 Reworked legacy sections (all back-compatible)

- `observability`: keep provider/fields/mapping; `indices` becomes the *default* family, overridable per service (§3.2); add `trace_backend` + `default_lookback_s`.
- `code`: keep single-repo fields as deprecated aliases projecting to `services[0].code`; add `orgs[]` allowlist (tenant checks move from one repo string to set membership).
- `state`: deprecated in favor of `datasources[role in (state, readonly)]`; retained as a read-only projection of the first state datasource until removal. Templates migrate to structured form.
- `referenceDocs`: unchanged.

### 3.6 Derivation / source blocks

```python
class ProfileSource(BaseModel):
    model_config = ConfigDict(frozen=True)
    type: str = "spring_config"  # authored | manifest | spring_config
    server_url: str
    spring_application: str
    spring_profile: str
    label: str | None = None
    fetched_at: datetime | None = None
    refresh_cadence: str | None = None  # P2 scheduled refresh only

class DerivationReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    fetched_at: datetime
    property_sources: list[str] = Field(default_factory=list)
    property_count: int = 0
    unmapped_keys: list[str] = Field(default_factory=list)
    needs_review: list[str] = Field(default_factory=list)
    dropped_sensitive_keys: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
```

Key names only, never values — safe for logs, responses, and audit rows.

## 4. Profile lifecycle & versioning semantics

`DRAFT → ACTIVE → DEPRECATED → ARCHIVED`; `ACTIVE → DRAFT` forbidden (clone instead); `ARCHIVED` terminal. Every mutation writes a new immutable revision (`version = latest + 1`); `save()` semantics unchanged. `GET /profiles/{app_id}` returns the latest revision of any status so consumers can retrieve and update DRAFTs (amendment: the original "readers default to latest `ACTIVE`" rule proved unworkable — DRAFT-only apps 404'd; investigation-time resolution still pins latest `ACTIVE`, see below); `?version=` pins (exact, no fallback — existing rule). Deletion is DRAFT-only: `DELETE /profiles/{app_id}` removes a whole application only when every revision is DRAFT, and `DELETE ...?version=N` removes a single DRAFT revision (e.g. discarding a bad derived DRAFT atop ACTIVE); any non-DRAFT history → 409, nothing deleted — non-DRAFT revisions are the immutable audit trail investigations pin against. `GET` lists latest per app *including* status so the UI can badge non-ACTIVE. Investigations keep pinning `profile_version`; a `DEPRECATED`/`ARCHIVED` profile carries on serving in-flight runs but blocks new creates/starts (checked in `CreateInvestigationService` + `_dispatch_workflow`). Derivation output is always `DRAFT` (§12.4).

## 5. API design (routes)

All routes tenant-scoped via `require_tenant`; all mutations accept `X-Idempotency-Key` (create/update/fulfill pattern already established).

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/profiles` | List latest per app; `?status=` filter; sanitized |
| POST | `/api/v1/profiles` | Create (version 1, `DRAFT` or `ACTIVE`); 201, or 200 idempotent replay on same `(id, version)` |
| GET | `/api/v1/profiles/{app_id}` | Latest `ACTIVE` by default; `?version=N` pins; unknown → 404 |
| GET | `/api/v1/profiles/{app_id}/revisions` | Revision summaries only (full body via `?version=`) |
| PUT | `/api/v1/profiles/{app_id}` | Full replacement → `latest+1`; transitions enforced; body `version` absent-or-equal else 409 |
| POST | `/api/v1/profiles:derive-from-spring` | Derive DRAFT from Spring (`{tenant_app_id, spring:{...}, persist?}`); never persists ACTIVE |
| POST | `/api/v1/profiles/{app_id}:refresh-from-spring` | Re-derive from stored `source`; writes only on material diff else `{changed: false}` |
| GET | `/api/v1/profiles/{app_id}:spring-drift` | Drift preview, no write (operator review screen) |

Bodies accept snake_case top-level + camelCase aliases on write; stored canonical. Errors: 400 schema/secret/transition violations (machine-readable `code` + field path), 404 unknown app/version, 409 version conflict / illegal transition, 422 semantic (unknown mapping, unknown capability, dangling `service_names`, unresolvable Spring binding), all tenant-isolated.

### 5.1 Read sanitization matrix (extends current strip)

Public reads strip: `queryTemplates` bodies (list views may keep template *names*; detail keeps names + descriptions, never SQL), any `connection_ref` *value* (return `{present: true, scheme}`), `mcp_server` credentials (name only), `derivation.unmapped_keys` keep (names only — safe). A privileged connection-meta read is OUT of scope (no secret-adjacent reads until a dedicated accessor role exists). `provision_tenant.py` output stays DRAFT with TBD placeholders by construction.

## 6. Persistence design

Migration `010_profile_v2_columns.py` (down_revision `009_profile_revisions`):

- Add `status VARCHAR NOT NULL DEFAULT 'ACTIVE'`, `domain VARCHAR(128)`, `planes TEXT[]`.
- Backfill `status` from `profile_json->>'status'`; drop defaults after backfill.
- `CREATE INDEX idx_app_profile_json_gin ON application_profiles USING GIN (profile_json jsonb_path_ops)` — service/datasource lookups for the registry without full scans.
- `CREATE INDEX idx_app_profile_tenant_status (tenant_id, status)` — list filtering + ACTIVE gate.
- `CREATE INDEX idx_app_profile_tenant_domain (tenant_id, domain)` — domain reuse scoping.
- Mirror `status/domain/planes` into columns on `save()`; keep `profile_json` authoritative on conflict (column is a read optimization, JSON wins).

Repository: `list(tenant_id, status=None)` latest-per-app with optional status filter; `list_revisions`/`get_by_application_id` unchanged; audit via outbox events (`ProfileRevisionCreated` with actor/tenant/id/from→to version — no new audit table in 1.0, resolving the earlier open question in favor of outbox reuse).

## 7. Consumer adoption matrix (what changes, file by file)

Each row: current behavior → 1.0 change. Nothing here alters provider protocols, only where each layer reads its inputs from.

### 7.1 Elastic / log names / tracing (`infrastructure/evidence/runtime/*`, `logs/*`)

- `elastic.py::search_runtime_evidence` — service-level index scope: `request.services` ∩ `profile.services[].telemetry.{log_indices,service_names}`; unknown service names reject (fail-closed typo guard) instead of querying blind. `indices` becomes the default family when the app declares no services.
- `query.py` — unchanged shape; service terms resolve against the `service_names` allowlist.
- `elastic_bridge.py` / `trace_telemetry.py` — already forward `mapping_source_id`; additionally forward `service_names` so derivation and retrieval share the filter set.

### 7.2 Source-code parsers (`evidence/code/*`, topology registry)

- `git.py` / `intelligence.py` — replace single-`repository` tenant checks with membership in `services[].code.{org,repos}`; resolve `repo_base` per org (`{base}/{org}/{repo}`), retiring global `IAP_CODE_REPO_BASE` as the *default* (kept as fallback root).
- `profile_repository_registry.py` — reverse lookup by `service.name` across `services[].name` (replaces linear scan over `code.serviceName`); `RepositoryIdentity` gains `org` from the matched service (replaces locator-parsing `git_org`); per-service `repository_id` becomes the canonical topology identity.
- `validator.py` — `GET_CODE` path check against the union of `services[].code.{paths,source_roots}`; `endpoints[]` constrains trace-surface validation (phase 2).

### 7.3 State providers (validator, oracle, new Postgres)

- `validator.py` — `QUERY_STATE` template allowlist becomes the union of state-role `datasources[].query_templates` namespaced `{datasource_id}.{name}` (structured `QueryTemplate`, DDL/DML validator on `sql`, per-template `allowed_tables`/`timeout_ms`); legacy flat templates remain as fallback.
- `oracle.py` — adapter instantiated per datasource (`AsyncOracleStateAdapter` per `datasource_id`) instead of one global DSN; `allowed_tables` from the datasource entry (replacing today's empty set).
- NEW `infrastructure/evidence/state/postgres.py` mirroring the oracle adapter's SELECT-only + template discipline — the seed has claimed `provider=postgres` with no backing code.

### 7.4 Dynamic multi-database connections (the core ask)

- New `DatasourceRegistry` (behind the existing `ProfileBackedRepositoryRegistry` seam, new module `infrastructure/evidence/datasources/registry.py`): `resolve(tenant, app_id, datasource_id[, version]) → live adapter`, keyed by profile revision (cache invalidated on new revision, TTL-bounded otherwise).
- New `SecretResolver` port (`ports/secrets/resolver.py`, impls under `infrastructure/secrets/*`): `secret://` (vault-backed in prod) and `env://` (`IAP_DS_<TENANT>_<ID>` in dev); plain credential-less DSNs allowed for local dev only (`IAP_ENVIRONMENT != production` gate).
- `mcp.py` — MCP servers declared per datasource (`mcp_server` name → entry in a registry built from *profile-declared* names, not the static YAML): `_oracle_mcp_servers(yaml)` becomes a fallback for entries with no profile declaration; tenant allowlist derives from the declaring tenant (supplemented by datasource `tenants[]` for shared platform stores) instead of YAML `tenants[]`.
- Bootstrap (`_wire_evidence_gateway`) stops registering global providers: elastic stays (shared deployment), oracle/MCP/pygit2 become lazily resolved per (tenant, app, datasource) through the registry. Provider selector keeps its `tenant:app:env` routing keys; the *connection* behind a key becomes profile-derived. Datasource `role` picks the provider family (state→SQL adapters, telemetry→elastic/kafka, object_store→S3 readers in later phases).

### 7.5 Authorizer & capabilities (`infrastructure/security/*`, gateway)

- Activate `allowed_capabilities`: `authorize_action` requires capability ∈ `allowed_capabilities` (when non-empty; empty preserves current behavior for migrated v1 profiles) ANDed with the evidence-type gate. `is_capability_enabled` reads the same list instead of returning the static enum set. `GET /capabilities` filters by the caller's profile.
- Gateway `_enforce_authorization` unchanged (already threads `application_id`).

### 7.6 Correlation, bounds, knowledge, chat, topology, worker

- Correlation: engine/validator consume `correlation.fields` with `priority` ordering, plus `investigation.correlation_depth`/`max_hypotheses` caps — all currently defined-but-unread.
- Investigation bounds: `evidence_budget` (per-app F-054 caps) replaces process-global `InvestigationLimits` in `execute_action_activity`; time windows already consumed.
- Knowledge: `KnowledgeConfig` (`reuse_scope/retention_days/min_confidence`) replaces magic constants in capture/reuse; `domain` enables cross-tenant fingerprint candidates (still redacted per D15; `enable_cross_tenant_fingerprints` gates even that).
- Chat: session `classification` ceiling from `data_classification`; `application_id` quota scope already profile-adjacent.
- Topology: build edges from `services[].dependencies{service,type,critical}` + `code.service_name` (already read); snapshot retention unchanged.
- Worker activities: `retrieve_evidence`/`execute_action` read per-service `telemetry` + `default_lookback_s` instead of app-level defaults only; reason prompt gains `{domain, planes, services[].name/role}` context (same envelope discipline as F-048 — typed records, never concatenated secrets).
- MCP tools: no signature changes; they inherit everything via services/gateway.
- UI (read-only in early phases): profiles screen renders services/datasources/status/SLOs/runbooks from GET; create/edit UI deferred (API-first).

## 8. Back-compat & migration

v1 documents validate as v2 via derivation: `services[]` synthesized as one entry from top-level `code`/`observability` (name = `application_id`, `service_names` = [] meaning "no filter"); `datasources[]` synthesized from `state` (kind = `state.provider`, role = state/readonly by heuristic, templates carried into structured form); `domain/planes/oncall/owners/tags` default empty; `allowed_capabilities` empty (preserves current auth behavior); legacy `observability/state/code` blocks retained *readable* (Optional, derived on read when absent) so old writers keep working. `scripts/seed_profile.py --dry-run` gains `--migrate-check` reporting what derivation would produce, no writes. Existing `tenant-a/example-app` stays valid untouched. Consumers try v2 sections first, fall back to legacy blocks (fallback removed per-consumer only when its v2 coverage is proven).

## 9. Security considerations

Secrets never in profile content (§3.3 scan + §5.1 strip matrix + §12 redaction); RLS unchanged; per-datasource `read_only: true` default with SELECT-only enforcement at both template validation and adapter layers (defense in depth, mirroring today's oracle posture); `allowed_hosts`/`localAllowedRoots` discipline extended to datasource `connection_ref` schemes (allowlist `secret://`, `env://`, dev-only credential-less DSNs); HTTPS-only for Spring endpoints; tenant isolation + RLS; property allowlists + redaction; fail-closed readers; audit via outbox `ProfileRevisionCreated` (+ derive/refresh entries with Spring app/profile/label and property counts, never values); MCP tenant scoping from declaring tenant; connection-scheme allowlist with dev-plain restricted to non-prod.

## 10. Spring Config Server derivation (dynamic profile instantiation)

Goal: given a Spring Config Server URL (+ application/profile/label), dynamically instantiate an ApplicationProfile DRAFT from the live Spring configuration instead of hand-authoring it. Reference material: `tmp/sourcecode/{config.yaml, pkg_layout.md, spring_config.py}` — the client design below adopts that reference's contract (OAuth2 client-credentials, HTTPS-only, allowlists, precedence merge, redaction, typed errors) and adds the IAP derivation layer on top. No Spring code exists in `src/` today; package layout follows `pkg_layout.md`.

### 10.1 Client (`integrations/spring_config/`, per `pkg_layout.md`)

- `client.py` — `SpringConfigClient` + `OAuth2ClientCredentials` exactly per the reference: token cache with early renewal, `GET {server}/{application}/{profile}[/{label}]`, timeouts, bounded GET-only retries (429/502/503/504, honor Retry-After), application allowlist + profile allowlist enforced *before* any I/O, label charset validation, precedence-ordered `propertySources` merge, property allowlist + sensitivity redaction, typed errors (authentication / authorization / not-found / rate-limit / server / transport), count-only logging (never bodies/values/tokens). Port the reference file largely as-is; adapt imports/logging to repo conventions. Sync `requests` is acceptable here (derive/refresh paths only, never the Temporal workflow hot loop — see §10.5).
- `models.py` — `ConfigResult{application, profile, label?, properties, property_sources}` + `SpringSource` declaration + `DerivationReport` (§3.6).
- `security.py` — property allowlist patterns + sensitivity matcher (superset of the reference regex: add `spring.datasource.password`, `*_keystore*`, `*truststore*`, `spring.rabbitmq.*password*`, cloud credential keys) plus tenant-scoped allowlist loader (which Spring apps/profiles a tenant may import).
- `exceptions.py` — the typed hierarchy, mapped to HTTP in §10.6.
- `config.yaml` — operator env template: `<PREFIX>_CONFIG_SERVER_URL`, `<PREFIX>_OAUTH_{TOKEN_URL,CLIENT_ID,CLIENT_SECRET(injected at runtime),SCOPE}`. Client secret comes from the runtime environment/secret store only — never from profile content, never logged.
- Tests mirror `pkg_layout.md` (`test_client/test_oauth/test_security`) against a stub Config Server (fixture `propertySources` payloads, no network).

### 10.2 Derivation mapping (Spring properties → profile sections)

A pure function `derive_profile(tenant_app_id, ConfigResult) → (DRAFT profile, report)`. Each rule is tagged observed (direct read) or inferred (heuristic, flagged in `needs_review[]`):

- Identity/services: `spring.application.name` → `services[].name` (observed); one service per distinct name seen across label variants, else a single service. `server.port` + `management.server.port` → service liveness hints (inferred, recorded as notes, not authoritative). `spring.profiles.active` → `environment` mapping (`prod*`→production, `stag*`→staging alias in `environments`).
- Datasources: `spring.datasource.url` → datasource entry, kind parsed from JDBC prefix (`jdbc:postgresql|oracle:thin|mysql|sqlserver`) with host/db captured and **credentials dropped** → `connection_ref: TBD` placeholder + `needs_review` (observed-shape, manual completion required — the design refuses to invent or persist credential-bearing URLs). `spring.jpa.*`/`spring.datasource.hikari.*` → pool/timeout hints into datasource ceilings. `spring.data.redis.*` → `kind: redis` (+ host/port/db spec); `spring.mongodb.*` → `kind: mongo`; `spring.cassandra.*` → `kind: cassandra` (schema-present, provider-deferred like `dynamodb`/`s3`). `spring.kafka.bootstrap-servers` + `spring.kafka.template.default-topic` → `kind: kafka` datasource (role telemetry/queue). `cloud.aws.s3.bucket` / `aws.s3.*` → `kind: s3` (bucket/prefix observed, credentials never). `spring.dynamodb.*` (custom namespace) → `kind: dynamodb` table entry when present. `spring.rabbitmq.*` → `kind: rabbitmq` (schema-present, provider-deferred).
- Telemetry: `logging.level.*` package map → investigation context notes (which packages log at DEBUG — scoping hints, not index selection); logstash/ELK appender props (`logging.logstash.*`) → `telemetry.log_indices` candidates (inferred). `management.tracing.*` / `management.zipkin.*` / OTel `otel.*` → `trace_backend` + candidate `service_names` (observed where explicit).
- Code: `spring.cloud.config.server.git.uri` / `...search-paths` → `code.org/repos` candidates (inferred — config repo ≠ service repos, flagged review, never authoritative). `info.app.*`/`build.*` → language/build hints when present.
- Discovery/dependencies: `eureka.client.*`, `spring.cloud.discovery.*`, `spring.cloud.gateway.routes[*]` → `services[].dependencies[]` candidates with `type` inferred (`gateway` routes → edge, registry entries → internal) and `critical: false` default (inferred, review-flagged).
- Anything unmatched lands in `report.unmapped_keys[]` (names only); anything credential-shaped is dropped before derivation, counted in `dropped_sensitive_keys[]` (never in the profile).

### 10.3 Tenant binding & allowlists

An import request binds `(tenant_id, tenant_app_id) ↔ (server_url, spring_application, spring_profile, label?)`. Enforcement order: tenant allowlist (is this Spring app importable by this tenant?) → profile allowlist → fetch → property allowlist + redaction → derive. The `source` block (§3.1) stores the binding for refresh. Cross-tenant Spring apps are rejected even with a valid token (fail-closed, mirrors RLS posture).

### 10.4 DRAFT-only rule + operator completion

Derivation output is ALWAYS `status: DRAFT` (even if the operator asks for ACTIVE — coerced with a warning in the response). Rationale: secrets are placeholders (`connection_ref: TBD`), `query_templates` are empty (operator authors per datasource), and inferred fields need human confirmation. The `derivation` provenance block travels with the revision; operator `PUT` completions flip touched sections' provenance to `authored`. Promotion `DRAFT → ACTIVE` requires: zero `TBD` connection refs, ≥1 state datasource with ≥1 template OR explicit `state: none` declaration, and passing `seed_profile --migrate-check`-equivalent validation (reused as a library call, not a subprocess).

### 10.5 Refresh, drift & scheduling

- On-demand: `POST /profiles/{id}:refresh-from-spring` re-fetches with the stored binding, re-derives, diffs against latest revision (field-path diff, secrets excluded from the diff output), writes `latest+1` only on material change; otherwise `{changed: false}`.
- Drift view: `GET /profiles/{id}:spring-drift` returns the field-path diff without writing — the operator's review screen.
- Scheduled refresh (later phase): opt-in per profile (`source.refresh_cadence`); runs as a Temporal cron-style workflow reusing the derive path; writes DRAFT revisions only, never auto-promotes. Never runs inside the investigation hot loop (client is sync `requests`; worker activities stay async-only).

### 10.6 Error mapping (integration → HTTP)

`ConfigAuthenticationError` → 502 (platform's upstream credential rejected — operator action); `ConfigAuthorizationError` → 403 (scope/allowlist); `ConfigNotFoundError` → 404 (unknown Spring app/profile/label); `ConfigRateLimitError` → 429 (+ `Retry-After` passthrough); `ConfigServerError`/`ConfigTransportError` → 503. All mapped errors carry stable `code`s (`SPRING_AUTH`, `SPRING_FORBIDDEN`, `SPRING_NOT_FOUND`, `SPRING_THROTTLED`, `SPRING_UNAVAILABLE`) and never echo URLs, tokens, or property values.

### 10.7 Security posture (extends §9)

HTTPS enforced for both token and config endpoints (constructor-validated, per reference); OAuth secret runtime-injected only; property allowlist defaults to a conservative Spring-namespace set (`spring.application.*`, `spring.datasource.url` sans credentials, `server.port`, `management.*`, `logging.level.*`, `eureka.*`, `info.*`) with per-tenant extension via operator config; sensitivity matcher runs *after* allowlisting (defense in depth); token cache is in-memory, per-process, never persisted; every derive/refresh audit-logs (actor, tenant, spring app/profile/label, property count, revision written or not).

## 11. Implementation phases

**Phase 0 (Foundation):** v2 models in domain (enums, all §3 blocks), migration `010_profile_v2_columns.py`, repository updates (`list` status filter, mirrored columns), backward-compat derivation.  
**Phase 1 (API CRUD):** full CRUD + sanitization + idempotency + validation (§5 minus Spring routes); router wiring; contract tests.  
**Phase 2 (Spring):** `spring_config` package + derive/refresh/drift endpoints + DRAFT-only enforcement (§10).  
**Phase 3 (Platform integration):** elastic, git/intelligence, state/validator, authorizer/capabilities, correlation/bounds/knowledge, topology/worker — read from v2 with legacy fallbacks (§7).  
**Phase 4 (Dynamic datasources + Postgres):** `SecretResolver`, `DatasourceRegistry`, `infrastructure/evidence/state/postgres.py`, bootstrap lazy wiring, profile-driven MCP registration (§7.4).

## 12. Acceptance criteria

(i) v1 profiles validate as v2 unchanged via derivation; (ii) CRUD round-trip + immutability + sanitization covered by contract tests; (iii) each §7 integration proves profile-driven behavior with fallback; (iv) `verify-platform.sh` green; (v) journey driver green with a service-scoped investigation; (vi) derived DRAFT from a Spring Config fixture validates and round-trips with no secrets in output.

## 13. Open questions (for review, not implementation)

1. Should `services[].telemetry.service_names` be authoritative-deny (unknown service → 422) or advisory-warn? Proposal leans deny (typo fail-closed), needs operator sign-off.
2. Secret-resolver port backend for non-prod: env-mapped refs only, or local file store? Proposal: env only.
3. `datasources[].tenants[]` sharing semantics: allow shared platform datasources, or force per-tenant declarations? Proposal: allow with allowlist review (audit-logged).
4. Do `slos`/`runbooks` stay schema-present/consumer-deferred beyond 1.0? Proposal: yes, activation is a later phase.
5. Multi-`environment` twins: one profile with aliases vs per-env profiles? Proposal: aliases, revisit if isolation semantics diverge.
6. Spring label pinning: pin `label` (git ref) per profile source, or track branch head with refresh? Proposal: pin on derive, branch-track only via scheduled refresh with diff review.
7. Spring allowlist governance: per-tenant app/profile allowlists in operator config vs a platform-wide registry? Proposal: per-tenant, declared alongside the tenant record.

## Appendix A: Files to create / modify

**New:** `integrations/spring_config/{__init__,client,models,security,exceptions}.py`; `infrastructure/evidence/state/postgres.py`; `infrastructure/evidence/datasources/registry.py`; `ports/secrets/resolver.py`; `infrastructure/secrets/*`; tests under `tests/integrations/spring_config/` (client/oauth/security/derivation).  
**Modified:** `domain/profile/models.py` (v2 schema); `api/v1/routers/profiles.py` (CRUD + derive/refresh/drift); `infrastructure/persistence/{profile_repository.py,models.py}` (mirrored columns, status filter); `migrations/versions/010_profile_v2_columns.py`; `bootstrap/__init__.py` (lazy registry wiring); `infrastructure/evidence/runtime/elastic.py`; `infrastructure/evidence/code/{git.py,intelligence.py}`; `infrastructure/topology/profile_repository_registry.py`; `infrastructure/evidence/validator.py` (+ state adapters); `infrastructure/evidence/mcp.py`; `infrastructure/security/profile_authorizer.py`; `api/dependencies.py` (service builders); `application/worker/activities.py` (per-service telemetry, evidence budget); `application/investigation/*` (correlation/bounds/knowledge consumption).

## Appendix B: v1 → v2 field derivation (migration reference)

| v1 source | v2 target | Rule |
|---|---|---|
| `code.{repository,defaultBranch,language,sourceRoots,buildSystem,moduleStructure,repositoryId,serviceName}` | `services[0].{code,repos[0],...}` + `services[0].name = application_id` | Single synthesized service; `service_names = []` (no filter) |
| `observability.{provider,indices,*Field,mappingSourceId}` | `services[0].telemetry` + top-level defaults | `indices` becomes default family |
| `state.*` | `datasources[0]{kind=state.provider, role=state\|readonly}` | Templates restructured with `read_only: true` |
| `correlation.fields` | `correlation.{fields}` + `priority = fields` (order preserved) | Priority defaults to field order |
| `investigation.*` | `investigation.*` 1:1 | `evidence_budget` absent (global caps apply) |
| `referenceDocs` | `reference_docs` 1:1 | Unchanged |
| `owner/status/allowed_capabilities` | Same, `allowed_capabilities` empty-safe | Empty preserves current auth behavior |
| `domain/planes/oncall/owners/tags/slos/runbooks` | Empty defaults | No v1 source; operator-added later |
