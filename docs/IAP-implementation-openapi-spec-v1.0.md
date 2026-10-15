# IAP OpenAPI Spec 1.0 — Typed Response Models for the Backend API

Status: FINAL SPEC (no code). Version 1.0. Supersedes `docs/ui-backend-openapi-typed-models-design.md`
(referred to below as "the proposal") as the implementation authority. §2 records a
finding-by-finding review of the proposal against the source tree — several of its
claims did not survive contact with the code, and this spec differs from it where noted.
Do not implement the proposal directly.

## 1. Purpose, scope & goals

The backend is FastAPI on Pydantic v2 with 55 path operations and zero declared
`response_model`s, so `openapi.json` carries no response shapes and the UI hand-writes
runtime validators that drift (two real incidents: reference-docs envelope changes;
the `ApplicationProfile` service-oriented rewrite). This spec types every 2xx response
with declared Pydantic models plus shared envelopes and one documented error envelope,
so clients can generate types and runtime validators from the spec.

Scope is the API layer only — no persistence, behavior, or auth changes — except the
single deliberate wire change in §4 (error-envelope unification).

Goals:

- Every 2xx response that carries content references a concrete component schema.
- Shared generic envelopes for the list shapes actually emitted (verified per endpoint).
- One documented error envelope; every route references it.
- Sanitized view models for redacted resources (`ProfileView`), so typing never re-leaks secrets.
- Byte-identical wire output except the error-envelope unification (proven by golden tests).
- A CI guard that fails on any untyped or open-object 2xx response.

## 2. Review findings (proposal claim → verdict)

Verified with `rg`, handler reads, and (where stated) the running spec. References are
to proposal sections.

### F1. Counts: 55 operations confirmed; "16 routers" is 15

55 `@router.*` decorators confirmed across the router package. The package contains
15 router modules (plus `__init__.py`), not 16:

```text
aggregate_reports, batch_intake, capabilities, chat, clusters, events, evidence,
health, hypotheses, investigations, jobs, knowledge, profiles, reference_docs,
timeline  (15 modules; 4+5+1+4+3+2+2+10+2+2+3+1+6+8+4 = 55 operations)
```

`rg response_model src/` returns zero matches — confirmed. The "15 request-body schemas"
claim was not re-audited (request bodies are out of scope for behavior) and is taken
as stated.

**Spec change:** package layout mirrors the 15 real modules. There is no `intake.py`
schema module — `POST /intake/errors` lives in `investigations.py`, so its ack view
lives in `schemas/investigations.py` (the proposal's own one-module-per-router rule
requires this; its `intake.py` entry violates it).

### F2. The endpoint inventory was written aspirationally — five entries are wrong

Each was checked against the handler's actual return statement:

| Proposal entry | Actual wire shape | Verdict |
|---|---|---|
| `GET /jobs/{job_id}` → `JobView` | `{job: {...}}` wrapper (`jobs.py:49`) | Needs `JobDetail{job}` view, not bare `JobView` |
| `GET /clusters/{cluster_id}` → `ClusterView` | `{cluster, assignments, findings, total, offset, limit}` (`clusters.py:84`) | Needs `ClusterDetail` view |
| `GET /aggregate-reports/history` → `ListEnvelope[ReportHistoryItemView]` | `{items, next_cursor, has_more}` cursor page (`aggregate_reports.py:218`) | Must be `CursorEnvelope[...]`, not `ListEnvelope` |
| `GET /batch-intake/{job_id}` → `BatchIntakeView` | `{job, summary, items, total, offset, limit}` (`batch_intake.py:245`) | Needs dedicated `BatchIntakeDetail` view |
| `GET /knowledge/search` row present; `GET /investigations/{id}/knowledge` missing from Appendix A (it exists: `knowledge.py:32`) | `{investigation_id, code_issue_fingerprint, artifacts, sessions, ...}` | Both routes specified in §6 |
| Profiles Spring routes → `ProfileDetail` / undefined `SpringDriftView` / `DeriveFromSpringResponse` | Refresh returns `{changed, version[, diff, profile, warnings]}`; drift returns `{changed, version, diff, needs_review, warnings}`; derive-without-persist has **no `version` key** | Three dedicated views specified in §5; refresh is NOT `ProfileDetail` |

