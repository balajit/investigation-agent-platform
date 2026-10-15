# Design: OpenAPI-typed request and response models for the backend API

Convert the FastAPI surface from untyped `dict[str, Any]` handlers to declared Pydantic `response_model`s across all 55 endpoints, plus shared envelope generics and a documented error envelope, so `openapi.json` carries every request and response shape and clients can generate types (and, optionally, runtime validators) from it. Scope is the API layer only — no persistence, behavior, or auth changes.

## Context

The backend is FastAPI (`fastapi>=0.141.1`) on Pydantic v2 (`pydantic>=2.0.0`). It has 16 routers under `src/investigation_agent_platform/api/v1/routers/` exposing 55 path operations. Every handler is annotated `-> dict[str, Any]` and none declare `response_model`.

Verified against the running server (`GET /openapi.json`) and the source tree:

- `rg response_model src/` returns **zero** matches. No route declares an output model.
- All **55** successful (2xx) responses are emitted as `{"type": "object", "additionalProperties": true}`. Not one response references a `#/components/schemas/...` type.
- `components.schemas` contains **15** entries, and all 15 are *request* bodies or shared enums/errors: `BackgroundJobStatus`, `BatchIntakeBody`, `BatchIntakeRecord`, `BuildReportBody`, `CreateInvestigationBody`, `CreateSessionBody`, `CsvPayload`, `ErrorIntakeBody`, `FulfillInputBody`, `HTTPValidationError`, `PostMessageBody`, `ReindexBody`, `RollbackBody`, `RunClustersBody`, `ValidationError`. There is no `ApplicationProfile`, `Investigation`, or any response view model.
- The live error envelope is produced by exception handlers in `api/app.py`: `{"error": {"code", "message", "correlation_id"[,"details"]}}` (lines ~327–427). The spec nonetheless advertises `HTTPValidationError` for 422 — a documented-vs-actual divergence.
- Handlers raise `HTTPException(status_code, detail={"code":..., "message":...})` in places (e.g. `profiles.py::_err`), which FastAPI renders as `{"detail": {...}}` — a **second** wire error shape distinct from the `{"error": {...}}` envelope.

The UI consumes the spec via `npx openapi-typescript` (`npm run generate:api-types`), which emits **types only**. Because the spec has no response shapes, runtime validation is hand-written Zod in `src/api/schemas.ts`, verified against the Pydantic source and live probes. This gap has produced real incidents:

- `GET /reference-docs/sources` and `/reference-docs/search` changed from `{items,total}` to `{items}` and `{items,next_cursor,has_more}` respectively; the UI's shared list parser (which required `total`) threw at runtime until fixed.
- The `ApplicationProfile` domain model was rewritten from a flat shape (`observability`/`state`/`code`) to a service-oriented shape (`services[]`/`datasources[]`/`mapping_source_id`); the UI detail panels silently rendered empty until realigned.

The UI-side contract check (`contracts/check-contract.sh`) compares only paths, methods, `operationId`s, and schema *names*. It cannot see body shapes, so response-shape drift passes it undetected.

## References

