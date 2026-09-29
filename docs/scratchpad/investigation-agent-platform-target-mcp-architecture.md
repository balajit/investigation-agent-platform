# Target MCP Architecture and Package/File Layout
## investigation-agent-platform

## Purpose

This document is an implementation blueprint for a coding agent.

The goal is to add an MCP service to **`investigation-agent-platform`** that exposes safe, bounded investigation capabilities to a coding/investigation agent.

The MCP layer must **not** become a second Elasticsearch client, a raw query gateway, or an authorization bypass.

The architecture must build on the existing evidence implementation:

- `src/investigation_agent_platform/infrastructure/evidence/runtime/elastic.py`
- `src/investigation_agent_platform/infrastructure/evidence/logs/elastic_bridge.py`

The current Elastic adapter already owns provider-side query construction, limits, search pagination, and evidence mapping. The MCP service should call domain/application capabilities that ultimately reuse those controls.

The existing trace bridge already delegates trace retrieval to `AsyncElasticAdapter` and derives code locations and SQL/table information. The target architecture should preserve that delegation while moving reusable investigation semantics into application/domain services where appropriate.

---

# 1. Architectural objective

The target flow is:

```text
┌─────────────────────────────────────────────────────────────┐
│                    Coding / Investigation Agent             │
│                                                             │
│  Uses MCP tools only                                        │
└──────────────────────────────┬──────────────────────────────┘
                               │ MCP
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                     MCP Service / Server                    │
│                                                             │
│  Transport                                                │
│  Tool registration                                         │
│  Request schema validation                                 │
│  Agent-facing error mapping                                │
│  Context propagation                                       │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                Investigation Application Layer              │
│                                                             │
│  InvestigationService                                      │
│  RuntimeEvidenceService                                    │
│  TraceInvestigationService                                 │
│  EvidenceDetailService                                     │
│  Query / pagination policy                                  │
│  Completeness / truncation semantics                       │
└──────────────────────────────┬──────────────────────────────┘
                               │
                ┌──────────────┴───────────────┐
                ▼                              ▼
┌──────────────────────────────┐  ┌───────────────────────────┐
│ Existing Evidence Ports      │  │ Existing Domain Models    │
│                              │  │                           │
│ RuntimeEvidenceProvider      │  │ Evidence                  │
│ EvidenceGateway              │  │ Provenance                │
│                              │  │ Requests                  │
└──────────────┬───────────────┘  └───────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────┐
│                Infrastructure Evidence Layer                │
│                                                             │
│ AsyncElasticAdapter                                         │
│ ElasticIncidentBridge                                      │
│ Elastic query builder / normalizer                         │
│ Cursor codec                                                │
│ Evidence projection / redaction                             │
└──────────────────────────────┬──────────────────────────────┘
                               │
                               ▼
                       Elasticsearch
```

---

# 2. Core architectural rules

## Rule 1 — MCP never talks directly to Elasticsearch

The MCP server must never instantiate or call:

```python
AsyncElasticsearch
```

directly.

It must call application/domain services.

This prevents:

- duplicated query security;
- raw Elasticsearch DSL exposure;
- tenant isolation bypass;
- inconsistent pagination;
- inconsistent provenance;
- uncontrolled provider queries.

---

## Rule 2 — MCP tools expose investigation operations, not infrastructure operations

Do not expose a tool such as:

```text
elasticsearch_search
```

or:

```text
run_elasticsearch_query
```

Do expose bounded investigation operations such as:

```text
search_runtime_evidence
get_evidence
investigate_trace
```

The agent should reason in investigation concepts.

---

## Rule 3 — authorization context is trusted infrastructure

Tenant and investigation identity must come from trusted request context.

Do not allow the LLM to supply:

```text
tenant_id
investigation_id
```

as arbitrary authorization context.

A tool request may contain query criteria, but the authenticated MCP session/context determines the tenant and investigation scope.

---

## Rule 4 — every result must preserve provenance

MCP responses should preserve enough provenance for an agent to answer:

```text
Where did this fact come from?
```

At minimum:

- evidence ID;
- provider;
- source;
- observed timestamp;
- retrieved timestamp;
- investigation ID;
- tenant context where appropriate;
- query/provenance metadata;
- completeness/truncation state.

---

## Rule 5 — provider bounds remain mandatory

The existing provider-side limits must remain enforced even if the MCP layer validates the same values.

The architecture intentionally uses defense in depth:

```text
MCP schema validation
        +
Application policy
        +
Provider boundary enforcement
```

Never remove provider-side limits because MCP has a maximum.

---

# 3. Target package/file layout

Use the following layout unless repository conventions require an equivalent naming structure.

```text
src/
└── investigation_agent_platform/
    │
    ├── domain/
    │   ├── evidence/
    │   │   ├── models.py
    │   │   ├── requests.py
    │   │   └── ...
    │   │
    │   ├── investigation/
    │   │   ├── models.py
    │   │   ├── requests.py
    │   │   └── ...
    │   │
    │   └── provenance/
    │       └── models.py
    │
    ├── application/
    │   ├── investigation/
    │   │   ├── __init__.py
    │   │   ├── service.py
    │   │   ├── runtime_evidence.py
    │   │   ├── trace_investigation.py
    │   │   └── evidence_detail.py
    │   │
    │   └── ...
    │
    ├── infrastructure/
    │   ├── evidence/
    │   │   ├── runtime/
    │   │   │   ├── elastic.py
    │   │   │   ├── cursor.py
    │   │   │   ├── query.py
    │   │   │   ├── projection.py
    │   │   │   └── validation.py
    │   │   │
    │   │   └── logs/
    │   │       └── elastic_bridge.py
    │   │
    │   └── ...
    │
    ├── mcp/
    │   ├── __init__.py
    │   ├── server.py
    │   ├── context.py
    │   ├── errors.py
    │   ├── registry.py
    │   │
    │   ├── schemas/
    │   │   ├── __init__.py
    │   │   ├── common.py
    │   │   ├── evidence.py
    │   │   ├── trace.py
    │   │   └── investigation.py
    │   │
    │   ├── tools/
    │   │   ├── __init__.py
    │   │   ├── runtime_evidence.py
    │   │   ├── evidence_detail.py
    │   │   └── trace_investigation.py
    │   │
    │   └── resources/
    │       └── ...
    │
    └── config/
        └── ...

tests/
├── unit/
│   ├── application/
│   │   └── investigation/
│   ├── infrastructure/
│   │   └── evidence/
│   └── mcp/
│
├── integration/
│   ├── mcp/
│   ├── evidence/
│   └── elastic/
│
└── contract/
    └── mcp/
```

If the repository already has a different application/service package convention, adapt the names while preserving these boundaries.

---

# 4. Responsibility of every new file

## `src/investigation_agent_platform/mcp/server.py`

Owns MCP server construction.

Responsibilities:

- create/configure MCP server;
- register tools;
- create request context;
- connect application services;
- expose transport entry point;
- configure lifecycle/shutdown.

Must not:

- construct Elasticsearch queries;
- parse SQL;
- access Elasticsearch directly;
- perform authorization using agent-provided tenant IDs.

---

## `src/investigation_agent_platform/mcp/registry.py`

Central tool registration.

Example responsibility:

```python
def register_investigation_tools(
    server: MCPServer,
    services: InvestigationServices,
) -> None:
    ...
```

Keep registration deterministic.

Do not embed tool implementation logic here.

---

# 5. MCP context

## `src/investigation_agent_platform/mcp/context.py`

Define a trusted request context.

Conceptually:

```python
@dataclass(frozen=True)
class InvestigationContext:
    tenant_id: str
    investigation_id: UUID
    correlation_id: str
    actor_id: str | None
```

The exact fields must follow the repository's existing authentication/request context.

Important:

```text
tenant_id
investigation_id
authorization context
```

must come from trusted infrastructure.

They must not be accepted as arbitrary tool arguments merely because the MCP schema supports them.

---

# 6. MCP error model

## `src/investigation_agent_platform/mcp/errors.py`

Translate internal errors into safe agent-facing errors.

Map categories such as:

```text
Invalid request
Invalid cursor
Unauthorized
Forbidden
Evidence not found
Provider unavailable
Provider timeout
Evidence incomplete
Internal failure
```

Never expose:

- Elasticsearch stack traces;
- internal filesystem paths;
- credentials;
- raw SQL containing sensitive values;
- provider request internals.

Do preserve actionable information.

Example:

```json
{
  "code": "INVALID_CURSOR",
  "message": "The pagination cursor is invalid or no longer matches this investigation query."
}
```

---

# 7. MCP schemas

## `src/investigation_agent_platform/mcp/schemas/common.py`

Define reusable schemas:

```text
TimeRangeInput
PaginationInput
ProvenanceOutput
CompletenessOutput
ErrorOutput
```

Do not duplicate schemas across tools.

---

## `src/investigation_agent_platform/mcp/schemas/evidence.py`

Define agent-facing evidence input/output models.

### Search input

Conceptually:

```python
class SearchRuntimeEvidenceInput:
    environment: str
    identifiers: dict[str, str] | None
    keywords: list[str] | None
    services: list[str] | None
    severities: list[str] | None
    start_time: datetime | None
    end_time: datetime | None
    limit: int
    cursor: str | None
```

Do not include:

```text
tenant_id
investigation_id
raw Elasticsearch query
index
```

as agent-controlled fields.

---

## Search output

Conceptually:

```python
class RuntimeEvidenceItem:
    evidence_id: UUID
    evidence_type: str
    observed_at: datetime | None
    retrieved_at: datetime
    summary: str
    attributes: dict[str, Any]
    provenance: ProvenanceOutput
```

And:

```python
class RuntimeEvidenceSearchOutput:
    items: list[RuntimeEvidenceItem]
    next_cursor: str | None
    has_more: bool
    completeness: CompletenessOutput
    total_count: int | None
```

The actual domain models should be reused or serialized rather than creating unnecessary duplicate representations.

---

# 8. Tool 1 — `search_runtime_evidence`

## Purpose

Allow the investigation agent to search bounded runtime evidence.

Tool name:

```text
search_runtime_evidence
```

### Input

Agent-controlled:

```text
environment
identifiers
keywords
services
severities
time range
limit
cursor
```

Trusted context:

```text
tenant_id
investigation_id
correlation_id
authorization
```

### Application call

```python
await runtime_evidence_service.search(
    context=context,
    request=request,
)
```

### Never allow

```text
raw Elasticsearch DSL
raw index name
arbitrary field selection
arbitrary sort
arbitrary aggregation
script queries
runtime mappings
```

---

# 9. Tool 2 — `get_evidence`

## Purpose

Retrieve a previously identified evidence item.

Tool name:

```text
get_evidence
```

### Input

```text
evidence_id
```

Context supplies:

```text
tenant_id
investigation_id
```

### Required behavior

The service must verify that the evidence belongs to the authorized investigation/tenant scope according to the repository's domain model.

Do not treat the evidence ID as authorization.

---

# 10. Tool 3 — `investigate_trace`

## Purpose

Provide a bounded, structured investigation view of a trace.

Tool name:

```text
investigate_trace
```

Input:

```text
trace_id
environment
time_range
```

Optional input only where justified:

```text
max_evidence
```

Do not expose arbitrary Elastic filters through this tool.

---

# 11. Trace result contract

The result should conceptually contain:

```json
{
  "trace_id": "...",
  "code_locations": [],
  "accessed_tables": [],
  "sql_statements": [],
  "evidence": [],
  "completeness": {
    "complete": true,
    "truncated": false,
    "reason": null
  }
}
```

Each derived item should retain source evidence references.

Example:

```json
{
  "function": "checkout.submit",
  "file_path": "/app/checkout.py",
  "line": 142,
  "evidence_ids": ["..."]
}
```

For tables:

```json
{
  "catalog": null,
  "schema": "payments",
  "table": "transactions",
  "evidence_ids": ["..."]
}
```

For SQL:

```json
{
  "statement": "...",
  "evidence_id": "...",
  "truncated": false,
  "parse_status": "parsed"
}
```

---

# 12. Application layer

## `application/investigation/runtime_evidence.py`

Create:

```python
class RuntimeEvidenceService:
    async def search(
        self,
        context: InvestigationContext,
        request: RuntimeEvidenceRequest,
    ) -> EvidenceQueryResult:
        ...
```

Responsibilities:

- validate application-level semantics;
- combine trusted context with request;
- invoke the evidence gateway/provider;
- enforce investigation-level policy;
- normalize completeness;
- return domain result.

It must not know MCP transport details.

---

# 13. Application trace service

## `application/investigation/trace_investigation.py`

Create:

```python
class TraceInvestigationService:
    async def investigate_trace(
        self,
        context: InvestigationContext,
        request: TraceInvestigationRequest,
    ) -> TraceInvestigationResult:
        ...
```

Responsibilities:

1. validate trace request;
2. construct runtime evidence request;
3. retrieve runtime evidence through the existing evidence abstraction;
4. extract code locations;
5. extract SQL;
6. extract table references;
7. retain evidence provenance;
8. report completeness/truncation;
9. return a deterministic result.

Do not import MCP modules.

---

# 14. Evidence detail service

## `application/investigation/evidence_detail.py`

Create:

```python
class EvidenceDetailService:
    async def get(
        self,
        context: InvestigationContext,
        evidence_id: UUID,
    ) -> Evidence:
        ...
```

This service is the correct place to enforce investigation-level retrieval semantics.

It should call the appropriate evidence gateway/provider.

If the existing provider protocol only supports:

```python
get_runtime_evidence(tenant_id, evidence_id)
```

determine whether the protocol needs to be extended with investigation context.

Do not fake investigation identity.

---

# 15. Infrastructure: split the Elastic adapter

The current `elastic.py` contains several distinct responsibilities.

Refactor carefully into:

```text
infrastructure/evidence/runtime/
├── elastic.py
├── cursor.py
├── query.py
├── projection.py
└── validation.py
```

---

# 16. `cursor.py`

Own:

```text
cursor encoding
cursor decoding
cursor version
cursor signature
cursor query binding
cursor expiration/rotation if required
```

Example conceptual API:

```python
class ElasticCursorCodec:
    def encode(
        self,
        *,
        sort_values: list[Any],
        query_fingerprint: str,
        tenant_id: str,
        investigation_id: UUID,
    ) -> str:
        ...

    def decode(
        self,
        *,
        cursor: str,
        expected_query_fingerprint: str,
        tenant_id: str,
        investigation_id: UUID,
    ) -> CursorState:
        ...
```

Invalid cursor must raise a typed error.

Never return `None` for both:

```text
no cursor
invalid cursor
```

---

# 17. Cursor payload

Use a versioned structure similar to:

```json
{
  "v": 1,
  "tenant_id": "...",
  "investigation_id": "...",
  "query_fingerprint": "...",
  "sort": ["...", "..."],
  "issued_at": "...",
  "expires_at": "..."
}
```

Only include fields necessary for pagination/security.

Sign the canonical serialized payload.

Use a secure configured signing key.

Do not hard-code:

```text
iap-elastic-cursor-binding-key
```

---

# 18. `query.py`

Own normalized Elastic query construction.

Create:

```python
@dataclass(frozen=True)
class NormalizedRuntimeQuery:
    tenant_id: str
    investigation_id: UUID
    environment: str
    identifiers: tuple[tuple[str, str], ...]
    keywords: tuple[str, ...]
    services: tuple[str, ...]
    severities: tuple[str, ...]
    start_time: datetime | None
    end_time: datetime | None
    limit: int
    sort_version: str
```

Then:

```python
class ElasticRuntimeQueryBuilder:
    def normalize(...) -> NormalizedRuntimeQuery:
        ...

    def build(...) -> dict[str, Any]:
        ...
```

The normalized representation must be used for:

- query fingerprint;
- cursor binding;
- deterministic behavior.

---

# 19. Query builder security rules

The builder must never accept arbitrary Elasticsearch DSL.

It may generate only the known supported clauses:

```text
term
terms
multi_match
range
bool
```

and the approved sort.

Never accept:

```text
script
regexp
wildcard
query_string
aggregations
runtime_mappings
source scripting
arbitrary sort fields
```

unless a future domain requirement explicitly adds them with separate security review.

---

# 20. `validation.py`

Own provider-boundary validation:

```text
tenant ID
environment
identifier keys
identifier values
keywords
services
severities
time range
limit
```

Important behavior:

### Invalid restrictive filters

Do not silently discard invalid values.

Example:

```text
severities=["NOT_A_LEVEL"]
```

must not become:

```text
no severity filter
```

It must fail validation.

### Oversized lists

Do not silently truncate unless the domain contract explicitly defines truncation.

Prefer:

```text
INVALID_REQUEST
```

for caller-controlled limits.

---

# 21. `projection.py`

Create:

```python
class ElasticEvidenceProjector:
    def project(
        self,
        hit: Mapping[str, Any],
        context: InvestigationContext,
        provenance: ...
    ) -> Evidence:
        ...
```

Responsibilities:

- validate provider response;
- extract timestamp safely;
- project allowed attributes;
- redact sensitive fields;
- bound content;
- build evidence identity;
- build provenance;
- preserve classification;
- distinguish observation time from retrieval time.

Do not pass the complete `_source` through blindly.

---

# 22. Timestamp policy

Never do:

```python
datetime.now(UTC)
```

as a fallback observation timestamp.

Use:

```text
observed_at = parsed provider timestamp
retrieved_at = current retrieval time
```

If observation time is unavailable:

```text
observed_at = None
```

if the domain supports that.

If it does not, update the domain representation explicitly rather than fabricating data.

---

# 23. Provenance policy

Separate:

```text
provider document identity
content fingerprint
query fingerprint
```

Do not use:

```python
hit["_id"]
```

as:

```text
normalized_query_hash
```

A query fingerprint should be:

```text
hash(canonical_normalized_query)
```

A content fingerprint should be:

```text
hash(canonical_projected_content)
```

only if the domain actually requires content hashing.

---

# 24. Evidence identity

Define one canonical platform identity.

The target architecture should support:

```text
search result
    -> evidence_id
    -> get_evidence(evidence_id)
```

without guessing whether the ID is:

- Elasticsearch `_id`;
- platform UUID;
- source reference.

If Elasticsearch identity is needed internally, store it as provider provenance.

---

# 25. Direct retrieval

The target service should conceptually do:

```text
MCP get_evidence
    ↓
EvidenceDetailService
    ↓
EvidenceGateway
    ↓
AsyncElasticAdapter
    ↓
validated provider retrieval
    ↓
projection
```

The direct retrieval path must not invent a fake investigation ID.

If investigation context is mandatory, propagate the actual context through the port.

---

# 26. Elastic bridge refactor

Keep:

```text
infrastructure/evidence/logs/elastic_bridge.py
```

only if the repository's infrastructure layering requires it.

Refactor its core extraction behavior into a reusable service/component:

```text
TraceTelemetryExtractor
```

Suggested file:

```text
infrastructure/evidence/logs/trace_telemetry.py
```

Responsibilities:

```text
Evidence[]
    ↓
code locations
SQL statements
table references
source evidence references
completeness metadata
```

The bridge can then become a compatibility adapter if required.

---

# 27. SQL extraction architecture

Create a small component:

```python
class SqlEvidenceExtractor:
    def extract(
        self,
        *,
        evidence_id: UUID,
        sql: str,
        dialect: str | None,
    ) -> SqlExtractionResult:
        ...
```

It must:

- never execute SQL;
- bound input length;
- safely parse;
- handle parse failure explicitly;
- support multi-statement input if repository semantics require it;
- preserve source evidence ID;
- redact sensitive literals where required;
- preserve table qualification.

---

# 28. Table extraction

Represent table references structurally.

Conceptually:

```python
@dataclass(frozen=True)
class TableReference:
    catalog: str | None
    schema: str | None
    table: str
```

Do not reduce:

```text
payments.transactions
```

to:

```text
transactions
```

unless the domain explicitly requires that representation.

Do not blindly lowercase quoted identifiers.

Use SQL dialect-aware normalization where supported.

---

# 29. Code-location extraction

Represent:

```python
@dataclass(frozen=True)
class CodeLocation:
    function: str | None
    file_path: str | None
    line: int | None
    evidence_ids: tuple[UUID, ...]
```

Normalize and validate:

- function length;
- path length;
- line type;
- line range.

Deduplicate deterministically.

Sort deterministically.

---

# 30. Completeness contract

Introduce a reusable model:

```python
@dataclass(frozen=True)
class EvidenceCompleteness:
    complete: bool
    truncated: bool
    reason: str | None
    returned_count: int
    examined_count: int
```

Use it in:

```text
RuntimeEvidenceSearchResult
TraceInvestigationResult
```

Possible reasons:

```text
provider_limit
application_limit
invalid_or_missing_metadata
provider_failure
pagination_not_exhausted
```

Do not use vague messages such as:

```text
some results may be missing
```

---

# 31. MCP response should expose completeness

An investigation agent must be able to distinguish:

```text
No evidence found
```

from:

```text
Evidence exists but only the first bounded page was examined
```

Therefore:

```json
{
  "items": [],
  "completeness": {
    "complete": true,
    "truncated": false,
    "reason": null
  }
}
```

and:

```json
{
  "items": [],
  "completeness": {
    "complete": false,
    "truncated": true,
    "reason": "provider_limit"
  }
}
```

must be semantically different.

---

# 32. Pagination strategy

For `search_runtime_evidence`:

```text
first call
    -> items + next_cursor

subsequent call
    -> cursor validated against exact query
    -> search_after
    -> items + next_cursor
```

The cursor must be bound to:

```text
tenant
investigation
normalized query
sort version
```

Do not bind it only to:

```text
tenant + investigation
```

---

# 33. Cursor expiration

If operationally appropriate, include:

```text
issued_at
expires_at
```

and reject expired cursors.

The expiration period must be configuration-driven and bounded.

If the repository does not need expiration, document why.

---

# 34. Transport architecture

Keep transport separate from tools.

Conceptually:

```text
mcp/server.py
        ↓
mcp/registry.py
        ↓
mcp/tools/*.py
        ↓
application services
```

Do not put business logic inside MCP decorators/handlers.

This makes the investigation services reusable by:

- MCP;
- REST/API;
- CLI;
- background workflows;
- tests.

---

# 35. Tool implementation pattern

Each tool should follow this structure:

```python
async def search_runtime_evidence(
    input: SearchRuntimeEvidenceInput,
    context: InvestigationContext,
    service: RuntimeEvidenceService,
) -> SearchRuntimeEvidenceOutput:
    request = input.to_domain_request()

    result = await service.search(
        context=context,
        request=request,
    )

    return SearchRuntimeEvidenceOutput.from_domain(result)
```

The handler should not:

- build Elastic DSL;
- access Elasticsearch;
- parse SQL;
- validate tenant authorization;
- manipulate cursors directly.

---

# 36. MCP tool descriptions

Tool descriptions are part of the agent interface.

Descriptions must explicitly state:

## `search_runtime_evidence`

> Search bounded runtime evidence within the current investigation. Results are scoped to the authenticated tenant and investigation. Use returned pagination cursors only with the same search criteria. Results may be incomplete when provider or application bounds are reached.

## `get_evidence`

> Retrieve a specific evidence item already identified in the current investigation. Access is restricted to the current investigation scope.

## `investigate_trace`

> Investigate runtime evidence associated with a trace and return bounded code-location, SQL, and table-reference evidence with source provenance and completeness metadata.

Avoid vague descriptions such as:

> Search Elasticsearch logs.

The tool description should communicate the investigation semantics.

---

# 37. Tool schemas must make unsafe operations impossible

Do not expose a generic dictionary such as:

```python
query: dict[str, Any]
```

for Elasticsearch operations.

Prefer typed fields.

Do not expose:

```text
index
body
query_dsl
sort
aggregations
source_filter
```

to the agent.

---

# 38. Security boundary

The target security flow is:

```text
MCP transport authentication
        ↓
trusted context
        ↓
application authorization
        ↓
domain request validation
        ↓
provider-bound validation
        ↓
bounded Elastic query
```

No stage should permit a lower-trust input to replace a higher-trust value.

---

# 39. Configuration

Add configuration for:

```text
ELASTIC_CURSOR_SIGNING_KEY
ELASTIC_CURSOR_TTL
ELASTIC_PROVIDER_TIMEOUT
ELASTIC_QUERY_TIMEOUT
ELASTIC_MAX_HITS
ELASTIC_MAX_TIME_WINDOW_SECONDS
ELASTIC_MAX_KEYWORD_TERMS
ELASTIC_MAX_SERVICES
ELASTIC_MAX_IDENTIFIER_LENGTH
ELASTIC_MAX_SQL_LENGTH
ELASTIC_MAX_CONTENT_LENGTH
```