**Spec change:** the §6 mapping table and Appendix A use the verified shapes. General rule
(§3.4): no view is written from memory — each is built from the handler's return
statement plus a golden test, in that order.

### F3. `response_model_exclude_none=True` as a global default is NOT wire-preserving — rejected

The proposal (D3) mandates `exclude_none=True` on every route, claiming handlers
"already omit unset keys." Counterexamples from the handlers:

- `GET /reference-docs/search` emits `"next_cursor": null` explicitly on the last page
  (`reference_docs.py:108`). With `exclude_none`, the key vanishes — a wire change that
  breaks cursor clients distinguishing "no more pages" (`null`) from a malformed body.
- `GET /investigations/{id}/conclusion` emits `"conclusion": null` when unconcluded
  (`investigations.py:644`).
- `GET /investigations` list emits `"offset": 0, "limit": <n>` **always**
  (`investigations.py:605`) — while other list endpoints (e.g. clusters list) omit
  them. Neither global setting (`exclude_none=True` drops the first group's keys;
  `False` injects `"offset": null` into the second group's bodies) is safe.

**Implemented rule (§3.3):** `response_model_exclude_unset=True` on every JSON route,
plus a construction discipline with two halves: (a) optional keys that must stay
absent are omitted from constructors (e.g. `idempotent_replay` only on replay
paths); (b) response models are built from `model_dump(mode="json")` dicts or
fully-explicit kwargs — **never from sparsely-constructed domain objects**, whose
unset defaults `exclude_unset` would silently drop (found live during
implementation: a `BackgroundJob(tenant_id, kind)` re-emitted as a 2-key body).
Idempotency stores cache the dumped dict; replays re-validate through the model.

### F4. There are THREE wire error shapes, not two — the unification is larger than stated

- `{"error": {...}}` from the `DomainException` / validation handlers (`api/app.py`).
- `{"detail": {"code", "message"}}` from `HTTPException(detail=dict)` (profiles router
  `_err`, Spring error mapping — 11 sites, all profiles).
- `{"detail": "string"}` from plain `HTTPException(detail=str)` — ~185 sites across
  every router.

**Spec change (§4):** unification covers all three via one `ApiError` type + handler,
with per-router conversion. The proposal's migration step understates the work; §7
orders it so profiles (dict-details) convert first, string-details convert per router.
`X-Correlation-ID` response header is already set by middleware (`api/app.py:307`),
so the proposal's header assertion in error tests is valid as written.

### F5. D7 test location and app construction need correcting

- `tests/api/` does not exist. Existing layouts are `tests/unit/`, `tests/integration/`,
  `tests/contract/` (MCP contract tests). The guard belongs at
  `tests/unit/test_openapi_contract.py` (fast, no I/O) — not a new top-level directory.
- `from investigation_agent_platform.main import app` does import safely (config load
  is defensive with in-memory fallback, `main.py:23-31`), but test convention is
  `create_app()` from `api/app.py` with a fresh `AppContext`. The guard uses
  `create_app()` — no lifespan side effects, same route table.

### F6. Agreed without change: D1 (package, corrected per F1), D2 (envelopes), D4 (sanitized views), D6 (input/output split), D8 (keep operationIds), D9 (defer generation)

Two amendments ride along: D2's `CursorEnvelope.next_cursor` keeps `str | None`
**without** global exclusion (per F3); D4's `extra="forbid"` is required on
secret-bearing views but advisory elsewhere (see §3.5).

### F7. `responses={"model": ...}` syntax, generic component naming, alias defaults — agreed

`responses={404: {"model": ErrorResponse, ...}}` is valid FastAPI additional-responses
syntax; parametrized generics produce unique component names; `response_model_by_alias`
defaults to `True` (kept explicit per route for readability).

### F8. Idempotency replay + response_model interaction needs a general rule, not one example

The proposal notes the reindex replay path. The rule must cover all replay paths
(investigations create, profiles create/update, chat sessions, reference reindex):
the store caches `Model.model_dump(mode="json")` produced with the route's exact
exclusion setting, and replay returns that dict for FastAPI to re-validate. A replay
payload built with different exclusion than the route declares is a 500 on replay —
covered by a replay test per idempotent endpoint (§8).

## 3. Implementation rules

### 3.1 Package layout (`api/v1/schemas/`, one module per router module)

```text
__init__.py            # re-exports
envelopes.py           # ListEnvelope, ItemsEnvelope, CursorEnvelope
errors.py              # ErrorDetail, ErrorResponse, COMMON_ERROR_RESPONSES
profiles.py            # full profile views (§5)
reference_docs.py      # per proposal (field-verified, §5)
investigations.py      # includes ErrorIntakeAck (route lives in investigations.py)
evidence.py findings.py hypotheses.py timeline.py jobs.py clusters.py knowledge.py
chat.py capabilities.py aggregate_reports.py batch_intake.py health.py
```

Schemas import from domain/ports models only, never from routers.

### 3.2 Envelopes (exact shapes; exclusion pinned per route, never global)

```python
class ListEnvelope[T](BaseModel):
    items: list[T]
    total: int
    offset: int | None = None
    limit: int | None = None

class ItemsEnvelope[T](BaseModel):
    items: list[T]

class CursorEnvelope[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None   # explicit null preserved; see §2/F3
    has_more: bool = False
```

(PEP 695 generics: verified byte-identical schema output to `Generic[T]` on
Pydantic 2.13; satisfies `ruff UP046`.)

### 3.3 Route declaration rule

Every 2xx-with-content route declares `response_model=<concrete>`,
`response_model_by_alias=True`, `response_model_exclude_unset=True`, and
`responses=COMMON_ERROR_RESPONSES`. Non-JSON routes (SSE streams, 204s) declare
no JSON model; the guard skips responses without `application/json` content
(the chat SSE route additionally documents its `text/event-stream` content so
the spec states the real contract instead of FastAPI's default empty-JSON 200).

Two construction disciplines make `exclude_unset` wire-safe:

1. **Optional-but-absent keys are omitted from constructors** (e.g.
   `idempotent_replay` only on replay paths; `version` only on derive-persist).
2. **Response models are built from `model_dump(mode="json")` dicts or
   fully-explicit kwargs — never from sparsely-constructed domain objects.**
   Dict input marks every key set, so explicit nulls survive; a domain object
   built with defaulted fields would sparse-emit (observed live with
   `BackgroundJob(tenant_id, kind)` re-emitting as a 2-key body). Idempotency
   stores cache the dumped dict; replays re-validate through the model.

### 3.4 View construction order (mandatory)

1. Read the handler's return statement. 2. Capture its golden body. 3. Write the view
to match. Never write a view from a domain model alone — F2 shows why.

### 3.5 `extra` policy

- `extra="forbid"` **required** on any view of a secret-bearing resource (profiles,
  anything carrying connection material, tokens, or credentials). A new domain field
  without a view field fails loudly instead of leaking.
- Elsewhere `extra` defaults to ignore; drift is caught by the contract guard +
  golden tests without coupling every domain change to view churn.

### 3.6 Ack naming

One named ack per endpoint shape (`JobCancelResponse{job, cancelled}`,
`SessionDeleteAck{session_id, status}`, `BatchIntakeAck{job_id, status}`, ...).
No shared generic `AckResponse` across different shapes.

## 4. Error unification (the one wire change)

New `ApiError` in `api/errors.py`:

```python
class ApiError(Exception):
    def __init__(self, *, code: str, http_status: int, message: str | None = None,
                 details: dict[str, Any] | None = None) -> None:
        self.code = code
        self.http_status = http_status
        self.message = message or _PUBLIC_MESSAGES.get(code, "An internal error occurred.")
        self.details = details
```

Handler in `api/app.py` renders `ErrorResponse{error: {code, message, correlation_id,
details?}}` with `exclude_none=True`, status from `exc.http_status`, reusing the
request's `correlation_id` (header already echoed by middleware). The
`DomainException` handler keeps its code→status mapping but returns the same body;
`RequestValidationError`/`PydanticValidationError` handlers likewise (422,
`details: {"fields": [...]}`). Router conversions: profiles `_err` and Spring mapping
first (dict-details, 11 sites), then `HTTPException(detail=str)` sites per router in
§7 order. Until a router converts, its old shape stands — mixed shapes during rollout
are expected and the UI mapper already tolerates both.

Message policy for this pass: **envelope-only change — message texts preserved
verbatim** (including `str(exc)` diagnostics already on the wire), so no
caller-visible content changes ride along with the unification. `ApiError` also
carries optional `headers` (used for `Retry-After` passthrough on 429/503 paths).
Content hardening (static messages, diagnostic details) is open question 5.

`ErrorDetail`: `code` (stable machine code), `message` (stable, non-sensitive),
`correlation_id`, `details` (optional; only validation errors populate
`{"fields": [...]}`). `COMMON_ERROR_RESPONSES` documents 400/401/403/404/409/422
against `ErrorResponse` on every route.

## 5. Profiles views (fully specified — verified against the implementation)

Sanitization boundary is `ApplicationProfile.sanitized()` (template SQL →
`{description}`, connection_ref → `{present, scheme}`); `ProfileView.from_domain`
is the single conversion and `response_model=ProfileView` is safe precisely because
the view cannot express a secret. `ConnectionRefView{present, scheme}`,
`QueryTemplateSummary{description?}`, service/datasource/correlation/investigation
(`defaultTimeWindow` et al. aliases preserved verbatim)/knowledge/source/derivation/
`referenceDocs`/SLO/runbook views per the proposal's D4 text, which was verified
field-for-field against `domain/profile/models.py` — adopted as written, with
`extra="forbid"` throughout (secret-bearing resource, §3.5).

```python
class ProfileView(BaseModel):  # sanitized profile; extra="forbid", populate_by_name
    id: str; tenant_id: str; name: str; description: str; environment: str
    environments: list[str]; version: int; status: str; owner: str; owners: list[str]
    domain: str | None; planes: list[str]; data_classification: str | None
    allowed_capabilities: list[str]; tags: list[str]
    source: ProfileSourceView | None; derivation: DerivationReportView | None
    services: list[ServiceView]; datasources: list[DatasourceView]
    mapping_source_id: str; correlation: CorrelationConfigView
    investigation: InvestigationConfigView; knowledge: KnowledgeConfigView
    reference_docs: ReferenceDocsProfileView | None = Field(alias="referenceDocs")
    slos: list[SloView]; runbooks: list[RunbookView]; oncall: dict[str, Any] | None

    @classmethod
    def from_domain(cls, profile: ApplicationProfile) -> "ProfileView":
        return cls.model_validate(profile.sanitized())

class ProfileRevisionView(BaseModel):  # extra="forbid"
    id: str; version: int; status: str; name: str; environment: str

class ProfileDetail(BaseModel):  # GET detail, POST create, PUT update; extra="forbid"
    profile: ProfileView; version: int; idempotent_replay: bool | None = None

class SpringDerivationView(BaseModel):  # POST ...:derive-from-spring; extra="forbid"
    profile: ProfileView
    version: int | None = None          # absent when persist=false
    derivation: DerivationReportView
    persisted: bool
    warnings: list[str]

class SpringRefreshView(BaseModel):  # POST ...:refresh-from-spring; extra="forbid"
    changed: bool; version: int
    diff: list[str] | None = None
    profile: ProfileView | None = None
    warnings: list[str] | None = None

class SpringDriftView(BaseModel):  # GET ...:spring-drift; extra="forbid"
    changed: bool; version: int; diff: list[str]
    needs_review: list[str]; warnings: list[str]
```

Route bindings: `GET /profiles` → `ListEnvelope[ProfileView]`;
`GET /profiles/{app_id}` → `ProfileDetail`; `GET .../revisions` →
`ListEnvelope[ProfileRevisionView]`; `POST /profiles`, `PUT /profiles/{app_id}` →
`ProfileDetail` (replay dicts stored as `ProfileDetail.model_dump(mode="json")` per §2/F8).
`_sanitize` is deleted in favor of `from_domain`. Redaction tests per the proposal's
Testing Philosophy (secret-bearing profile → only `{present, scheme}`/`{description}`;
negative `extra="forbid"` test) are required, not optional.

## 6. Remaining routers (verified envelope shapes; views per §3.4 order)

| Router | Endpoint(s) | Verified wire shape → response model |
|---|---|---|
| investigations | `GET /investigations` | `{items[{investigation_id,status,application_id,created_at?,updated_at?}],total,offset,limit}` (offset/limit always emitted) → `ListEnvelope[InvestigationListItemView]` |
| investigations | `GET /investigations/{id}` | `{investigation, pending_requirements[]}` → `InvestigationDetailResponse` |
| investigations | `POST /investigations` (201) | `{id, session_id, application_id, status, tenant_id, investigation}` → `CreateInvestigationResponse` |
| investigations | `…/start`, `…/retry` (202) | `{investigation_id, status, workflow_id, correlation_id, message[, session_number, merged, code_issue_fingerprint]}` → `DispatchAck` |
| investigations | `GET …/conclusion` | `{…, status, conclusion|null}` (explicit null!) → `ConclusionResponse` |
| investigations | `GET …/knowledge` | `{investigation_id, code_issue_fingerprint, artifacts, sessions, ...}` → `KnowledgeResponse` (verify tail fields from handler during implementation) |
| investigations | `…/pause,resume,cancel,approve-action,fulfill`, `POST /intake/errors` (202) | per-endpoint named acks (§3.6) |
| evidence | `GET …/evidence`, `…/evidence/{id}` | `{items,total}` / `{evidence}` → `ListEnvelope[Evidence]`, `EvidenceDetail{evidence}` (domain `Evidence` as response model — no secrets) |
| findings | `GET …/findings` | `{items,total,offset,limit}` → `ListEnvelope[Finding]` (domain `Finding` as response model) |
| hypotheses | `GET …/hypotheses` | `{items,total,offset,limit}` → `ListEnvelope[Hypothesis]` (domain model as response model) |
| timeline | `GET …/timeline` | `{items,total,offset,limit}` → `ListEnvelope[TimelineEvent]` (domain model as response model) |
| jobs | `GET /jobs` | `{items,total,offset,limit}` → `ListEnvelope[BackgroundJob]` (domain model as response model) |
| jobs | `GET /jobs/{id}` | `{job}` → `JobDetail{job}` (NOT bare `JobView`) |
| jobs | `POST …/cancel` | `{job, cancelled}` → `JobCancelResponse` |
| clusters | `GET /clusters` | `{items,total}` → `ListEnvelope[FindingCluster]` (domain model as response model) |
| clusters | `GET /clusters/{id}` | `{cluster, assignments, findings, total, offset, limit}` → `ClusterDetail` (NOT bare `ClusterView`) |
| clusters | `POST /clusters/run` | `{job_id, workflow_id, status}` → `ClusterRunResponse` (verified; replay-tested) |
| knowledge | `GET /knowledge/search` | `{items,total}` → `ListEnvelope[KnowledgeArtifact]` (domain model as response model) |
| chat | `POST /chat/sessions` | `{session}` → `ChatSessionResponse` (verified; replay-tested) |
| chat | `GET …/messages` | `{items,total,offset,limit}` → `ListEnvelope[ChatMessage]` (domain model as response model) |
| chat | `POST …/messages` | SSE `text/event-stream` (out of scope; documented content type, no JSON model) |
| chat | `DELETE …` | `{session_id, status}` → `SessionDeleteAck` |
| capabilities | `GET /capabilities` | `{items,total}` → `ListEnvelope[CapabilityView]` |
| aggregate_reports | `GET /latest` | `{artifact, metadata, signed_url, expires_in_seconds}` → `ReportMetadataView` |
| aggregate_reports | `GET /history` | `{items, next_cursor, has_more}` cursor → `CursorEnvelope[ReportHistoryItemView]` (NOT `ListEnvelope`) |
| aggregate_reports | `POST`, `DELETE` | current dicts → `ReportBuildAck`, `ReportPurgeAck` |
| batch_intake | `POST`, `GET /{job}` | `{job, summary, items, total, offset, limit}` → `BatchIntakeDetail` (NOT `ListEnvelope` wrapper) |
| batch_intake | `…/cancel, …/retry, DELETE` | `{job_id, status}` → `BatchIntakeAck` |
| reference_docs | `GET /sources` | `{items}` → `ItemsEnvelope[ReferenceSourceView]` |
| reference_docs | `GET /search` | `{items, next_cursor|null, has_more}` → `CursorEnvelope[ReferenceSearchHitView]` (null preserved!) |
| reference_docs | `GET /status` | `IndexStatus` dump → `IndexStatusView` (fields verified: tenant_id, source_id, active_generation, source_revision, chunk_count, status) |
| reference_docs | `POST /rollback` | `IndexStatus` dump → `IndexStatusView` (the service returns the refreshed status, NOT the generation row — verified against `rollback_generation`; there is deliberately no `IndexGeneration` view) |
| reference_docs | `POST /reindex` | `{job_id, workflow_id, status}` → `ReindexAck` (verified; replay-tested) |
| reference_docs | `DELETE …` | `{source_id, status, rows_removed}` → `SourceDeleteAck` |
| health | `GET /live`, `/ready` | `{status[, dependencies]}` → `HealthView` |
| events | pause/resume/approve-action | existing dicts → named acks per §3.6 |

### 3.7 Domain models as response models (non-secret resources only)

Where a handler returns `model.model_dump(mode="json")` and the model carries no
secret-bearing fields (audited: `Investigation`, `Finding`, `Hypothesis`,
`TimelineEvent`, `Evidence`, `BackgroundJob`, `KnowledgeArtifact`,
`InvestigationChatSession`, `ChatMessage`, `FindingCluster`,
`FindingClusterAssignment`, `InputRequirement`, `InvestigationConclusion`,
`BatchIntakeRecord`, `BatchRecordResult`, `BatchIntakeResult`, `ArtifactRef`),
the domain model itself is the `response_model`. Wire-identical by construction
with no view/domain drift class. Secret-bearing resources always get explicit
sanitized projections (§3.5); hand-built dict shapes always get dedicated views.

All rows above were resolved via §3.4 (return statement → golden → view) during
implementation. Two review-time corrections are recorded: rollback returns
`IndexStatus` (not `IndexGeneration` — no such view exists), and purge returns
`purged: list[str]` (keys, not a count).

## 7. Rollout (ordered, additive; golden-first)

1. Add `schemas/{envelopes,errors}.py` + contract guard
   (`tests/unit/test_openapi_contract.py`). Implemented as a **ratchet**, not an
   initially-failing test: `UNTYPED_ALLOWLIST` enumerates the known-untyped routes
   (55 at inception) and the guard fails both on new untyped routes and on
   allowlisted routes that gain types without leaving the list. Non-JSON routes
   (SSE) are exempt by explicit `SSE_EXEMPT` membership with rationale.
2. Error unification (§4): `ApiError` + handler + `COMMON_ERROR_RESPONSES`. Convert
   profiles router first (11 dict-detail sites), then per-router string-detail sites
   in step-4 order. Only wire change in the program; coordinate with UI.
3. Profiles + reference-docs (worked references, §5 + verified views).
4. Remaining routers: investigations → evidence/findings/hypotheses/timeline →
   jobs → clusters → knowledge → chat → capabilities → aggregate_reports →
   batch_intake → events → health. Per route: golden test, then view, then
   `response_model`; each step keeps the suite green.
5. Regenerate UI snapshot + generated types; UI generator switch stays a separate
   follow-up (proposal D9, agreed).

## 8. Acceptance criteria

(i) `tests/unit/test_openapi_contract.py` passes: every 2xx-with-content response
references a component schema, and no 2xx schema is an open `additionalProperties`
object. (ii) A golden wire test per endpoint pins the pre-change body byte-for-byte;
the suite passes with `response_model`s enabled. (iii) Redaction tests prove
`ProfileView` never emits connection values or template SQL, and `extra="forbid"`
rejects widening. (iv) Error tests prove the single `{"error": {...}}` body and the
`X-Correlation-ID` match for 400/404/409/422. (v) Every idempotent endpoint has a
replay test through `response_model` validation. (vi) A generation smoke test runs
the UI generator against the built spec and exits 0.

## 9. Open questions (for review, not implementation)

1. Per-response validation cost on hot reads (measure first; custom route class
   to keep schema-but-skip-validation only if benchmarks justify).
2. `generate_unique_id_function` for cleaner client method names — deferred, tracked
   separately (proposal D8, agreed).
3. Whether `has_more`-style always-emitted booleans should lose their model defaults
   to force explicit construction (strictness vs convenience).
4. Golden-test storage format (inline literals chosen; fixtures directory optional
   later — literals won for grep-ability during implementation).
5. Error-message content hardening: `str(exc)` diagnostics preserved verbatim in this
   pass (envelope-only change). Follow-up: static messages + structured `details`
   for caller-actionable errors (fulfill schema/delivery, concurrency).

## Appendix A — Corrected 55-endpoint inventory

| Method | Path | Response model |
|---|---|---|
| `DELETE` | `/api/v1/aggregate-reports` | `ReportPurgeAck` |
| `POST` | `/api/v1/aggregate-reports` | `ReportBuildAck` |
| `GET` | `/api/v1/aggregate-reports/history` | `CursorEnvelope[ReportHistoryItemView]` |
| `GET` | `/api/v1/aggregate-reports/latest` | `ReportMetadataView` |
| `POST` | `/api/v1/batch-intake` | `BatchIntakeDetail` |
| `DELETE` | `/api/v1/batch-intake/{job_id}` | `BatchIntakeAck` |
| `GET` | `/api/v1/batch-intake/{job_id}` | `BatchIntakeDetail` |
| `POST` | `/api/v1/batch-intake/{job_id}/cancel` | `BatchIntakeAck` |
| `POST` | `/api/v1/batch-intake/{job_id}/retry` | `BatchIntakeAck` |
| `GET` | `/api/v1/capabilities` | `ListEnvelope[CapabilityView]` |
| `POST` | `/api/v1/chat/sessions` | `ChatSessionResponse` |
| `DELETE` | `/api/v1/chat/sessions/{session_id}` | `SessionDeleteAck` |
| `GET` | `/api/v1/chat/sessions/{session_id}/messages` | `ListEnvelope[ChatMessage]` |
| `POST` | `/api/v1/chat/sessions/{session_id}/messages` | SSE `text/event-stream` (no JSON model) |
| `GET` | `/api/v1/clusters` | `ListEnvelope[FindingCluster]` |
| `POST` | `/api/v1/clusters/run` | `ClusterRunResponse` |
| `GET` | `/api/v1/clusters/{cluster_id}` | `ClusterDetail` |
| `GET` | `/api/v1/health/live` | `HealthView` |
| `GET` | `/api/v1/health/ready` | `HealthView` |
| `POST` | `/api/v1/intake/errors` | `ErrorIntakeAck` |
| `GET` | `/api/v1/investigations` | `ListEnvelope[InvestigationListItemView]` |
| `POST` | `/api/v1/investigations` | `CreateInvestigationResponse` |
| `GET` | `/api/v1/investigations/{investigation_id}` | `InvestigationDetailResponse` |
| `POST` | `/api/v1/investigations/{investigation_id}/approve-action` | `ApproveActionAck` |
| `POST` | `/api/v1/investigations/{investigation_id}/cancel` | `CancelAck` |
| `GET` | `/api/v1/investigations/{investigation_id}/conclusion` | `ConclusionResponse` |
| `GET` | `/api/v1/investigations/{investigation_id}/evidence` | `ListEnvelope[Evidence]` |
| `GET` | `/api/v1/investigations/{investigation_id}/evidence/{evidence_id}` | `EvidenceDetail` |
| `GET` | `/api/v1/investigations/{investigation_id}/findings` | `ListEnvelope[Finding]` |
| `GET` | `/api/v1/investigations/{investigation_id}/hypotheses` | `ListEnvelope[Hypothesis]` |
| `POST` | `/api/v1/investigations/{investigation_id}/input-requirements/{requirement_id}/fulfill` | `FulfillAck` |
| `GET` | `/api/v1/investigations/{investigation_id}/knowledge` | `KnowledgeResponse` |
| `POST` | `/api/v1/investigations/{investigation_id}/pause` | `PauseAck` |
| `POST` | `/api/v1/investigations/{investigation_id}/resume` | `ResumeAck` |
| `POST` | `/api/v1/investigations/{investigation_id}/retry` | `DispatchAck` |
| `POST` | `/api/v1/investigations/{investigation_id}/start` | `DispatchAck` |
| `GET` | `/api/v1/investigations/{investigation_id}/timeline` | `ListEnvelope[TimelineEvent]` |
| `GET` | `/api/v1/jobs` | `ListEnvelope[BackgroundJob]` |
| `GET` | `/api/v1/jobs/{job_id}` | `JobDetail` |
| `POST` | `/api/v1/jobs/{job_id}/cancel` | `JobCancelResponse` |
| `GET` | `/api/v1/knowledge/search` | `ListEnvelope[KnowledgeArtifact]` |
| `GET` | `/api/v1/profiles` | `ListEnvelope[ProfileView]` |
| `POST` | `/api/v1/profiles` | `ProfileDetail` |
| `GET` | `/api/v1/profiles/{app_id}` | `ProfileDetail` |
| `PUT` | `/api/v1/profiles/{app_id}` | `ProfileDetail` |
| `DELETE` | `/api/v1/profiles/{app_id}` | `ProfileDeleteAck{id, version?, deleted}` (whole-app only when all revisions DRAFT, else single `?version=N` DRAFT revision) |
| `GET` | `/api/v1/profiles/{app_id}/revisions` | `ListEnvelope[ProfileRevisionView]` |
| `POST` | `/api/v1/profiles/{app_id}:refresh-from-spring` | `SpringRefreshView` |
| `GET` | `/api/v1/profiles/{app_id}:spring-drift` | `SpringDriftView` |
| `POST` | `/api/v1/profiles:derive-from-spring` | `SpringDerivationView` |
| `POST` | `/api/v1/reference-docs/reindex` | `ReindexAck` |
| `POST` | `/api/v1/reference-docs/rollback` | `IndexStatusView` |
| `GET` | `/api/v1/reference-docs/search` | `CursorEnvelope[ReferenceSearchHitView]` |
| `GET` | `/api/v1/reference-docs/sources` | `ItemsEnvelope[ReferenceSourceView]` |
| `DELETE` | `/api/v1/reference-docs/sources/{source_id}` | `SourceDeleteAck` |
| `GET` | `/api/v1/reference-docs/status` | `IndexStatusView` |

## Appendix B — Wire-compat traps (normative for implementers)

1. **Explicit nulls are data.** `next_cursor: null`, `conclusion: null` are emitted
   today; no global `exclude_none`. Pin per route with golden tests.
2. **Always-emitted pagination echoes.** `GET /investigations` always sends
   `offset`/`limit`; other lists omit them. Views share the envelope; exclusion is
   per route.
3. **Idempotency caches must match route exclusion.** Store
   `Model.model_dump(mode="json")` under the route's exclusion setting; replay tests
   go through `response_model` validation.
4. **Aliases serialize by alias.** Keep `response_model_by_alias=True` explicit;
   golden tests use alias keys (`referenceDocs`, `defaultTimeWindow`, ...).
5. **UUID/datetime serialize as strings.** Views declare `UUID`/`datetime` types and
   let FastAPI serialize; golden tests compare the string forms.
6. **No domain model with secrets as a `response_model`, ever.** Code review must
   reject it; the redaction tests are the backstop.