- [FastAPI — Generating SDKs](https://fastapi.tiangolo.com/advanced/generate-clients/) — Confirms that declaring response models puts their schemas in OpenAPI and enables TypeScript SDK generation; FastAPI emits OpenAPI **3.1**, so generators must support 3.1.
- [FastAPI — Response Model, Return Type](https://fastapi.tiangolo.com/tutorial/response-model/) — Defines `response_model`, `response_model_exclude_none`, and `response_model_by_alias` semantics used below.
- [FastAPI — Extra Models](https://fastapi.tiangolo.com/tutorial/extra-models/) — Input/output model separation.
- [FastAPI — Additional Responses in OpenAPI](https://fastapi.tiangolo.com/advanced/additional-responses/) — Documents non-200 responses via `responses={...}`.
- [FastAPI — Separate OpenAPI Schemas for Input and Output](https://fastapi.tiangolo.com/how-to/separate-openapi-schemas/) — The `-Input`/`-Output` schema split behavior.
- [Pydantic v2 — Models](https://docs.pydantic.dev/latest/concepts/models/) — `BaseModel`, `ConfigDict`, and generic models (`Generic[T]`).
- [OpenAPI Specification 3.1.0](https://spec.openapis.org/oas/v3.1.0) — Component schema and response object contract.
- [openapi-typescript](https://openapi-ts.dev/) — The tool the UI uses today to generate `src/types/api-generated.ts` (types only).
- [Hey API (`@hey-api/openapi-ts`)](https://heyapi.dev/) — FastAPI-recommended TypeScript SDK generator; candidate for a runtime-validator plugin.
- [Orval](https://orval.dev/) — Alternative client + Zod generator.
- [openapi-zod-client](https://github.com/astahmer/openapi-zod-client) — Generates runtime Zod schemas from an OpenAPI document.
- [Azure API Guidelines — Errors](https://github.com/microsoft/api-guidelines/blob/vNext/azure/Guidelines.md#handling-errors) — Reference for a single consistent error envelope.
- UI-side context: `docs/ui-api-matrix.md`, `docs/ui-backend-contract-gaps.md` (D16, D19), `docs/ui-architecture.md`.

## Goals / Non-Goals

**Goals:**

- Every one of the 55 endpoints declares a concrete Pydantic `response_model` for its 2xx responses.
- Shared generic envelope models: `ListEnvelope[T]`, `ItemsEnvelope[T]`, `CursorEnvelope[T]`, and single-item wrappers.
- A documented, single error envelope: `ErrorResponse { error: ErrorDetail }`, referenced from every route's `responses`.
- Sanitized **view** models for anything that currently redacts data (`ApplicationProfile` → `ProfileView`), so declaring a response model never re-leaks secrets.
- `components.schemas` in `openapi.json` contains every request and response shape, making generation possible.
- Wire output stays byte-compatible with today's responses (no field renames, no type changes) except the deliberate error-envelope unification in D5.
- A CI guard that fails if any 2xx response is untyped, so the gap cannot silently return.

**Non-Goals:**

- No database or migration changes.
- No new endpoints, fields, or behavior. No auth, tenancy, or rate-limit changes.
- Not implementing the client generator here — the UI-side generation switch is specified (D9) but executed as a follow-up change in the UI repo.
- Not renaming existing wire fields except the error body (D5).
- Not typing the SSE/WebSocket stream protocol (`/jobs/{id}/stream`, chat streaming); those keep their current contract.

## Decisions

### D1: Response schemas live in a dedicated `api/v1/schemas/` package, one module per router

**Decision:** All response view models and envelopes live in a new package, mirroring the router layout one-to-one. Routers import from it; the package imports nothing from routers (no cycles).

```text
src/investigation_agent_platform/api/v1/schemas/
    __init__.py            # re-exports for convenience
    envelopes.py           # ListEnvelope, ItemsEnvelope, CursorEnvelope
    errors.py              # ErrorDetail, ErrorResponse, COMMON_ERROR_RESPONSES
    profiles.py            # ProfileView, ProfileDetail, ProfileRevisionView, ProfileSourceView, DerivationReportView
    reference_docs.py      # ReferenceSourceView, IndexStatusView, IndexGenerationView, ReferenceSearchHitView, ReindexAck, SourceDeleteAck
    investigations.py      # InvestigationListItemView, InvestigationDetailResponse, ConclusionResponse, KnowledgeResponse
    evidence.py            # EvidenceItemView
    findings.py            # FindingView
    hypotheses.py          # HypothesisView
    timeline.py            # TimelineEventView
    jobs.py                # JobView, JobCancelResponse
    clusters.py            # ClusterView, ClusterRunResponse
    knowledge.py           # KnowledgeArtifactView
    chat.py                # ChatSessionResponse, ChatMessageView
    capabilities.py        # CapabilityView
    aggregate_reports.py   # ReportMetadataView, ReportHistoryItemView, ReportBuildAck
    batch_intake.py        # BatchIntakeView, BatchIntakeAck
    intake.py              # ErrorIntakeAck
    health.py              # HealthView
```

Each module carries a one-line docstring naming the router it serves.

**Alternative considered:** Define view models inline in each router file. Rejected — routers already carry request bodies and dependencies; a parallel package keeps the response contract discoverable in one place and makes the D7 CI guard's ownership obvious.

### D2: Shared generic envelopes

**Decision:** Three generic envelopes cover every list shape currently emitted. `offset`/`limit` are `None`-defaulted so a single `ListEnvelope[T]` types both paginated endpoints (`{items,total,offset,limit}`) and non-paginated ones (`{items,total}`). Serialization drops the `None`s via `response_model_exclude_none=True` per route (D3), preserving the exact current wire shape.

```python
# src/investigation_agent_platform/api/v1/schemas/envelopes.py
"""Shared response envelopes. Shapes match what handlers emit today."""
from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ListEnvelope(BaseModel, Generic[T]):
    """`{items, total[, offset, limit]}` — paginated and non-paginated lists."""

    items: list[T] = Field(description="Page of results.")
    total: int = Field(ge=0, description="Total rows available, not just this page.")
    offset: int | None = Field(
        default=None,
        ge=0,
        description="Offset echo. Present only for endpoints that paginate.",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        description="Limit echo. Present only for endpoints that paginate.",
    )


class ItemsEnvelope(BaseModel, Generic[T]):
    """`{items}` — a bare list with no total (e.g. reference-docs/sources)."""

    items: list[T] = Field(description="All results.")


class CursorEnvelope(BaseModel, Generic[T]):
    """`{items, next_cursor, has_more}` — opaque cursor pagination."""

    items: list[T] = Field(description="Page of results.")
    next_cursor: str | None = Field(
        default=None,
        description="Opaque continuation token. `null` on the last page.",
    )
    has_more: bool = Field(
        default=False,
        description="True when `next_cursor` is non-null.",
    )
```

`ListEnvelope[InvestigationListItemView]` etc. become concrete component schemas automatically (FastAPI derives a unique schema name per parametrization, e.g. `ListEnvelope_InvestigationListItemView_`).

**Alternative considered:** A `Paginated` mixin with plain inheritance. Rejected — Pydantic v2 parametrized generics produce cleaner, self-describing component names and enforce the item type end to end.

### D3: Declare `response_model` on every 2xx; preserve the wire exactly

**Decision:** Each route gets `response_model=<concrete type>`, `response_model_exclude_none=True` (so optional `offset`/`limit` and null optionals match today's payload — the handlers already omit unset keys), and the default `response_model_by_alias=True` (Pydantic v2 default; required because `ProfileView` and others preserve aliases like `referenceDocs`).

**Alternative considered:** `response_model_exclude_unset=True`. Rejected — it depends on which fields were set during construction and is fragile when models are built via `model_validate`; `exclude_none` is deterministic and matches the "omit absent" intent.

**Trade-offs:** FastAPI re-validates and re-serializes every response through the model. This adds a small per-response cost; measure before enabling broadly (see Risks). If needed, `response_model` can be kept for the OpenAPI schema while validation is relaxed in production via a custom route class — deferred.

### D4: Sanitized view models — a response model must never widen what is returned

**Decision:** Where a domain model can contain secrets, the response model is an explicit *sanitized projection*, not the domain model. `ApplicationProfile.sanitized()` already redacts `connection_ref` to `{present, scheme}` and `query_templates` to `{description}`; `ProfileView` encodes exactly that contract.

```python
# src/investigation_agent_platform/api/v1/schemas/profiles.py
"""Response models for the profiles router (appl-profile 1.0 §5).

The wire shape is the SANITIZED profile: no connection values, no template SQL.
ProfileView.from_domain() is the single conversion point; declaring
response_model=ProfileView is safe precisely because it cannot express a secret.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.profile.models import ApplicationProfile


class ConnectionRefView(BaseModel):
    """Redacted connection reference. NEVER the raw `secret://`/`env://` value."""

    model_config = ConfigDict(extra="forbid")

    present: bool = Field(description="Whether a connection reference is configured.")
    scheme: str = Field(description="Reference scheme, e.g. 'secret' or 'env'.")


class QueryTemplateSummary(BaseModel):
    """Redacted query template: description only, NEVER the SQL body."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(default=None, description="Human description; SQL is never returned.")


class ServiceTelemetryView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    log_indices: list[str]
    service_names: list[str]
    trace_backend: str | None
    metrics_backend: str | None
    default_lookback_s: int | None


class ServiceCodeView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org: str | None
    repos: list[str]
    default_branch: str
    language: str | None
    source_roots: list[str]
    build_system: str | None
    module_structure: str | None
    paths: list[str]


class ServiceDependencyView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    type: str
    critical: bool


class ServiceView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    role: str | None
    description: str | None
    telemetry: ServiceTelemetryView
    code: ServiceCodeView
    dependencies: list[ServiceDependencyView]
    endpoints: list[str]
    repository_id: str | None


class DatasourceView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    kind: str
    role: str
    description: str | None
    connection_ref: ConnectionRefView | None
    read_only: bool
    allowed_tables: list[str]
    query_templates: dict[str, QueryTemplateSummary]
    mcp_server: str | None
    tenants: list[str]


class CorrelationConfigView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: list[str]
    priority: list[str]


class InvestigationConfigView(BaseModel):
    """Aliased field names are the wire contract — keep them verbatim."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    default_time_window: int = Field(alias="defaultTimeWindow")
    max_evidence_per_query: int = Field(alias="maximumEvidencePerQuery")
    max_investigation_duration: int = Field(alias="maximumInvestigationDuration")
    enabled_evidence_types: list[str] = Field(alias="enabledEvidenceTypes")
    correlation_depth: int = Field(alias="correlationDepth")
    max_hypotheses: int = Field(alias="maximumHypotheses")
    evidence_budget: dict[str, int] | None = None
    parameters_schema: dict[str, Any] | None = Field(default=None, alias="parametersSchema")


class KnowledgeConfigView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reuse_scope: str
    retention_days: int
    min_confidence: float
    enable_cross_tenant_fingerprints: bool


class ProfileSourceView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    server_url: str
    spring_application: str
    spring_profile: str
    label: str | None = None
    fetched_at: str | None = None
    refresh_cadence: str | None = None


class DerivationReportView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fetched_at: str
    property_sources: list[str]
    property_count: int
    unmapped_keys: list[str]
    needs_review: list[str]
    dropped_sensitive_keys: list[str]
    notes: list[str]


class ReferenceDocsProfileView(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    config_version: str = Field(alias="configVersion")
    sources: list[dict[str, Any]]
    allowed_hosts: list[str] = Field(alias="allowedHosts")
    local_allowed_roots: list[str] = Field(alias="localAllowedRoots")
    embedding_model: str = Field(alias="embeddingModel")


class SloView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    target: float
    window: str
    objective: str | None


class RunbookView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    url: str | None
    path: str | None
    type: str
    ref: str | None


class ProfileView(BaseModel):
    """Sanitized ApplicationProfile — the ONLY profile shape that may cross the wire.

    `extra="forbid"` is deliberate: `from_domain` validates `sanitized()` output,
    so any future field added to the domain model without a matching view field
    fails loudly here rather than leaking silently.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    tenant_id: str
    name: str
    description: str
    environment: str
    environments: list[str]
    version: int
    status: str
    owner: str
    owners: list[str]
    domain: str | None
    planes: list[str]
    data_classification: str | None
    allowed_capabilities: list[str]
    tags: list[str]
    source: ProfileSourceView | None
    derivation: DerivationReportView | None
    services: list[ServiceView]
    datasources: list[DatasourceView]
    mapping_source_id: str
    correlation: CorrelationConfigView
    investigation: InvestigationConfigView
    knowledge: KnowledgeConfigView
    reference_docs: ReferenceDocsProfileView | None = Field(alias="referenceDocs")
    slos: list[SloView]
    runbooks: list[RunbookView]
    oncall: dict[str, Any] | None

    @classmethod
    def from_domain(cls, profile: ApplicationProfile) -> "ProfileView":
        """The single conversion point. Reuses the domain redaction verbatim."""
        return cls.model_validate(profile.sanitized())


class ProfileRevisionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: int
    status: str
    name: str | None
    environment: str | None


class ProfileDetail(BaseModel):
    """`{profile, version}` — GET/POST/PUT single-profile responses."""

    model_config = ConfigDict(extra="forbid")

    profile: ProfileView
    version: int
    idempotent_replay: bool | None = None
```

`ProfileView.from_domain` replaces the router-local `_sanitize`. Because `sanitized()` is already the redaction boundary, the view adds no new trust assumption.

**Alternative considered:** `response_model=ApplicationProfile` on the routes. Rejected — FastAPI would serialize the *full* domain model, emitting `connection_ref` values and template SQL. That is a security regression, not a typing change.

**Trade-offs:** `extra="forbid"` means adding a domain field requires updating the view in the same change. This is intentional (drift is caught at test time) but must be documented for the team.

### D5: One documented error envelope, referenced from every route

**Decision:** Standardize every non-2xx body on `{"error": {code, message, correlation_id, details?}}` and reference `ErrorResponse` from each route via a shared `responses` constant. This unifies today's two shapes (`{"error": {...}}` from the exception handlers and `{"detail": {...}}` from `HTTPException(detail=dict)`).

```python
# src/investigation_agent_platform/api/v1/schemas/errors.py
"""The single public error envelope (F-062/F-063)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    code: str = Field(description="Stable machine code, e.g. NOT_FOUND, ValidationError, CONFLICT.")
    message: str = Field(description="Stable, non-sensitive, human-readable message.")
    correlation_id: str = Field(description="Server correlation id; also sent as X-Correlation-ID header.")
    details: dict[str, Any] | None = Field(
        default=None,
        description="Optional structured detail. Only validation errors populate this ({'fields': [...]}).",
    )


class ErrorResponse(BaseModel):
    error: ErrorDetail


# Attach to every path operation: responses=COMMON_ERROR_RESPONSES
COMMON_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Bad request."},
    401: {"model": ErrorResponse, "description": "Unauthorized."},
    403: {"model": ErrorResponse, "description": "Forbidden."},
    404: {"model": ErrorResponse, "description": "Resource not found."},
    409: {"model": ErrorResponse, "description": "Conflict (concurrency or idempotency)."},
    422: {"model": ErrorResponse, "description": "Validation error."},
}
```

Introduce a single exception type and handler so every error path emits the same body:

```python
# src/investigation_agent_platform/api/errors.py  (additions)
from typing import Any


class ApiError(Exception):
    """The one HTTP error type. Handlers render it as ErrorResponse."""

    def __init__(
        self,
        *,
        code: str,
        http_status: int,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.http_status = http_status
        self.message = message or _PUBLIC_MESSAGES.get(code, "An internal error occurred.")
        self.details = details
```

```python
# src/investigation_agent_platform/api/app.py  (replace the DomainException / validation handlers' bodies)
from investigation_agent_platform.api.errors import ApiError
from investigation_agent_platform.api.v1.schemas.errors import ErrorDetail, ErrorResponse


def _error_response(correlation_id: str, *, code: str, message: str, details: dict[str, Any] | None = None) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorDetail(code=code, message=message, correlation_id=correlation_id, details=details)
    )
    return JSONResponse(status_code=0, content=body.model_dump(exclude_none=True))  # status set by caller


@app.exception_handler(ApiError)
async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
    cid = getattr(request.state, "correlation_id", "unknown")
    body = ErrorResponse(
        error=ErrorDetail(code=exc.code, message=exc.message, correlation_id=cid, details=exc.details)
    )
    return JSONResponse(status_code=exc.http_status, content=body.model_dump(exclude_none=True))


@app.exception_handler(RequestValidationError)
async def handle_validation_exception(request: Request, exc: RequestValidationError) -> JSONResponse:
    cid = getattr(request.state, "correlation_id", "unknown")
    body = ErrorResponse(
        error=ErrorDetail(
            code="ValidationError",
            message="Invalid request parameters or payload",
            correlation_id=cid,
            details=sanitize_extra({"fields": exc.errors()}),
        )
    )
    return JSONResponse(status_code=422, content=body.model_dump(exclude_none=True))
```

The `DomainException` handler keeps its code→status mapping but returns the same `ErrorResponse` body (its current body already matches). Router helpers like `profiles.py::_err` change from raising `HTTPException(detail={"code","message"})` to raising `ApiError(...)`.

**Alternative considered:** Keep `HTTPException(detail=dict)` and document both shapes. Rejected — two error shapes force every client to branch, and the spec can only document one per status. Unifying is a one-time wire change the UI error mapper already tolerates.

**Trade-offs:** This **is** a wire change for endpoints that currently return `{"detail": {...}}` (a strict improvement, but it must be called out to consumers). Endpoints already returning `{"error": {...}}` are unchanged. Coordinate the rollout with the UI (see Migrations).

### D6: Input and output models are distinct types

**Decision:** Never reuse a domain model as both request and response. Request bodies keep their existing `*Body` models; responses get the new `*View` models. Where a resource round-trips, the `*View` is what GET returns and the `*Body` is what POST/PUT accepts — they are allowed to differ.

**Alternative considered:** One shared model per resource. Rejected — the sanitized read shape and the accepted write shape genuinely differ (e.g. `ProfileView` vs the profile write body), and conflating them would either over-accept writes or over-expose reads.

### D7: CI guard — fail on any untyped 2xx response

**Decision:** Add a backend test that walks `app.openapi()` and asserts every 2xx response that carries content references a schema. This closes the loophole that let the reference-docs and profile shape changes pass unnoticed.

```python
# tests/api/test_openapi_contract.py
"""Guard: every success response must be typed (no additionalProperties blobs)."""
from __future__ import annotations

from investigation_agent_platform.main import app


def _is_typed(schema: dict) -> bool:
    if not isinstance(schema, dict):
        return False
    if "$ref" in schema:
        return True
    if schema.get("type") == "array":
        return "$ref" in (schema.get("items") or {})
    if "anyOf" in schema or "allOf" in schema or "oneOf" in schema:
        return True
    return False


def test_every_success_response_is_typed() -> None:
    spec = app.openapi()
    untyped: list[str] = []
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            for code, resp in (op.get("responses") or {}).items():
                if not str(code).startswith("2"):
                    continue
                content = resp.get("content") or {}
                if not content:
                    continue  # e.g. 204 No Content
                schema = (content.get("application/json") or {}).get("schema", {})
                if not _is_typed(schema):
                    untyped.append(f"{method.upper()} {path} -> {code}")
    assert not untyped, "Untyped success responses:\n" + "\n".join(untyped)


def test_no_response_schema_is_an_open_object() -> None:
    spec = app.openapi()
    offenders: list[str] = []
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            for code, resp in (op.get("responses") or {}).items():
                if not str(code).startswith("2"):
                    continue
                schema = ((resp.get("content") or {}).get("application/json") or {}).get("schema", {})
                if schema.get("additionalProperties") is True and "properties" not in schema:
                    offenders.append(f"{method.upper()} {path} -> {code}")
    assert not offenders, "Open-object success responses:\n" + "\n".join(offenders)
```

**Alternative considered:** Extend the UI's `contracts/check-contract.sh` instead. Rejected as the primary guard — the backend must own its contract; the UI check remains as a secondary snapshot diff.

### D8: Keep current `operationId`s (defer renaming)

**Decision:** Leave FastAPI's default `operationId` generation in place. The UI's generated types and `contracts/openapi.snapshot.json` key off these ids; renaming them is an unrelated breaking change. A future change may add `generate_unique_id_function` for cleaner client method names (per the FastAPI SDK guide), tracked separately.

**Alternative considered:** Adopt `generate_unique_id_function` now. Rejected — out of scope and forces a coordinated snapshot regeneration with no contract benefit.

### D9: Client generation becomes possible — specified here, executed in the UI repo

**Decision:** Once D1–D7 land, the UI switches from "types-only + hand-written Zod" to generated runtime schemas. Recommended path: keep `openapi-typescript` for compile-time types **and** add a runtime-schema generator (evaluate `@hey-api/openapi-ts` — the FastAPI-recommended tool — or `openapi-zod-client`/Orval) as a follow-up UI change. The backend deliverable that unblocks it is a rich `openapi.json`; this design does not change the UI toolchain itself.

**Alternative considered:** Generate both types and validators from OpenAPI immediately as part of this change. Rejected — that is a separate repo's change with its own review; sequencing it after the backend is typed avoids churn.

**Trade-offs:** Until the UI generator lands, the hand-written Zod remains and the backend must not silently drift; the D7 guard plus golden wire tests (Testing Philosophy) cover that interim.

---

## Data Storage

No `docs/dev/architecture.md` exists in the backend project (only an archived copy under `docs/archive/`). No persistence structures are introduced, modified, or migrated by this design. Nothing to specify here.

## Data Structures

### Envelopes and errors

Defined in full in D2 (`envelopes.py`) and D5 (`errors.py`).

### Profiles (reference implementation)

Defined in full in D4 (`profiles.py`): `ProfileView`, `ProfileDetail`, `ProfileRevisionView`, and the nested service/datasource/config views.

### Reference docs (reference implementation)

```python
# src/investigation_agent_platform/api/v1/schemas/reference_docs.py
"""Response models for the reference-docs router (Part 11.6)."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ReferenceSourceView(BaseModel):
    """One configured source. Not a secret (id, kind, owning app only)."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    kind: str
    application_id: str


class IndexStatusView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    source_id: str
    active_generation: int
    source_revision: str
    chunk_count: int
    status: str


class IndexGenerationView(BaseModel):
    """`POST /reference-docs/rollback` result (IndexGeneration.model_dump(json))."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    tenant_id: str
    source_id: str
    generation: int
    status: str
    source_revision: str
    embedding_model: str
    embedding_version: str
    chunk_count: int
    provenance: dict[str, Any] | None
    created_at: datetime


class ReferenceSearchHitView(BaseModel):
    """One hybrid retrieval hit. `content` is already-served data, not a secret."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: UUID
    source_id: str
    document_path: str
    content: str
    score: float
    source_revision: str
    metadata: dict[str, Any]


class ReindexAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    workflow_id: str
    status: str = Field(description="BackgroundJobStatus value, e.g. QUEUED.")


class SourceDeleteAck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    status: str = Field(description="Always 'DELETED'.")
    rows_removed: int
```

### Response-model mapping for the remaining routers

Each remaining view wraps an existing domain model (sanitized where needed) and is mechanically defined from it. The domain model named is the source of truth for the fields; the view declares only wire-visible fields in wire order.

| Router | Endpoint(s) | Response model | Item source |
| --- | --- | --- | --- |
| investigations | `GET /investigations` | `ListEnvelope[InvestigationListItemView]` | `{investigation_id, status, application_id, created_at, updated_at}` (already explicit in handler) |
| investigations | `GET /investigations/{id}` | `InvestigationDetailResponse` | `{investigation, pending_requirements}` — `investigation` = `InvestigationView` mapped from `domain.investigation.models.Investigation` |
| investigations | `POST /investigations` | `CreateInvestigationResponse` | `{id, session_id, application_id, status, tenant_id, investigation}` |
| investigations | `…/start`, `…/retry` | `DispatchAck` | `{investigation_id, status:"RUNNING", workflow_id, correlation_id, message}` |
| investigations | `…/conclusion` | `ConclusionResponse` | `{investigation_id, status, conclusion}` |
| investigations | `…/knowledge` | `KnowledgeResponse` | `{investigation_id, code_issue_fingerprint, artifacts, sessions, other_tenant_sessions}` — `other_tenant_sessions` already redacted |
| investigations | `…/pause`, `…/resume`, `…/cancel`, `…/approve-action`, `…/input-requirements/…/fulfill` | `AckResponse` (per-endpoint) | current dicts |
| evidence | `GET …/evidence`, `…/evidence/{id}` | `ListEnvelope[EvidenceItemView]`, `EvidenceItemView` | `domain.evidence.models.Evidence` |
| findings | `GET …/findings` | `ListEnvelope[FindingView]` | `domain.finding.models.Finding` |
| hypotheses | `GET …/hypotheses` | `ListEnvelope[HypothesisView]` | `domain.hypothesis.models.Hypothesis` |
| timeline | `GET …/timeline` | `ListEnvelope[TimelineEventView]` | `domain.timeline.models.TimelineEvent` |
| jobs | `GET /jobs` | `ListEnvelope[JobView]` | `domain.common.background_job.BackgroundJob` |
| jobs | `GET /jobs/{id}` | `JobView` | same |
| jobs | `POST /jobs/{id}/cancel` | `JobCancelResponse` | `{job, cancelled}` |
| clusters | `GET /clusters`, `GET /clusters/{id}` | `ListEnvelope[ClusterView]`, `ClusterView` | `domain.topology` cluster model |
| clusters | `POST /clusters/run` | `ClusterRunResponse` | current dict |
| knowledge | `GET /knowledge/search` | `ListEnvelope[KnowledgeArtifactView]` | `domain.knowledge` artifact model |
| chat | `POST /chat/sessions` | `ChatSessionResponse` | `{session}` |
| chat | `GET …/messages` | `ListEnvelope[ChatMessageView]` | `domain.chat` message model |
| capabilities | `GET /capabilities` | `ListEnvelope[CapabilityView]` | current dict |
| aggregate_reports | `GET /latest` | `ReportMetadataView` | metadata + signed URL (short-lived; never a storage key) |
| aggregate_reports | `GET /history` | `ListEnvelope[ReportHistoryItemView]` | history rows |
| aggregate_reports | `POST /aggregate-reports`, `DELETE` | `ReportBuildAck`, `ReportPurgeAck` | current dicts |
| batch_intake | `POST /batch-intake`, `GET /{job_id}` | `BatchIntakeView` (`ListEnvelope[BatchIntakeRecord]` already partially generated) | `BatchIntakeRecord` |
| batch_intake | `…/cancel`, `…/retry`, `DELETE` | `BatchIntakeAck` | current dicts |
| intake | `POST /intake/errors` | `ErrorIntakeAck` | current dict |
| health | `GET /health/live`, `/health/ready` | `HealthView` | `{status[, dependencies]}` |

Implementers fill each `*View` from the named domain model using the D4 pattern; the two modules above are the worked references.

## Interfaces

### REST API — response models by router

| Router | Endpoints | Success response model(s) | Error responses |
| --- | --- | --- | --- |
| health | 2 | `HealthView` | `COMMON_ERROR_RESPONSES` |
| investigations | 10 | `ListEnvelope[InvestigationListItemView]`, `InvestigationDetailResponse`, `CreateInvestigationResponse`, `DispatchAck`, `ConclusionResponse`, `KnowledgeResponse`, acks | `COMMON_ERROR_RESPONSES` |
| evidence | 2 | `ListEnvelope[EvidenceItemView]`, `EvidenceItemView` | `COMMON_ERROR_RESPONSES` |
| findings | 1 | `ListEnvelope[FindingView]` | `COMMON_ERROR_RESPONSES` |
| hypotheses | 1 | `ListEnvelope[HypothesisView]` | `COMMON_ERROR_RESPONSES` |
| timeline | 1 | `ListEnvelope[TimelineEventView]` | `COMMON_ERROR_RESPONSES` |
| jobs | 3 | `ListEnvelope[JobView]`, `JobView`, `JobCancelResponse` | `COMMON_ERROR_RESPONSES` |
| clusters | 3 | `ListEnvelope[ClusterView]`, `ClusterView`, `ClusterRunResponse` | `COMMON_ERROR_RESPONSES` |
| knowledge | 2 | `ListEnvelope[KnowledgeArtifactView]`, `KnowledgeResponse` | `COMMON_ERROR_RESPONSES` |
| chat | 4 | `ChatSessionResponse`, `ListEnvelope[ChatMessageView]`, acks | `COMMON_ERROR_RESPONSES` |
| capabilities | 1 | `ListEnvelope[CapabilityView]` | `COMMON_ERROR_RESPONSES` |
| aggregate_reports | 4 | `ReportMetadataView`, `ListEnvelope[ReportHistoryItemView]`, acks | `COMMON_ERROR_RESPONSES` |
| batch_intake | 5 | `BatchIntakeView`, `ListEnvelope[BatchIntakeRecord]`, acks | `COMMON_ERROR_RESPONSES` |
| intake | 1 | `ErrorIntakeAck` | `COMMON_ERROR_RESPONSES` |
| profiles | 8 | `ListEnvelope[ProfileView]`, `ProfileDetail`, `ListEnvelope[ProfileRevisionView]`, `ProfileDetail` (create/update), spring-drift/derive/refresh views | `COMMON_ERROR_RESPONSES` |
| reference_docs | 6 | `ItemsEnvelope[ReferenceSourceView]`, `CursorEnvelope[ReferenceSearchHitView]`, `IndexStatusView`, `IndexGenerationView`, `ReindexAck`, `SourceDeleteAck` | `COMMON_ERROR_RESPONSES` |

### Error response

| Status | Model | Body |
| --- | --- | --- |
| 400 | `ErrorResponse` | `{"error": {"code": "…", "message": "…", "correlation_id": "…"}}` |
| 401 / 403 | `ErrorResponse` | same, no `details` |
| 404 | `ErrorResponse` | same |
| 409 | `ErrorResponse` | same (concurrency / idempotency code) |
| 422 | `ErrorResponse` | same, with `details.fields[]` for form mapping |

Full endpoint inventory (all 55) is in Appendix A.

## Implementation Detail

### `reference_docs.py` — before and after

Before:

```python
@router.get("/reference-docs/sources")
async def list_reference_sources(
    x_tenant_id: str = Depends(require_tenant),
    application_id: str | None = Query(default=None, max_length=128, pattern=NO_NUL_PATTERN),
) -> dict[str, Any]:
    service = _service(get_app_context())
    return {"items": await service.list_sources(x_tenant_id, application_id)}


@router.get("/reference-docs/search")
async def search_reference_docs(...) -> dict[str, Any]:
    service = _service(get_app_context())
    hits, next_cursor = await service.search(...)
    return {
        "items": [hit.model_dump(mode="json") for hit in hits],
        "next_cursor": next_cursor,
        "has_more": next_cursor is not None,
    }
```

After:

```python
from investigation_agent_platform.api.v1.schemas.envelopes import CursorEnvelope, ItemsEnvelope
from investigation_agent_platform.api.v1.schemas.errors import COMMON_ERROR_RESPONSES
from investigation_agent_platform.api.v1.schemas.reference_docs import (
    ReferenceSearchHitView,
    ReferenceSourceView,
)


@router.get(
    "/reference-docs/sources",
    response_model=ItemsEnvelope[ReferenceSourceView],
    response_model_exclude_none=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def list_reference_sources(
    x_tenant_id: str = Depends(require_tenant),
    application_id: str | None = Query(default=None, max_length=128, pattern=NO_NUL_PATTERN),
) -> ItemsEnvelope[ReferenceSourceView]:
    service = _service(get_app_context())
    rows = await service.list_sources(x_tenant_id, application_id)
    return ItemsEnvelope[ReferenceSourceView](
        items=[ReferenceSourceView.model_validate(r) for r in rows]
    )


@router.get(
    "/reference-docs/search",
    response_model=CursorEnvelope[ReferenceSearchHitView],
    response_model_exclude_none=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def search_reference_docs(...) -> CursorEnvelope[ReferenceSearchHitView]:
    service = _service(get_app_context())
    try:
        hits, next_cursor = await service.search(...)
    except ValueError as exc:
        raise ApiError(code="VALIDATION_ERROR", http_status=400, message=str(exc)) from exc
    return CursorEnvelope[ReferenceSearchHitView](
        items=[ReferenceSearchHitView.model_validate(h.model_dump(mode="json")) for h in hits],
        next_cursor=next_cursor,
        has_more=next_cursor is not None,
    )
```

The remaining reference-docs routes follow: `status` → `response_model=IndexStatusView` (service already returns the model; return `IndexStatusView.model_validate(result.model_dump(mode="json"))` or declare the domain model directly in the schema module), `rollback` → `IndexGenerationView`, `reindex` → `ReindexAck` (keep caching `response_payload` as a dict; the replay path returns the cached dict, which FastAPI validates against `ReindexAck`), `delete` → `SourceDeleteAck`.

### `profiles.py` — before and after

Before (list, detail, create/update excerpts):

```python
def _sanitize(profile: ApplicationProfile) -> dict[str, Any]:
    return profile.sanitized()


@router.get("/profiles")
async def list_profiles(...) -> dict[str, Any]:
    profiles = await ctx.profile_repo.list(x_tenant_id, status=status_filter)
    return {"items": [_sanitize(p) for p in profiles], "total": len(profiles)}
```

After:

```python
from investigation_agent_platform.api.v1.schemas.envelopes import ListEnvelope
from investigation_agent_platform.api.v1.schemas.errors import COMMON_ERROR_RESPONSES
from investigation_agent_platform.api.v1.schemas.profiles import (
    ProfileDetail,
    ProfileRevisionView,
    ProfileView,
)


@router.get(
    "/profiles",
    response_model=ListEnvelope[ProfileView],
    response_model_exclude_none=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def list_profiles(
    x_tenant_id: str = Depends(require_tenant),
    status_filter: str | None = Query(default=None, alias="status"),
) -> ListEnvelope[ProfileView]:
    ctx = get_app_context()
    if status_filter is not None:
        try:
            ProfileStatus(status_filter)
        except ValueError as exc:
            raise ApiError(code="PROFILE_UNKNOWN_STATUS", http_status=400, message=f"Unknown status {status_filter!r}.") from exc
    profiles = await ctx.profile_repo.list(x_tenant_id, status=status_filter)
    items = [ProfileView.from_domain(p) for p in profiles]
    return ListEnvelope[ProfileView](items=items, total=len(items))


@router.get(
    "/profiles/{app_id}",
    response_model=ProfileDetail,
    response_model_exclude_none=True,
    responses=COMMON_ERROR_RESPONSES,
)
async def get_profile(...) -> ProfileDetail:
    ...
    return ProfileDetail(profile=ProfileView.from_domain(profile), version=profile.version)
```

`create_profile` / `update_profile` return `ProfileDetail(profile=..., version=..., idempotent_replay=...)`. **Idempotency note:** the reserve/replay store caches a dict; store `ProfileDetail(...).model_dump(mode="json")` and return that dict on replay (FastAPI validates it against `response_model`). `_sanitize` is deleted in favor of `ProfileView.from_domain`.

### `app.py` — unified error handlers

Replace the bodies of `handle_domain_exception`, `handle_validation_exception`, and `handle_pydantic_validation_exception` with the `ErrorResponse`-based versions in D5, and register `handle_api_error` for `ApiError`. The DBAPIError NUL-byte branch also returns an `ErrorResponse(str)` body. Keep the existing status-code mapping and correlation-id binding.

### Settings

No new settings. (Optional, deferred: a `response_validation_enabled` flag gating response re-validation in production if the D3 cost proves material.)

## Migrations

There is no schema or data migration. The "migration" is an ordered, additive code rollout:

1. **Add `api/v1/schemas/` package** (`envelopes.py`, `errors.py`) and the D7 guard test. Land this first; the guard is initially expected to fail (55 offenders) and marks the work.
2. **Unify the error envelope** (D5): add `ApiError`, the handler, and `COMMON_ERROR_RESPONSES`. Convert router-local `HTTPException(detail=dict)` helpers to `ApiError`. This is the only wire-visible change; coordinate the deploy with the UI team (the UI mapper already handles both shapes).
3. **Profiles and reference_docs** (highest drift risk, worked references) — add their schema modules, set `response_model`, delete `_sanitize`.
4. **Remaining routers**, one module at a time, in this order: investigations → evidence/findings/hypotheses/timeline → jobs → clusters → knowledge → chat → capabilities → aggregate_reports → batch_intake → intake → health.
5. **Regenerate the UI contract snapshot** (`contracts/openapi.snapshot.json`) and `src/types/api-generated.ts` once every router is typed; the UI-side generator switch (D9) is a separate follow-up change.

Backward compatibility: steps 1–4 are additive to the spec (new component schemas, no removed fields, no renamed fields) except the D5 error-body unification. No endpoint changes behavior.

## Testing Philosophy

### Contract guard

The D7 test walks `app.openapi()` and fails if any 2xx response is untyped or is an open `additionalProperties: true` object. This is the primary defense against regression and must pass in CI before any router is considered done.

### Wire compatibility

For every endpoint, add a golden test that captures the JSON produced by the real handler (via `httpx.ASGITransport`/`TestClient`) and asserts it is unchanged from the pre-change body, field for field. This catches serialization differences introduced by `response_model` — timestamp formats, UUID-as-string, alias handling, and `exclude_none` edge cases. Snapshot the "before" bodies from the running pre-change build.

### Redaction

Assert that `ProfileView.from_domain` output never contains `connection_ref` values, template SQL, or secret-shaped strings, even when the underlying `ApplicationProfile` has them. Add a test that constructs a profile with a `secret://` connection ref and a SQL template, then asserts the serialized `ProfileView` exposes only `{present, scheme}` and `{description}`. Include a negative test that a deliberately leaked field fails validation (`extra="forbid"`).

### Error envelope

For each error class (validation, not-found, conflict, unauthorized), assert the response body is exactly `{"error": {code, message, correlation_id[, details]}}` and that `X-Correlation-ID` matches `correlation_id`. Cover the 422 `details.fields[]` shape used for form mapping.

### Generation smoke test

A test (or CI step) that runs the chosen UI generator against the freshly built `app.openapi()` and asserts it exits 0, guarding against a spec that is technically typed but un-generatable (e.g. an unresolvable `$ref`).

## Documentation Plan

### `docs/dev/api-contract.md` (backend, new)

**Audience:** Backend and client developers

Document the response-model convention: the `api/v1/schemas/` package layout, the three envelope generics, the single error envelope, the `extra="forbid"` view-model drift rule, and the rule that any model with secrets uses an explicit sanitized projection. Include the D7 guard and how to run it, and a short "how to add a response model" recipe with the profiles module as the example.

### `docs/dev/architecture.md` (backend)

**Audience:** Backend developers

No `docs/dev/architecture.md` exists today (only an archived copy). If one is created, add an "API contract" subsection covering the typed-response convention and the CI guard. Until then, `docs/dev/api-contract.md` is the home.

### `docs/ui-api-matrix.md` (UI)

**Audience:** UI developers

Replace the "hand-verified envelope" notes with the generated-schema contract once D9 lands. Record the reference-docs envelopes (`{items}` and `{items,next_cursor,has_more}`) and the profiles lifecycle responses as spec-sourced rather than probe-sourced.

### `docs/ui-backend-contract-gaps.md` (UI)

**Audience:** UI developers

Update D16 and D19: D16 ("concrete response envelopes exist") can cite the spec as the source once typed; D19 ("actual error bodies differ from the OpenAPI declaration") is resolved by D5.

### `docs/ui-architecture.md` (UI)

**Audience:** UI developers

Update the "API layer" section note that `src/types/api-generated.ts` is "regenerate from the live spec": once the spec is rich, extend the note to cover the runtime-schema generator chosen in D9.

## Risks / Trade-offs

### Secret leakage via a naive response model

**Risk:** The most dangerous failure mode is setting `response_model=ApplicationProfile` (or any domain model carrying secrets) and thereby serializing `connection_ref` values and template SQL that `sanitized()` currently strips. This would be a silent security regression, not a typing improvement.

**Mitigation:** D4 mandates explicit sanitized view models for any redacted resource, with `extra="forbid"` so the view cannot silently widen. The redaction test in Testing Philosophy constructs a secret-bearing profile and asserts the view exposes only `{present, scheme}`/`{description}`. Code review must reject any `response_model=` pointing at a raw domain model.

### Wire drift from serialization differences

**Risk:** Enabling `response_model` changes how values serialize — datetimes, UUIDs, aliases, and `None` handling can shift the exact JSON even when fields are unchanged.

**Mitigation:** Golden wire tests per endpoint, captured from the pre-change build, assert byte-for-byte equality. `response_model_by_alias=True` and `response_model_exclude_none=True` are set explicitly to preserve current output.

### The error-envelope unification is a breaking change for some clients

**Risk:** Endpoints that currently return `{"detail": {...}}` will return `{"error": {...}}` after D5. A client parsing `detail` breaks.

**Mitigation:** This is called out as the only intentional wire change, scoped to step 2 of the rollout and coordinated with the UI (whose error mapper already handles both shapes). Endpoints already returning `{"error": {...}}` are unaffected.

### Per-response validation cost

**Risk:** FastAPI re-validates and re-serializes every response through the model, adding CPU on hot read paths.

**Mitigation:** Measure on representative endpoints before broad rollout. If material, keep `response_model` for the OpenAPI schema but relax runtime validation via a custom route class — deferred, and only if benchmarks justify it.

### Large blast radius across 55 endpoints

**Risk:** Touching every route risks regressions and a long-lived branch.

**Mitigation:** Incremental, per-router rollout (Migrations), with the D7 guard and golden tests gating each step. Profiles and reference-docs land first as worked references.

### `extra="forbid"` couples the view to the domain model

**Risk:** Adding a domain field requires a matching view field in the same change, or tests fail.

**Mitigation:** Intentional — it converts silent drift into a test failure. Documented in `docs/dev/api-contract.md` so the team expects it.

### Stale `openapi.json` snapshot and generated types in the UI

**Risk:** Until the UI regenerates its snapshot and types (Migrations step 5), the UI's contract check may fail or pass misleadingly.

**Mitigation:** Regenerate both as the final step, deliberately and reviewably, and switch the UI to spec-sourced schemas per D9.

## Accessibility

Not applicable. This design changes a JSON API contract; it introduces no user-facing interface (no GUI, TUI, or CLI surface).

## Appendix A — Endpoint inventory (55)

| Method | Path | Response model (target) |
| --- | --- | --- |
| `DELETE` | `/api/v1/aggregate-reports` | `ReportPurgeAck` |
| `POST` | `/api/v1/aggregate-reports` | `ReportBuildAck` |
| `GET` | `/api/v1/aggregate-reports/history` | `ListEnvelope[ReportHistoryItemView]` |
| `GET` | `/api/v1/aggregate-reports/latest` | `ReportMetadataView` |
| `POST` | `/api/v1/batch-intake` | `BatchIntakeView` |
| `DELETE` | `/api/v1/batch-intake/{job_id}` | `BatchIntakeAck` |
| `GET` | `/api/v1/batch-intake/{job_id}` | `BatchIntakeView` |
| `POST` | `/api/v1/batch-intake/{job_id}/cancel` | `BatchIntakeAck` |
| `POST` | `/api/v1/batch-intake/{job_id}/retry` | `BatchIntakeAck` |
| `GET` | `/api/v1/capabilities` | `ListEnvelope[CapabilityView]` |
| `POST` | `/api/v1/chat/sessions` | `ChatSessionResponse` |
| `DELETE` | `/api/v1/chat/sessions/{session_id}` | `AckResponse` |
| `GET` | `/api/v1/chat/sessions/{session_id}/messages` | `ListEnvelope[ChatMessageView]` |
| `POST` | `/api/v1/chat/sessions/{session_id}/messages` | `ChatMessageView` |
| `GET` | `/api/v1/clusters` | `ListEnvelope[ClusterView]` |
| `POST` | `/api/v1/clusters/run` | `ClusterRunResponse` |
| `GET` | `/api/v1/clusters/{cluster_id}` | `ClusterView` |
| `GET` | `/api/v1/health/live` | `HealthView` |
| `GET` | `/api/v1/health/ready` | `HealthView` |
| `POST` | `/api/v1/intake/errors` | `ErrorIntakeAck` |
| `GET` | `/api/v1/investigations` | `ListEnvelope[InvestigationListItemView]` |
| `POST` | `/api/v1/investigations` | `CreateInvestigationResponse` |
| `GET` | `/api/v1/investigations/{investigation_id}` | `InvestigationDetailResponse` |
| `POST` | `/api/v1/investigations/{investigation_id}/approve-action` | `ApproveActionResponse` |
| `POST` | `/api/v1/investigations/{investigation_id}/cancel` | `AckResponse` |
| `GET` | `/api/v1/investigations/{investigation_id}/conclusion` | `ConclusionResponse` |
| `GET` | `/api/v1/investigations/{investigation_id}/evidence` | `ListEnvelope[EvidenceItemView]` |
| `GET` | `/api/v1/investigations/{investigation_id}/evidence/{evidence_id}` | `EvidenceItemView` |
| `GET` | `/api/v1/investigations/{investigation_id}/findings` | `ListEnvelope[FindingView]` |
| `GET` | `/api/v1/investigations/{investigation_id}/hypotheses` | `ListEnvelope[HypothesisView]` |
| `POST` | `/api/v1/investigations/{investigation_id}/input-requirements/{requirement_id}/fulfill` | `AckResponse` |
| `GET` | `/api/v1/investigations/{investigation_id}/knowledge` | `KnowledgeResponse` |
| `POST` | `/api/v1/investigations/{investigation_id}/pause` | `AckResponse` |
| `POST` | `/api/v1/investigations/{investigation_id}/resume` | `AckResponse` |
| `POST` | `/api/v1/investigations/{investigation_id}/retry` | `DispatchAck` |
| `POST` | `/api/v1/investigations/{investigation_id}/start` | `DispatchAck` |
| `GET` | `/api/v1/investigations/{investigation_id}/timeline` | `ListEnvelope[TimelineEventView]` |
| `GET` | `/api/v1/jobs` | `ListEnvelope[JobView]` |
| `GET` | `/api/v1/jobs/{job_id}` | `JobView` |
| `POST` | `/api/v1/jobs/{job_id}/cancel` | `JobCancelResponse` |
| `GET` | `/api/v1/knowledge/search` | `ListEnvelope[KnowledgeArtifactView]` |
| `GET` | `/api/v1/profiles` | `ListEnvelope[ProfileView]` |
| `POST` | `/api/v1/profiles` | `ProfileDetail` |
| `GET` | `/api/v1/profiles/{app_id}` | `ProfileDetail` |
| `PUT` | `/api/v1/profiles/{app_id}` | `ProfileDetail` |
| `GET` | `/api/v1/profiles/{app_id}/revisions` | `ListEnvelope[ProfileRevisionView]` |
| `POST` | `/api/v1/profiles/{app_id}:refresh-from-spring` | `ProfileDetail` |
| `GET` | `/api/v1/profiles/{app_id}:spring-drift` | `SpringDriftView` |
| `POST` | `/api/v1/profiles:derive-from-spring` | `DeriveFromSpringResponse` |
| `POST` | `/api/v1/reference-docs/reindex` | `ReindexAck` |
| `POST` | `/api/v1/reference-docs/rollback` | `IndexGenerationView` |
| `GET` | `/api/v1/reference-docs/search` | `CursorEnvelope[ReferenceSearchHitView]` |
| `GET` | `/api/v1/reference-docs/sources` | `ItemsEnvelope[ReferenceSourceView]` |
| `DELETE` | `/api/v1/reference-docs/sources/{source_id}` | `SourceDeleteAck` |
| `GET` | `/api/v1/reference-docs/status` | `IndexStatusView` |