Only make values configurable where operationally appropriate.

Security ceilings must remain protected from unsafe configuration.

Validate configuration at startup.

---

# 40. Dependency wiring

Create an application composition object if the repository does not already have one.

Conceptually:

```python
@dataclass
class InvestigationServices:
    runtime_evidence: RuntimeEvidenceService
    evidence_detail: EvidenceDetailService
    trace_investigation: TraceInvestigationService
```

Construction:

```text
Elastic client
    ↓
AsyncElasticAdapter
    ↓
Evidence gateway/provider
    ↓
RuntimeEvidenceService
    ↓
TraceInvestigationService
    ↓
MCP tools
```

Do not instantiate dependencies inside individual MCP tool functions.

---

# 41. Testing architecture

## Unit tests

```text
tests/unit/mcp/
    test_context.py
    test_errors.py
    test_runtime_evidence_tool.py
    test_evidence_detail_tool.py
    test_trace_investigation_tool.py
```

Verify:

- schema validation;
- trusted context propagation;
- error mapping;
- serialization.

---

## Application tests

```text
tests/unit/application/investigation/
    test_runtime_evidence_service.py
    test_evidence_detail_service.py
    test_trace_investigation_service.py
```

Verify:

- authorization scope;
- request semantics;
- completeness;
- provenance;
- delegation.

---

## Infrastructure tests

```text
tests/unit/infrastructure/evidence/
    test_elastic_cursor.py
    test_elastic_query.py
    test_elastic_validation.py
    test_elastic_projection.py
    test_trace_telemetry.py
    test_sql_extraction.py
```

---

# 42. MCP contract tests

Create:

```text
tests/contract/mcp/
    test_tool_catalog.py
    test_tool_schemas.py
    test_tool_errors.py
    test_tool_security.py
```

Contract tests must verify:

- exact tool names;
- required/optional arguments;
- no tenant authorization input;
- no raw Elastic DSL input;
- output shape;
- error shape;
- pagination fields.

---

# 43. Integration tests

Create:

```text
tests/integration/mcp/
    test_search_runtime_evidence.py
    test_get_evidence.py
    test_investigate_trace.py
```

Use mocked/fake evidence providers where possible.

For Elastic-specific integration:

```text
tests/integration/elastic/
    test_query_generation.py
    test_cursor_pagination.py
    test_provider_response_mapping.py
```

---

# 44. Security tests

The MCP test suite must include attempts to:

```text
change tenant
change investigation
reuse another investigation cursor
reuse another query cursor
inject wildcard into environment
inject wildcard into tenant
send arbitrary Elastic DSL
send oversized keyword
send invalid severity
send oversized SQL
send sensitive SQL literals
send malformed provider response
```

Every attempt must result in:

```text
safe rejection
```

or:

```text
bounded, documented behavior
```

Never an authorization bypass.

---

# 45. Example request flow

## Agent request

```json
{
  "environment": "production",
  "identifiers": {
    "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736"
  },
  "keywords": ["timeout"],
  "services": ["checkout"],
  "severities": ["ERROR"],
  "start_time": "2026-09-28T01:00:00Z",
  "end_time": "2026-09-28T01:15:00Z",
  "limit": 25
}
```

Trusted context:

```text
tenant_id = tenant-from-auth
investigation_id = investigation-from-auth
```

Flow:

```text
MCP
 ↓
SearchRuntimeEvidenceInput
 ↓
RuntimeEvidenceService
 ↓
NormalizedRuntimeQuery
 ↓
query fingerprint
 ↓
ElasticRuntimeQueryBuilder
 ↓
AsyncElasticAdapter
 ↓
Elasticsearch
 ↓
ElasticEvidenceProjector
 ↓
Evidence[]
 ↓
MCP response
```

---

# 46. Example pagination flow

First request:

```text
cursor = null
```

Response:

```json
{
  "items": [...],
  "has_more": true,
  "next_cursor": "signed-token",
  "completeness": {
    "complete": false,
    "truncated": false,
    "reason": "pagination_available"
  }
}
```

Second request:

```text
same query
cursor = signed-token
```

The server:

1. reconstructs normalized query;
2. calculates expected fingerprint;
3. verifies cursor signature;
4. verifies tenant;
5. verifies investigation;
6. verifies query fingerprint;
7. verifies cursor version/expiry;
8. extracts sort values;
9. calls Elasticsearch with `search_after`.

---

# 47. Example invalid cursor

If the agent changes:

```text
service=checkout
```

to:

```text
service=billing
```

while reusing the cursor:

```text
INVALID_CURSOR
```

must be returned.

The service must not silently restart from page one.

---

# 48. Example trace flow

```text
investigate_trace
        ↓
TraceInvestigationService
        ↓
RuntimeEvidenceService
        ↓
AsyncElasticAdapter
        ↓
Evidence[]
        ↓
TraceTelemetryExtractor
        ├── code locations
        ├── SQL
        └── table references
        ↓
Completeness + provenance
        ↓
MCP output
```

Every derived fact should be traceable to evidence IDs.

---

# 49. What MCP should NOT expose

The target server must not expose tools for:

```text
raw Elasticsearch search
raw Elasticsearch DSL
arbitrary index selection
index enumeration
cluster health
cluster configuration
document deletion
document update
bulk operations
script execution
arbitrary SQL execution
arbitrary shell commands
filesystem access
credential retrieval
secret retrieval
```

This MCP service is an **investigation evidence interface**, not an infrastructure administration interface.

---

# 50. Resource design

Do not add MCP resources unless the repository has a clear need.

Potential future read-only resources may include:

```text
investigation://current
investigation://current/evidence/{id}
```

However, initial implementation should prefer tools for parameterized investigation operations.

Avoid duplicating the same data through both resources and tools.

---

# 51. Recommended initial MCP tool surface

Implement only:

```text
search_runtime_evidence
get_evidence
investigate_trace
```

Do not create dozens of narrowly scoped tools.

These three tools correspond directly to the existing runtime evidence and trace-investigation capabilities.

Additional tools should be added only when a concrete investigation workflow requires them.

---

# 52. Implementation sequence for coding agent

## Step 1

Inspect repository conventions and existing ports.

Do not start by creating MCP code.

---

## Step 2

Identify existing:

```text
application services
dependency injection
authentication context
gateway interfaces
exception hierarchy
configuration system
MCP dependencies, if already present
```

---

## Step 3

Create/confirm application investigation services.

---

## Step 4

Refactor Elastic internals into:

```text
cursor.py
query.py
validation.py
projection.py
```

only where useful.

Avoid unnecessary file splitting if repository conventions favor fewer modules.

---

## Step 5

Fix existing Elastic security/correctness defects required by the previous remediation prompt.

The MCP service must not be built on known unsafe cursor/provenance/data-exposure behavior.

---

## Step 6

Implement trace telemetry extraction as a reusable service.

---

## Step 7

Implement MCP schemas.

---

## Step 8

Implement MCP tools.

---

## Step 9

Implement server and dependency wiring.

---

## Step 10

Add contract tests.

---

## Step 11

Add security/adversarial tests.

---

## Step 12

Add integration tests.

---

## Step 13

Run:

```text
format
lint
type-check
unit tests
integration tests
contract tests
security tests
```

---

# 53. Definition of done

The implementation is complete only when:

### Architecture

- [ ] MCP does not access Elasticsearch directly.
- [ ] MCP tools call application services.
- [ ] Application services call existing evidence abstractions.
- [ ] Elastic infrastructure remains provider-specific.
- [ ] Trace extraction is reusable outside MCP.

### Security

- [ ] Tenant comes from trusted context.
- [ ] Investigation comes from trusted context.
- [ ] Arbitrary Elastic DSL is impossible through tool schemas.
- [ ] Tenant/environment index scope is validated.
- [ ] Cursor is cryptographically protected.
- [ ] Cursor is bound to exact normalized query.
- [ ] Invalid cursor never falls back to page one.
- [ ] Sensitive evidence content is projected/redacted.
- [ ] SQL is bounded and treated as untrusted data.

### Correctness

- [ ] Evidence IDs have one documented meaning.
- [ ] Query fingerprint is actually a query fingerprint.
- [ ] Observation time is never fabricated.
- [ ] Provider data is validated.
- [ ] Completeness is explicit.
- [ ] Trace-derived facts contain source evidence references.
- [ ] Table references preserve meaningful qualification.
- [ ] Output ordering is deterministic.

### MCP

- [ ] `search_runtime_evidence` implemented.
- [ ] `get_evidence` implemented.
- [ ] `investigate_trace` implemented.
- [ ] Tool descriptions accurately describe scope and limitations.
- [ ] Input schemas are strict.
- [ ] Output schemas are stable.
- [ ] Errors are safe and typed.

### Testing

- [ ] Unit tests.
- [ ] Application tests.
- [ ] Infrastructure tests.
- [ ] MCP contract tests.
- [ ] Security/adversarial tests.
- [ ] Integration tests.
- [ ] Pagination tests.
- [ ] malformed provider data tests.

### Operations

- [ ] Configuration is validated.
- [ ] Cursor signing key is not hard-coded.
- [ ] Timeouts are coherent.
- [ ] Provider limits remain enforced.
- [ ] Observability is present without logging sensitive evidence.

---

# 54. Final implementation report required

The coding agent must produce:

## Architecture changes

List each new package/file and its responsibility.

## Tool catalog

For each MCP tool:

```text
name
purpose
trusted context
agent-controlled input
output
errors
provider dependencies
```

## Security model

Explain:

```text
authentication
authorization
tenant isolation
investigation isolation
cursor security
input validation
data projection
```

## Evidence semantics

Explain:

```text
evidence ID
provider ID
source location
content fingerprint
query fingerprint
observation time
retrieval time
classification
```

## Completeness semantics

Explain exactly when:

```text
complete = true
complete = false
truncated = true
```

## Test evidence

Report exact commands and results.

## Residual risks

List known limitations and assumptions.

---

# 55. Target architecture summary

The final architecture should look like:

```text
                       INVESTIGATION AGENT
                              │
                              │ MCP
                              ▼
                  ┌───────────────────────┐
                  │     MCP SERVER        │
                  │                       │
                  │  Context              │
                  │  Schemas              │
                  │  Tools                │
                  │  Errors               │
                  └───────────┬───────────┘
                              │
                              ▼
             ┌────────────────────────────────┐
             │ INVESTIGATION APPLICATION      │
             │                                │
             │ RuntimeEvidenceService         │
             │ EvidenceDetailService          │
             │ TraceInvestigationService      │
             └───────────────┬────────────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │ EVIDENCE PORTS       │
                  │ / GATEWAYS           │
                  └──────────┬───────────┘
                             │
                             ▼
             ┌────────────────────────────────┐
             │ ELASTIC INFRASTRUCTURE         │
             │                                │
             │ AsyncElasticAdapter            │
             │ QueryBuilder                   │
             │ CursorCodec                    │
             │ Validation                     │
             │ EvidenceProjection             │
             │ TraceTelemetryExtractor        │
             │ SQLExtractor                   │
             └───────────────┬────────────────┘
                             │
                             ▼
                      ELASTICSEARCH
```

The key design principle is:

> **The MCP server is an agent-facing interface to investigation capabilities; it is not an Elasticsearch interface.**

The coding agent should preserve that boundary throughout implementation.

---

# 56. Concrete MCP tool definitions

The following definitions are implementation contracts, not illustrative pseudocode.

Use JSON Schema **2020-12** for all tool schemas. MCP specifies JSON Schema 2020-12 as the default/required baseline, and tool definitions may provide both `inputSchema` and `outputSchema`. When an output schema is declared, the server must return structured content conforming to it. citeturn0search0turn0search1

All three investigation tools are read-only and idempotent.

The server should expose annotations equivalent to:

```json
{
  "readOnlyHint": true,
  "destructiveHint": false,
  "idempotentHint": true,
  "openWorldHint": true
}
```

Treat annotations as metadata/hints, not as an authorization mechanism. citeturn0search1

---

# 57. Common JSON Schema definitions

The implementation may place these in:

```text
src/investigation_agent_platform/mcp/schemas/common.py
```

or compose them into the individual schemas.

## `InvestigationProvenance`

```json
{
  "$defs": {
    "InvestigationProvenance": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "evidence_id": {
          "type": "string",
          "format": "uuid"
        },
        "provider": {
          "type": "string",
          "minLength": 1,
          "maxLength": 100
        },
        "provider_record_id": {
          "type": ["string", "null"],
          "maxLength": 500
        },
        "source": {
          "type": ["string", "null"],
          "maxLength": 500
        },
        "observed_at": {
          "type": ["string", "null"],
          "format": "date-time"
        },
        "retrieved_at": {
          "type": "string",
          "format": "date-time"
        },
        "query_fingerprint": {
          "type": ["string", "null"],
          "pattern": "^[a-f0-9]{64}$"
        }
      },
      "required": [
        "evidence_id",
        "provider",
        "provider_record_id",
        "source",
        "observed_at",
        "retrieved_at",
        "query_fingerprint"
      ]
    }
  }
}
```

Do not expose:

```text
tenant_id
authorization claims
credentials
Elastic index names
raw Elasticsearch request bodies
```

through this output unless an explicit product requirement establishes a safe reason.

---

# 58. Common completeness schema

```json
{
  "$defs": {
    "Completeness": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "complete": {
          "type": "boolean"
        },
        "truncated": {
          "type": "boolean"
        },
        "reason": {
          "type": ["string", "null"],
          "enum": [
            null,
            "pagination_available",
            "provider_limit",
            "application_limit",
            "provider_failure",
            "metadata_incomplete",
            "input_bound"
          ]
        },
        "returned_count": {
          "type": "integer",
          "minimum": 0
        },
        "examined_count": {
          "type": "integer",
          "minimum": 0
        }
      },
      "required": [
        "complete",
        "truncated",
        "reason",
        "returned_count",
        "examined_count"
      ]
    }
  }
}
```

Important semantic distinction:

```text
has_more=true
```

means another page exists.

It does not necessarily mean the current result is truncated by an error.

Therefore:

```text
pagination_available
```

is not the same as:

```text
provider_limit
```

---

# 59. Common evidence item schema

```json
{
  "$defs": {
    "EvidenceItem": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "evidence_id": {
          "type": "string",
          "format": "uuid"
        },
        "evidence_type": {
          "type": "string",
          "minLength": 1,
          "maxLength": 100
        },
        "summary": {
          "type": "string",
          "maxLength": 2000
        },
        "observed_at": {
          "type": ["string", "null"],
          "format": "date-time"
        },
        "attributes": {
          "type": "object",
          "additionalProperties": true
        },
        "provenance": {
          "$ref": "#/$defs/InvestigationProvenance"
        }
      },
      "required": [
        "evidence_id",
        "evidence_type",
        "summary",
        "observed_at",
        "attributes",
        "provenance"
      ]
    }
  }
}
```

The `attributes` object is still bounded by the projection layer.

`additionalProperties: true` here does **not** mean the Elastic `_source` may be passed through.

The projector must construct the object from an allowlisted/bounded representation.

---

# 60. Tool definition — `search_runtime_evidence`

## MCP registration

```json
{
  "name": "search_runtime_evidence",
  "title": "Search Runtime Evidence",
  "description": "Search bounded runtime evidence within the current authenticated investigation. Results are automatically scoped to the authorized tenant and investigation. Use the returned next_cursor only with the same search criteria. This tool does not accept raw Elasticsearch queries, index names, or authorization context.",
  "inputSchema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "environment": {
        "type": "string",
        "minLength": 1,
        "maxLength": 100,
        "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$",
        "description": "Application environment to investigate, for example production or staging."
      },
      "identifiers": {
        "type": ["object", "null"],
        "additionalProperties": {
          "type": "string",
          "minLength": 1,
          "maxLength": 500
        },
        "maxProperties": 10,
        "description": "Supported investigation identifiers such as trace_id, request_id, correlation_id, or service-specific identifiers."
      },
      "keywords": {
        "type": ["array", "null"],
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 200
        },
        "maxItems": 20,
        "uniqueItems": true
      },
      "services": {
        "type": ["array", "null"],
        "items": {
          "type": "string",
          "minLength": 1,
          "maxLength": 200
        },
        "maxItems": 20,
        "uniqueItems": true
      },
      "severities": {
        "type": ["array", "null"],
        "items": {
          "type": "string",
          "enum": [
            "TRACE",
            "DEBUG",
            "INFO",
            "WARN",
            "WARNING",
            "ERROR",
            "FATAL"
          ]
        },
        "maxItems": 10,
        "uniqueItems": true
      },
      "start_time": {
        "type": ["string", "null"],
        "format": "date-time"
      },
      "end_time": {
        "type": ["string", "null"],
        "format": "date-time"
      },
      "limit": {
        "type": "integer",
        "minimum": 1,
        "maximum": 100
      },
      "cursor": {
        "type": ["string", "null"],
        "minLength": 1,
        "maxLength": 4096
      }
    },
    "required": [
      "environment",
      "identifiers",
      "keywords",
      "services",
      "severities",
      "start_time",
      "end_time",
      "limit",
      "cursor"
    ]
  },
  "outputSchema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$defs": {
      "InvestigationProvenance": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "evidence_id": {
            "type": "string",
            "format": "uuid"
          },
          "provider": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100
          },
          "provider_record_id": {
            "type": ["string", "null"],
            "maxLength": 500
          },
          "source": {
            "type": ["string", "null"],
            "maxLength": 500
          },
          "observed_at": {
            "type": ["string", "null"],
            "format": "date-time"
          },
          "retrieved_at": {
            "type": "string",
            "format": "date-time"
          },
          "query_fingerprint": {
            "type": ["string", "null"],
            "pattern": "^[a-f0-9]{64}$"
          }
        },
        "required": [
          "evidence_id",
          "provider",
          "provider_record_id",
          "source",
          "observed_at",
          "retrieved_at",
          "query_fingerprint"
        ]
      },
      "EvidenceItem": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "evidence_id": {
            "type": "string",
            "format": "uuid"
          },
          "evidence_type": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100
          },
          "summary": {
            "type": "string",
            "maxLength": 2000
          },
          "observed_at": {
            "type": ["string", "null"],
            "format": "date-time"
          },
          "attributes": {
            "type": "object",
            "additionalProperties": true
          },
          "provenance": {
            "$ref": "#/$defs/InvestigationProvenance"
          }
        },
        "required": [
          "evidence_id",
          "evidence_type",
          "summary",
          "observed_at",
          "attributes",
          "provenance"
        ]
      },
      "Completeness": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "complete": {
            "type": "boolean"
          },
          "truncated": {
            "type": "boolean"
          },
          "reason": {
            "type": ["string", "null"],
            "enum": [
              null,
              "pagination_available",
              "provider_limit",
              "application_limit",
              "provider_failure",
              "metadata_incomplete",
              "input_bound"
            ]
          },
          "returned_count": {
            "type": "integer",
            "minimum": 0
          },
          "examined_count": {
            "type": "integer",
            "minimum": 0
          }
        },
        "required": [
          "complete",
          "truncated",
          "reason",
          "returned_count",
          "examined_count"
        ]
      }
    },
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "items": {
        "type": "array",
        "items": {
          "$ref": "#/$defs/EvidenceItem"
        },
        "maxItems": 100
      },
      "next_cursor": {
        "type": ["string", "null"],
        "maxLength": 4096
      },
      "has_more": {
        "type": "boolean"
      },
      "total_count": {
        "type": ["integer", "null"],
        "minimum": 0
      },
      "completeness": {
        "$ref": "#/$defs/Completeness"
      }
    },
    "required": [
      "items",
      "next_cursor",
      "has_more",
      "total_count",
      "completeness"
    ]
  },
  "annotations": {
    "readOnlyHint": true,
    "destructiveHint": false,
    "idempotentHint": true,
    "openWorldHint": true
  }
}
```

---

# 61. `search_runtime_evidence` semantic rules

The coding agent must implement these rules:

### Empty filters

This is permitted only if the application policy permits broad investigation searches.

If broad searches are not permitted, reject with:

```text
INVALID_REQUEST
```

Do not silently manufacture filters.

### Time range

If both are supplied:

```text
start_time <= end_time
```

must hold.

Enforce a configured maximum window.

### Identifiers

Identifier keys must come from an application allowlist.

Do not allow the agent to invent an arbitrary field path such as:

```text
"_source.password"
```

or:

```text
"foo.bar.baz"
```

### Severity

Unknown severity must fail validation.

Do not discard invalid severity values.

### Limit

The schema maximum is not the only protection.

The application/provider layer must enforce its own maximum.

---

# 62. Tool definition — `get_evidence`

## Input schema

```json
{
  "name": "get_evidence",
  "title": "Get Evidence",
  "description": "Retrieve one evidence item from the current authenticated investigation. The evidence ID is not an authorization credential; the server verifies that the item belongs to the authorized investigation scope.",
  "inputSchema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "evidence_id": {
        "type": "string",
        "format": "uuid",
        "description": "Canonical investigation-agent-platform evidence identifier."
      }
    },
    "required": [
      "evidence_id"
    ]
  },
  "annotations": {
    "readOnlyHint": true,
    "destructiveHint": false,
    "idempotentHint": true,
    "openWorldHint": true
  }
}
```

## Output schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "evidence_id": {
      "type": "string",
      "format": "uuid"
    },
    "evidence_type": {
      "type": "string",
      "minLength": 1,
      "maxLength": 100
    },
    "summary": {
      "type": "string",
      "maxLength": 4000
    },
    "observed_at": {
      "type": ["string", "null"],
      "format": "date-time"
    },
    "attributes": {
      "type": "object",
      "additionalProperties": true
    },
    "provenance": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "evidence_id": {
          "type": "string",
          "format": "uuid"
        },
        "provider": {
          "type": "string",
          "minLength": 1,
          "maxLength": 100
        },
        "provider_record_id": {
          "type": ["string", "null"],
          "maxLength": 500
        },
        "source": {
          "type": ["string", "null"],
          "maxLength": 500
        },
        "observed_at": {
          "type": ["string", "null"],
          "format": "date-time"
        },
        "retrieved_at": {
          "type": "string",
          "format": "date-time"
        },
        "query_fingerprint": {
          "type": ["string", "null"],
          "pattern": "^[a-f0-9]{64}$"
        }
      },
      "required": [
        "evidence_id",
        "provider",
        "provider_record_id",
        "source",
        "observed_at",
        "retrieved_at",
        "query_fingerprint"
      ]
    }
  },
  "required": [
    "evidence_id",
    "evidence_type",
    "summary",
    "observed_at",
    "attributes",
    "provenance"
  ]
}
```

---

# 63. Tool definition — `investigate_trace`

## Input schema

```json
{
  "name": "investigate_trace",
  "title": "Investigate Trace",
  "description": "Investigate a trace within the current authenticated investigation and return bounded runtime evidence, code locations, SQL statements, and table references. Every derived finding retains source evidence IDs. Results explicitly report completeness and truncation.",
  "inputSchema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "trace_id": {
        "type": "string",
        "minLength": 1,
        "maxLength": 200,
        "pattern": "^[A-Za-z0-9._:-]+$",
        "description": "Trace identifier to investigate."
      },
      "environment": {
        "type": "string",
        "minLength": 1,
        "maxLength": 100,
        "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]*$"
      },
      "start_time": {
        "type": ["string", "null"],
        "format": "date-time"
      },
      "end_time": {
        "type": ["string", "null"],
        "format": "date-time"
      },
      "max_evidence": {
        "type": "integer",
        "minimum": 1,
        "maximum": 100
      }
    },
    "required": [
      "trace_id",
      "environment",
      "start_time",
      "end_time",
      "max_evidence"
    ]
  },
  "annotations": {
    "readOnlyHint": true,
    "destructiveHint": false,
    "idempotentHint": true,
    "openWorldHint": true
  }
}
```

---

# 64. `investigate_trace` output schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$defs": {
    "EvidenceRef": {
      "type": "string",
      "format": "uuid"
    },
    "CodeLocation": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "function": {
          "type": ["string", "null"],
          "maxLength": 500
        },
        "file_path": {
          "type": ["string", "null"],
          "maxLength": 2000
        },
        "line": {
          "type": ["integer", "null"],
          "minimum": 1
        },
        "evidence_ids": {
          "type": "array",
          "items": {
            "$ref": "#/$defs/EvidenceRef"
          },
          "minItems": 1,
          "maxItems": 20,
          "uniqueItems": true
        }
      },
      "required": [
        "function",
        "file_path",
        "line",
        "evidence_ids"
      ]
    },
    "TableReference": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "catalog": {
          "type": ["string", "null"],
          "maxLength": 500
        },
        "schema": {
          "type": ["string", "null"],
          "maxLength": 500
        },
        "table": {
          "type": "string",
          "minLength": 1,
          "maxLength": 500
        },
        "evidence_ids": {
          "type": "array",
          "items": {
            "$ref": "#/$defs/EvidenceRef"
          },
          "minItems": 1,
          "maxItems": 20,
          "uniqueItems": true
        }
      },
      "required": [
        "catalog",
        "schema",
        "table",
        "evidence_ids"
      ]
    },
    "SqlStatement": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "statement": {
          "type": "string",
          "maxLength": 20000
        },
        "evidence_id": {
          "$ref": "#/$defs/EvidenceRef"
        },
        "dialect": {
          "type": ["string", "null"],
          "maxLength": 100
        },
        "parse_status": {
          "type": "string",
          "enum": [
            "parsed",
            "partial",
            "failed",
            "not_attempted"
          ]
        },
        "truncated": {
          "type": "boolean"
        }
      },
      "required": [
        "statement",
        "evidence_id",
        "dialect",
        "parse_status",
        "truncated"
      ]
    },
    "Completeness": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "complete": {
          "type": "boolean"
        },
        "truncated": {
          "type": "boolean"
        },
        "reason": {
          "type": ["string", "null"],
          "enum": [
            null,
            "pagination_available",
            "provider_limit",
            "application_limit",
            "provider_failure",
            "metadata_incomplete",
            "input_bound"
          ]
        },
        "returned_count": {
          "type": "integer",
          "minimum": 0
        },
        "examined_count": {
          "type": "integer",
          "minimum": 0
        }
      },
      "required": [
        "complete",
        "truncated",
        "reason",
        "returned_count",
        "examined_count"
      ]
    }
  },
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "trace_id": {
      "type": "string",
      "minLength": 1,
      "maxLength": 200
    },
    "code_locations": {
      "type": "array",
      "items": {
        "$ref": "#/$defs/CodeLocation"
      },
      "maxItems": 100
    },
    "tables": {
      "type": "array",
      "items": {
        "$ref": "#/$defs/TableReference"
      },
      "maxItems": 100
    },
    "sql_statements": {
      "type": "array",
      "items": {
        "$ref": "#/$defs/SqlStatement"
      },
      "maxItems": 100
    },
    "evidence": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "properties": {
          "evidence_id": {
            "type": "string",
            "format": "uuid"
          },
          "evidence_type": {
            "type": "string",
            "maxLength": 100
          },
          "summary": {
            "type": "string",
            "maxLength": 2000
          }
        },
        "required": [
          "evidence_id",
          "evidence_type",
          "summary"
        ]
      },
      "maxItems": 100
    },
    "completeness": {
      "$ref": "#/$defs/Completeness"
    }
  },
  "required": [
    "trace_id",
    "code_locations",
    "tables",
    "sql_statements",
    "evidence",
    "completeness"
  ]
}
```

---

# 65. MCP error contract

Tool-level failures should be returned as tool results with a stable structured error object rather than leaking provider exceptions.

Use:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "properties": {
    "error": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "code": {
          "type": "string",
          "enum": [
            "INVALID_REQUEST",
            "INVALID_CURSOR",
            "CURSOR_EXPIRED",
            "FORBIDDEN",
            "EVIDENCE_NOT_FOUND",
            "PROVIDER_TIMEOUT",
            "PROVIDER_UNAVAILABLE",
            "INCOMPLETE_EVIDENCE",
            "INTERNAL_ERROR"
          ]
        },
        "message": {
          "type": "string",
          "maxLength": 1000
        },
        "retryable": {
          "type": "boolean"
        }
      },
      "required": [
        "code",
        "message",
        "retryable"
      ]
    }
  },
  "required": [
    "error"
  ]
}
```

Never expose an internal exception class name as the agent-facing error contract.

---

# 66. Agent investigation session — example 1: identify an incident from runtime evidence

The following is an example of the intended **agent behavior**, not a hard-coded workflow.

## User request

```text
Investigate why checkout requests started timing out around 14:05 UTC.
```

## Step 1 — search runtime evidence

Agent invokes:

```json
{
  "name": "search_runtime_evidence",
  "arguments": {
    "environment": "production",
    "identifiers": null,
    "keywords": [
      "timeout",
      "checkout"
    ],
    "services": [
      "checkout"
    ],
    "severities": [
      "ERROR",
      "WARN"
    ],
    "start_time": "2026-09-28T14:00:00Z",
    "end_time": "2026-09-28T14:15:00Z",
    "limit": 25,
    "cursor": null
  }
}
```

Important:

The agent does **not** send:

```json
{
  "tenant_id": "...",
  "investigation_id": "...",
  "index": "..."
}
```

Those values are supplied by trusted server context.

## Step 2 — inspect evidence

The result contains evidence IDs:

```json
{
  "items": [
    {
      "evidence_id": "11111111-1111-4111-8111-111111111111",
      "evidence_type": "runtime_log",
      "summary": "checkout request exceeded upstream timeout",
      "observed_at": "2026-09-28T14:04:31Z",
      "attributes": {
        "service": "checkout",
        "severity": "ERROR"
      },
      "provenance": {
        "evidence_id": "11111111-1111-4111-8111-111111111111",
        "provider": "elasticsearch",
        "provider_record_id": "abc123",
        "source": "runtime",
        "observed_at": "2026-09-28T14:04:31Z",
        "retrieved_at": "2026-09-28T14:06:02Z",
        "query_fingerprint": "..."
      }
    }
  ],
  "next_cursor": null,
  "has_more": false,
  "total_count": 1,
  "completeness": {
    "complete": true,
    "truncated": false,
    "reason": null,
    "returned_count": 1,
    "examined_count": 1
  }
}
```

The agent now has a factual observation and an evidence ID.

---

# 67. Agent investigation session — example 2: follow a trace

## User request

```text
Follow the trace from the timeout and determine what code and database operations were involved.
```

Suppose the previous evidence identified:

```text
trace_id = 4bf92f3577b34da6a3ce929d0e0e4736
```

The agent invokes:

```json
{
  "name": "investigate_trace",
  "arguments": {
    "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
    "environment": "production",
    "start_time": "2026-09-28T14:00:00Z",
    "end_time": "2026-09-28T14:15:00Z",
    "max_evidence": 100
  }
}
```

Possible result:

```json
{
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "code_locations": [
    {
      "function": "checkout.submit",
      "file_path": "/app/checkout.py",
      "line": 142,
      "evidence_ids": [
        "22222222-2222-4222-8222-222222222222"
      ]
    }
  ],
  "tables": [
    {
      "catalog": null,
      "schema": "payments",
      "table": "transactions",
      "evidence_ids": [
        "33333333-3333-4333-8333-333333333333"
      ]
    }
  ],
  "sql_statements": [
    {
      "statement": "SELECT id, status FROM payments.transactions WHERE id = ?",
      "evidence_id": "33333333-3333-4333-8333-333333333333",
      "dialect": "postgresql",
      "parse_status": "parsed",
      "truncated": false
    }
  ],
  "evidence": [
    {
      "evidence_id": "22222222-2222-4222-8222-222222222222",
      "evidence_type": "trace_span",
      "summary": "checkout.submit span"
    },
    {
      "evidence_id": "33333333-3333-4333-8333-333333333333",
      "evidence_type": "database_query",
      "summary": "Database query against payments.transactions"
    }
  ],
  "completeness": {
    "complete": true,
    "truncated": false,
    "reason": null,
    "returned_count": 2,
    "examined_count": 2
  }
}
```

The agent can now say:

```text
The trace includes checkout.submit at /app/checkout.py:142 and a database query against payments.transactions. Both findings are backed by separate evidence records.
```

It should not claim causality that the evidence does not establish.

---

# 68. Agent investigation session — example 3: pagination

Suppose the first search returns:

```json
{
  "items": [ "...25 items..." ],
  "next_cursor": "eyJ2IjoxLCJ0ZW5hbnQiOi...",
  "has_more": true,
  "completeness": {
    "complete": false,
    "truncated": false,
    "reason": "pagination_available",
    "returned_count": 25,
    "examined_count": 25
  }
}
```

The agent should recognize:

```text
The first page is not the complete result set.
```

It invokes the same tool with the same filters and:

```json
{
  "cursor": "eyJ2IjoxLCJ0ZW5hbnQiOi..."
}
```

If it changes:

```text
services = ["checkout"]
```

to:

```text
services = ["payments"]
```

while reusing the cursor, the server returns:

```json
{
  "error": {
    "code": "INVALID_CURSOR",
    "message": "The pagination cursor does not match the current search criteria.",
    "retryable": false
  }
}
```

The agent must restart pagination with:

```text
cursor = null
```

for the new query.

---

# 69. Agent investigation session — example 4: incomplete provider result

Suppose trace investigation reaches the configured provider limit.

The server returns:

```json
{
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "code_locations": [],
  "tables": [],
  "sql_statements": [],
  "evidence": [],
  "completeness": {
    "complete": false,
    "truncated": true,
    "reason": "provider_limit",
    "returned_count": 100,
    "examined_count": 100
  }
}
```

The agent must interpret this as:

```text
The investigation is incomplete.
```

It must **not** conclude:

```text
No database activity occurred.
```

A suitable next action is to narrow the investigation using a more specific trace/time window if the tool contract permits it.

---

# 70. Agent investigation session — example 5: retrieve a specific evidence item

After `investigate_trace`, the agent needs the details behind a SQL finding.

It invokes:

```json
{
  "name": "get_evidence",
  "arguments": {
    "evidence_id": "33333333-3333-4333-8333-333333333333"
  }
}
```

The service checks:

```text
authenticated tenant
        +
authenticated investigation
        +
evidence ownership/scope
```

before returning the evidence.

If the evidence does not belong to the investigation:

```json
{
  "error": {
    "code": "EVIDENCE_NOT_FOUND",
    "message": "The requested evidence is not available in the current investigation.",
    "retryable": false
  }
}
```

Do not return:

```text
"You are not authorized because this evidence belongs to tenant X."
```

That would disclose information about another investigation/tenant.

---

# 71. Agent investigation session — example 6: adversarial tool use

The agent attempts:

```json
{
  "name": "search_runtime_evidence",
  "arguments": {
    "environment": "production",
    "identifiers": {
      "_source.password": "secret"
    },
    "keywords": null,
    "services": null,
    "severities": null,
    "start_time": null,
    "end_time": null,
    "limit": 100,
    "cursor": null
  }
}
```

The server rejects the identifier key because it is not in the supported identifier allowlist.

The agent attempts:

```json
{
  "name": "search_runtime_evidence",
  "arguments": {
    "environment": "production",
    "identifiers": null,
    "keywords": [
      "{\"query\":{\"match_all\":{}}}"
    ],
    "services": null,
    "severities": null,
    "start_time": null,
    "end_time": null,
    "limit": 100,
    "cursor": null
  }
}
```

This remains ordinary keyword input.

The application layer must ensure that the keyword is treated as data, never as Elasticsearch DSL.

There is no MCP argument named:

```text
query
body
dsl
index
script
aggregation
```

so a raw provider query cannot be expressed through the tool contract.

---

# 72. Agent investigation strategy

The MCP tool descriptions should encourage this investigation pattern:

```text
1. Establish the investigation window.
2. Search for bounded runtime evidence.
3. Identify stable evidence IDs and trace IDs.
4. Follow relevant traces.
5. Inspect derived code/database findings.
6. Retrieve individual evidence records when details are required.
7. Continue pagination when has_more=true.
8. Narrow the query when completeness=false and truncation prevents a conclusion.
9. Cite evidence IDs when presenting findings.
10. Distinguish observations from hypotheses.
```

The MCP server should provide facts and provenance.

The agent remains responsible for reasoning over those facts.

---

# 73. Example complete investigation transcript

```text
USER:
Investigate the checkout timeout around 14:05 UTC.

AGENT:
I will first search runtime evidence around the reported time.

TOOL:
search_runtime_evidence(
  environment="production",
  keywords=["timeout", "checkout"],
  services=["checkout"],
  severities=["ERROR", "WARN"],
  start_time="2026-09-28T14:00:00Z",
  end_time="2026-09-28T14:15:00Z",
  limit=25
)

TOOL RESULT:
1 runtime evidence item
trace_id = 4bf92f3577b34da6a3ce929d0e0e4736
evidence_id = 11111111-1111-4111-8111-111111111111
complete = true

AGENT:
The runtime evidence identifies a trace. I will inspect that trace for code and database activity.

TOOL:
investigate_trace(
  trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
  environment="production",
  start_time="2026-09-28T14:00:00Z",
  end_time="2026-09-28T14:15:00Z",
  max_evidence=100
)

TOOL RESULT:
code_locations:
  /app/checkout.py:142 checkout.submit

tables:
  payments.transactions

sql:
  SELECT id, status FROM payments.transactions WHERE id = ?

completeness:
  complete=true
  truncated=false

AGENT:
The trace contains checkout.submit at /app/checkout.py:142 and a database operation against payments.transactions. I will retrieve the database evidence for additional context.

TOOL:
get_evidence(
  evidence_id="33333333-3333-4333-8333-333333333333"
)

TOOL RESULT:
database query evidence + provenance

AGENT:
Final investigation:
- Observed timeout evidence at 14:04:31 UTC.
- The associated trace includes checkout.submit at /app/checkout.py:142.
- The trace includes a database query against payments.transactions.
- The investigation results were complete for the requested bounded trace window.
- These observations are backed by evidence IDs ...
```

This transcript demonstrates the intended separation:

```text
agent reasoning
      ↓
MCP tool
      ↓
application service
      ↓
evidence infrastructure
      ↓
provider
```

The agent never needs to know that Elasticsearch exists.

---

# 74. Tool catalog contract test

The coding agent must add a test that effectively asserts:

```python
EXPECTED_TOOLS = {
    "search_runtime_evidence",
    "get_evidence",
    "investigate_trace",
}
```

and verifies:

```text
all expected tools exist
no unexpected infrastructure/admin tools exist
all tools have inputSchema
all tools have outputSchema where applicable
all schemas are valid JSON Schema
all tool names are unique
```

The MCP specification defines tool names, descriptions, input schemas, optional output schemas, and annotations as part of the tool definition. citeturn0search1turn0search4

---

# 75. Schema contract tests

Validate every schema using a JSON Schema 2020-12 validator.

Tests must include:

### Valid requests

```text
normal runtime search
search with identifiers
search with time range
first page
subsequent page
trace investigation
evidence retrieval
```

### Invalid requests

```text
unknown properties
limit=0
limit>100
empty evidence ID
malformed UUID
invalid datetime
start_time > end_time
unknown severity
too many keywords
too many services
oversized identifier
invalid trace ID
oversized cursor
```

The test suite must assert rejection at the MCP/schema boundary where possible and at the application boundary as defense in depth.

---

# 76. Schema evolution rules

Treat MCP schemas as public contracts.

When changing a schema:

1. determine backward compatibility;
2. update contract tests;
3. update tool description;
4. update agent session examples;
5. update versioning/documentation if required;
6. do not silently reinterpret an existing field.

In particular, do not change the semantics of:

```text
evidence_id
cursor
completeness
observed_at
retrieved_at
query_fingerprint
```

without an explicit migration strategy.

---

# 77. Final coding-agent instruction

Implement the MCP layer exactly as an investigation capability boundary.

The coding agent must not optimize for:

```text
"make Elasticsearch available to the agent"
```

It must optimize for:

```text
"give the investigation agent safe, bounded, provenance-preserving
access to the evidence capabilities of investigation-agent-platform"
```

The resulting MCP interface should make the safe path easy and the unsafe path structurally unavailable.

The final implementation must include:

```text
MCP server
    +
3 investigation tools
    +
strict input schemas
    +
strict output schemas
    +
trusted investigation context
    +
application services
    +
existing evidence abstraction
    +
hardened Elastic infrastructure
    +
cursor/query binding
    +
provenance
    +
completeness semantics
    +
trace evidence references
    +
contract tests
    +
adversarial tests
    +
agent-session documentation
```

Do not add raw infrastructure tools merely because they are convenient during implementation.
