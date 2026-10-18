# Coding Agent Implementation Prompt — MCP Investigation Service

## Role

You are a principal Python engineer implementing an MCP service for the **`investigation-agent-platform`**.

Your task is to extend the existing platform so an AI investigation agent can safely and deterministically investigate application issues through MCP tools.

This is an implementation task, not a conceptual design exercise. Inspect the repository before changing code, preserve existing architectural conventions, and integrate with the platform's domain/port/infrastructure boundaries rather than introducing a parallel architecture.

---

# 1. Objective

Create an MCP service that exposes investigation-oriented tools backed by the platform's existing evidence infrastructure.

The MCP service must allow an investigation agent to:

1. Search runtime evidence/logs.
2. Retrieve a specific evidence record.
3. Search runtime telemetry for a trace.
4. Extract code locations associated with a trace.
5. Extract database/table lineage associated with a trace.
6. Inspect the raw/runtime evidence needed to continue an investigation.
7. Paginate safely through large result sets.
8. Preserve tenant and investigation isolation.
9. Preserve provenance, correlation, and evidence identity.
10. Return structured, bounded, machine-readable results suitable for an LLM agent.
11. Fail safely and explicitly when an investigation request is invalid, unauthorized, too expensive, or unsupported.

The MCP layer is an agent-facing adapter. It must not bypass the existing evidence policies.

---

# 2. Existing implementation that must be preserved and reused

The existing Elastic runtime adapter is:

`src/investigation_agent_platform/infrastructure/evidence/runtime/elastic.py`

It already provides:

- `AsyncElasticAdapter`
- `get_runtime_evidence(...)`
- `search_runtime_evidence(...)`
- signed pagination cursors
- tenant/application binding for cursors
- provider-side result limits
- provider-side time-window limits
- keyword limits
- service limits
- allowed identifier-label filtering
- severity filtering
- service filtering
- keyword searching
- timestamp filtering
- provenance construction
- evidence freshness construction
- OpenTelemetry tracing

The adapter explicitly enforces provider-side bounds rather than trusting an LLM-generated request. Preserve this security model. The current provider ceilings include:

- `ELASTIC_MAX_HITS = 200`
- `ELASTIC_MAX_TIME_WINDOW_SECONDS = 7 * 24 * 3600`
- `ELASTIC_MAX_KEYWORD_TERMS = 20`
- `ELASTIC_MAX_SERVICES = 10`

The implementation also signs pagination cursors with HMAC and binds them to tenant/application context. Do not replace this with client-controlled unsigned pagination state.

Relevant existing implementation:

- `AsyncElasticAdapter._encode_signed_cursor(...)`
- `AsyncElasticAdapter._decode_signed_cursor(...)`
- `AsyncElasticAdapter.get_runtime_evidence(...)`
- `AsyncElasticAdapter.search_runtime_evidence(...)`
- `AsyncElasticAdapter._map_hit_to_evidence(...)`

Source grounding:

- The adapter's tenant-bound signed cursor and provider limits are implemented in `elastic.py`. fileciteturn0file0L40-L56
- Runtime evidence search already clamps caller-controlled limits, keywords, services, and severities at the provider boundary. fileciteturn0file0L147-L170
- Time-range validation and the bounded Elasticsearch query are already implemented. fileciteturn0file0L181-L221
- Pagination is based on signed `search_after` state rather than arbitrary deep pagination. fileciteturn0file0L224-L254

The existing Elastic incident bridge is:

`src/investigation_agent_platform/infrastructure/evidence/logs/elastic_bridge.py`

It currently provides:

`ElasticIncidentBridge.extract_runtime_telemetry_for_trace(...)`

It:

1. Builds a `RuntimeEvidenceRequest`.
2. Filters by `trace_id`.
3. Delegates querying to `AsyncElasticAdapter`.
4. Extracts ECS code location information.
5. Extracts SQL statements.
6. Parses SQL with SQLGlot.
7. Produces accessed-table information.

Source grounding:

- The bridge deliberately delegates execution to `AsyncElasticAdapter`, thereby reusing its limits and security policies. fileciteturn0file1L21-L47
- It extracts code locations from ECS fields and parses database statements with SQLGlot. fileciteturn0file1L61-L92
- It returns trace ID, code locations, accessed tables, and raw SQL statements. fileciteturn0file1L54-L58

---

# 3. Critical architectural requirement

Do NOT implement the MCP server by directly querying Elasticsearch from MCP tool handlers.

The dependency direction must remain:

```text
MCP Client / Investigation Agent
              |
              v
       MCP Tool Handlers
              |
              v
    Investigation Application
              |
              v
       Evidence Ports
              |
              v
   AsyncElasticAdapter / Bridge
              |
              v
        Elasticsearch
```

The MCP layer must not become a second evidence-access implementation.

If the repository already contains application services, use cases, dependency injection, ports, request models, response models, authentication/context objects, or MCP conventions, integrate with those existing abstractions.

If an appropriate application service does not exist, introduce one rather than placing business logic directly in MCP handlers.

---

# 4. MCP tools to implement

Implement a focused tool set. Tool names should be stable, descriptive, and optimized for agent use.

Recommended public MCP tools:

## 4.1 `search_runtime_evidence`

Purpose:

Search application runtime evidence/logs for an investigation.

Input should support, subject to existing domain model capabilities:

- `tenant_id` or authenticated tenant context
- `investigation_id`
- `environment`
- `keywords`
- `services`
- `severities`
- `identifiers`
- `time_range`
- `limit`
- `cursor`

Do not expose arbitrary Elasticsearch DSL.

The MCP schema must explicitly describe:

- accepted values
- limits
- timestamp format
- pagination behavior
- identifier semantics
- failure behavior

Return structured data containing at minimum:

```json
{
  "items": [],
  "has_more": false,
  "next_cursor": null,
  "total_count": 0
}
```

Each item must retain enough evidence identity/provenance information for the agent to reference it in subsequent operations.

Do not return an unbounded raw Elasticsearch response.

---

# 5. `get_runtime_evidence`

Expose a tool for retrieving a single evidence item.

Input:

- `tenant_id` from trusted execution context where possible
- `investigation_id`
- `evidence_id`

Use the existing:

`AsyncElasticAdapter.get_runtime_evidence(...)`

Do not reimplement the Elastic lookup.

The response must preserve:

- evidence ID
- evidence type
- provider
- source
- title
- summary
- content snippet
- content URI
- fingerprint
- classification
- observed timestamp
- retrieval timestamp
- attributes
- provenance
- freshness

Sensitive/raw attributes should be returned only according to the platform's classification and authorization policy.

---

# 6. `get_trace_telemetry`

Expose a tool for trace-centered investigation.

Input:

- `tenant_id`
- `investigation_id`
- `trace_id`
- `environment`
- optional `time_range`

Delegate to:

`ElasticIncidentBridge.extract_runtime_telemetry_for_trace(...)`

Do not duplicate its Elastic querying logic.

Return structured telemetry:

```json
{
  "trace_id": "...",
  "code_locations": [],
  "accessed_tables": [],
  "raw_sql_statements": []
}
```

However, improve the bridge/application contract where necessary so MCP output is safe and useful to an investigation agent.

---

# 7. Code-location extraction requirements

The existing bridge recognizes ECS-style code metadata:

- function
- filepath/file.name
- lineno/file.line

Preserve compatibility with these representations.

Normalize returned code locations to a stable schema, for example:

```json
{
  "function": "...",
  "file_path": "...",
  "line": 123
}
```

Requirements:

- Deduplicate identical locations.
- Preserve multiple distinct locations.
- Validate line numbers.
- Avoid fabricating source locations.
- Represent missing values as `null`, not misleading placeholders.
- Preserve source/evidence references where possible so the agent can trace a conclusion back to evidence.

---

# 8. SQL/database lineage requirements

The existing bridge parses runtime SQL using SQLGlot and extracts table names.

Preserve this behavior but harden the boundary.

Requirements:

1. Never execute SQL returned from logs.
2. Treat SQL as untrusted evidence.
3. Preserve raw SQL only when allowed by the platform's classification policy.
4. Normalize accessed table names deterministically.
5. Deduplicate table names.
6. Preserve parsing failures as structured metadata where useful.
7. Do not claim lineage when parsing did not establish it.
8. Support multiple SQL statements if the domain data can contain them.
9. Avoid allowing SQL content to become executable MCP input.
10. Ensure malformed SQL cannot fail the entire trace investigation.

The existing bridge intentionally catches SQL parsing failures and continues. Preserve this resilience. fileciteturn0file1L81-L95

---

# 9. MCP server architecture

Create an MCP server module under the repository's existing infrastructure/application conventions.

Prefer a structure similar to:

```text
src/investigation_agent_platform/
    application/
        investigation/
            ...
    infrastructure/
        mcp/
            server.py
            context.py
            schemas.py
            tools/
                runtime_evidence.py
                trace_telemetry.py
            errors.py
```

Adapt this layout to the actual repository conventions rather than blindly creating duplicate layers.

The MCP server must have:

- server initialization
- dependency injection
- request/context extraction
- authentication/authorization integration
- tool registration
- typed input models
- typed output models
- exception mapping
- structured logging
- OpenTelemetry tracing
- graceful startup/shutdown

If the project already has an MCP framework/dependency, use it. Do not introduce another MCP implementation without checking the dependency configuration.

---

# 10. Security model

This is a security-sensitive agent-facing interface.

The implementation must assume:

> MCP inputs can be generated by an LLM and therefore must be treated as untrusted input.

Never rely on system prompts to enforce security.

## 10.1 Tenant isolation

Tenant identity must come from trusted authenticated execution context whenever the platform supports it.

Do not permit an agent to change tenant scope by merely supplying another `tenant_id`.

If the current architecture requires tenant_id as an explicit request field, validate it against authenticated context before calling infrastructure.

Every evidence query must remain tenant-scoped.

The existing Elastic implementation uses tenant-specific index patterns such as:

`logs-{tenant_id}-*-*`

and must remain tenant constrained. fileciteturn0file0L106-L123

## 10.2 Investigation isolation

Every investigation-scoped operation must bind results to the current investigation.

Do not accept an investigation ID merely as metadata if it can be used to cross-reference another investigation.

The existing signed cursor is already bound to tenant and investigation/application context. Preserve that behavior. fileciteturn0file0L73-L99

## 10.3 No arbitrary Elasticsearch query

Do not expose:

- raw Elasticsearch DSL
- arbitrary index names
- arbitrary field paths
- script queries
- painless scripts
- arbitrary aggregations
- arbitrary sort fields
- arbitrary `search_after`
- arbitrary query bodies

The MCP contract must expose a constrained investigation query language backed by the existing request model.

## 10.4 Input bounds

Enforce limits at multiple layers:

1. MCP schema validation.
2. Application service validation.
3. Provider/infrastructure validation.

Never rely only on the MCP schema because another caller may invoke the application layer.

The existing provider-side limits must remain authoritative. fileciteturn0file0L50-L56

## 10.5 Time-window limits

Reject invalid ranges where:

- end <= start
- duration exceeds provider policy
- timestamps cannot be parsed
- timezone semantics are ambiguous

The existing adapter already rejects invalid and over-large windows. fileciteturn0file0L181-L193

Do not weaken these constraints.

---

# 11. Cursor handling

Pagination must be agent-friendly and secure.

Requirements:

- Return `next_cursor` only when additional results exist.
- Accept only cursors generated by the platform.
- Preserve HMAC validation.
- Bind cursor to tenant.
- Bind cursor to investigation.
- Reject malformed/tampered cursors.
- Do not expose raw Elasticsearch sort values as a client-controlled pagination mechanism.
- Do not allow changing filters while reusing a cursor unless cursor semantics explicitly support it.

Important improvement:

The existing cursor is signed against tenant and investigation/application identity, but the implementation should be reviewed to ensure a cursor cannot be replayed with a materially different query filter.

If necessary, extend the signed cursor payload to include a canonical hash of the effective query/filter parameters.

This prevents:

```text
request A:
  trace_id = X
  cursor = C

request B:
  trace_id = Y
  cursor = C
```

from producing semantically inconsistent pagination.

Do this without breaking existing cursor compatibility unless the repository has a migration strategy.

---

# 12. Evidence provenance

An investigation agent must be able to distinguish:

- observed evidence
- derived information
- inferred relationships

Do not flatten these into one opaque text response.

For each tool result, retain or expose provenance sufficient to answer:

- Which provider produced the evidence?
- Which evidence ID produced this result?
- When was it observed?
- When was it retrieved?
- Which investigation retrieved it?
- Which query operation produced it?

The existing evidence mapping already constructs `EvidenceProvenance` and `EvidenceFreshness`. fileciteturn0file0L286-L299

Preserve this model.

---

# 13. Agent-oriented response design

MCP responses should be concise but sufficiently structured for an LLM.

Do not make the agent parse giant JSON blobs containing raw infrastructure responses.

Prefer:

```json
{
  "results": [
    {
      "evidence_id": "...",
      "timestamp": "...",
      "service": "...",
      "severity": "...",
      "summary": "...",
      "source": "...",
      "provenance": {}
    }
  ],
  "pagination": {
    "has_more": true,
    "next_cursor": "..."
  }
}
```

For trace telemetry:

```json
{
  "trace_id": "...",
  "code_locations": [
    {
      "function": "...",
      "file_path": "...",
      "line": 123,
      "evidence_ids": ["..."]
    }
  ],
  "database": {
    "accessed_tables": [],
    "statements": []
  },
  "evidence_count": 0
}
```

Avoid narrative prose generated by the server. The investigation agent should perform reasoning over structured evidence.

---

# 14. Error contract

Create stable MCP-facing error categories.

At minimum distinguish:

- invalid request
- unauthorized
- forbidden
- evidence not found
- provider unavailable
- provider timeout
- security policy violation
- invalid pagination cursor
- unsupported operation
- internal error

Do not leak:

- Elasticsearch credentials
- connection strings
- internal stack traces
- secrets
- raw infrastructure exception details
- arbitrary query bodies

Internal logs may retain diagnostic information according to existing logging/security standards.

The existing adapter maps Elastic failures to `ExecutionError` and missing records to `EvidenceNotFoundException`. fileciteturn0file0L122-L130

Build a deterministic MCP error mapping over these domain exceptions.

---

# 15. Observability

Every MCP invocation must be observable.

Create an OpenTelemetry span around each MCP tool invocation.

Include low-cardinality attributes such as:

- tool name
- tenant ID or safe tenant identifier
- investigation ID
- environment
- provider
- result count
- has_more
- error category
- correlation ID

Do NOT put into telemetry:

- raw SQL
- arbitrary log messages
- secrets
- complete evidence payloads
- authorization tokens
- high-cardinality uncontrolled user content

Propagate an existing correlation ID when available.

The Elastic adapter already instruments searches with OpenTelemetry and accepts an optional correlation ID. fileciteturn0file0L141-L145

The MCP layer should connect to that tracing context rather than creating disconnected traces.

---

# 16. Logging

Use structured logging.

Every failed investigation operation should make it possible to determine:

- tool
- investigation
- tenant
- correlation ID
- failure category
- provider
- duration

Do not log full evidence payloads by default.

Be especially careful with:

- raw SQL
- authentication headers
- user identifiers
- session identifiers
- log messages
- exception strings from external providers

The current Elastic implementation already avoids exposing full query bodies in its failure log. Preserve that principle. fileciteturn0file0L232-L243

---

# 17. Classification and data minimization

Evidence currently maps to:

`ClassificationLevel.INTERNAL`

Do not assume every MCP caller is allowed to see every evidence field.

Introduce or reuse a response sanitization/policy layer.

At minimum:

- return only fields necessary for investigation;
- preserve evidence identity and provenance;
- redact secrets if they appear in log attributes;
- prevent accidental credential/token exposure;
- avoid returning unrestricted attributes when the agent only needs selected investigation fields.

If the platform already contains classification/authorization policy, integrate with it rather than creating a competing policy engine.

---

# 18. Important existing implementation risks to address

Review and fix the following issues as part of the MCP implementation.

## 18.1 Hard-coded cursor secret

The current implementation defines:

`CURSOR_HMAC_SECRET = b"iap-elastic-cursor-binding-key"`

This must not remain a hard-coded production secret.

Move cursor signing material to secure configuration/secrets management.

Requirements:

- configurable
- never committed as a production secret
- fail closed if required secure configuration is absent
- support deterministic key loading at startup
- document key rotation implications

Do not silently generate a random key on every process start if that would invalidate active cursors unexpectedly.

---

## 18.2 Cursor/query binding

Review the cursor payload.

The current cursor binds:

- sort values
- tenant ID
- app/investigation ID

It does not visibly bind the rest of the query filters. fileciteturn0file0L73-L99

Add canonical query binding if compatible with the repository architecture.

A cursor should be valid only for the query context that generated it.

---

## 18.3 `get_runtime_evidence` investigation semantics

The direct fetch currently receives `tenant_id` and `evidence_id`, while its generated fetch context is deterministic and the mapping receives that context as an investigation ID. fileciteturn0file0L106-L131

Review this carefully.

The MCP API should require an explicit investigation context where the platform's evidence model requires investigation ownership.

Do not allow a direct evidence lookup to become an unintended cross-investigation data-access primitive.

---

## 18.4 Evidence ID versus Elasticsearch document ID

The evidence model creates a UUID from:

`elastic:{index}:{document_id}`

while the direct Elastic lookup uses the Elasticsearch document ID.

Make the MCP contract explicit about whether `evidence_id` means:

- platform Evidence UUID, or
- provider document ID.

Do not leave this ambiguous.

If necessary, add a provider-reference field or a deterministic translation layer.

The agent should never have to guess which identifier a tool expects.

---

## 18.5 Raw attributes

The existing evidence mapping copies the complete Elasticsearch `_source` into:

`attributes={**src, "tenant_id": tenant_id}`

This is potentially much broader than an agent-facing investigation response should expose. fileciteturn0file0L301-L318

Keep rich attributes internally if required, but introduce a controlled MCP projection.

Never assume all log fields are safe for model exposure.

---

## 18.6 Raw SQL exposure

The bridge currently returns `raw_sql_statements`.

Treat this as sensitive evidence.

Introduce a policy-controlled projection so the MCP tool can return:

- normalized SQL,
- redacted SQL,
- table lineage,
- or raw SQL only when authorized.

Never execute returned SQL.

---

## 18.7 Trace result completeness

The bridge uses a fixed request limit of 100. fileciteturn0file1L39-L51

Review whether 100 records is sufficient for a trace investigation.

Do not simply increase the limit without provider-side cost analysis.

If the trace may contain more evidence, expose pagination or a bounded continuation strategy.

Do not silently claim that extracted telemetry is complete when the query was truncated.

The result should communicate whether the source data was fully consumed.

---

## 18.8 Duplicate code locations

The current bridge appends code locations without deduplication. fileciteturn0file1L65-L80

Normalize/deduplicate code locations in the application-facing result.

Preserve evidence references so deduplication does not destroy traceability.

---

## 18.9 SQL parsing limitations

The bridge currently calls `sqlglot.parse_one(...)`.

Review multi-statement and dialect-specific SQL behavior.

If the application can emit multiple dialects, introduce an appropriate dialect configuration or safe fallback strategy.

Never let SQL parsing failure prevent the rest of trace telemetry from being returned.

---

## 18.10 Timestamp handling

The Elastic mapping falls back to `datetime.now(UTC)` when a timestamp is absent or invalid. fileciteturn0file0L271-L284

Review whether this is appropriate for evidence semantics.

A missing observed timestamp should not be represented as if the event occurred at retrieval time without explicit indication.

Prefer a representation that distinguishes:

- observed timestamp
- missing/invalid timestamp
- retrieval timestamp

Do not fabricate event chronology.

---

# 19. Investigation workflow support

The tools should support an investigation agent performing workflows such as:

## Workflow A — Find an application failure

```text
search_runtime_evidence
    |
    +--> inspect evidence
    |
    +--> identify trace_id
    |
    +--> get_trace_telemetry
    |
    +--> identify code location
    |
    +--> identify accessed database tables
```

## Workflow B — Trace-centric investigation

```text
trace_id
  |
  v
get_trace_telemetry
  |
  +--> code locations
  |
  +--> SQL statements
  |
  +--> accessed tables
  |
  +--> evidence references
```

## Workflow C — Time-window investigation

```text
time_range
  |
  v
search_runtime_evidence
  |
  +--> page using signed cursor
  |
  +--> inspect relevant evidence
  |
  +--> correlate services/errors
```

The MCP tools must make these workflows possible without exposing infrastructure internals to the agent.

---

# 20. Tool descriptions

MCP tool descriptions are part of the agent interface.

Write precise descriptions.

Each description should explain:

- what the tool does;
- when the agent should use it;
- required inputs;
- optional inputs;
- result semantics;
- pagination behavior;
- security/authorization expectations;
- whether returned data is observed or derived;
- important limitations.

Do not put security policy solely in tool descriptions. Enforcement must exist in code.

---

# 21. Input schemas

Use strongly typed request models.

Do not accept:

```python
dict[str, Any]
```

as the primary public MCP tool contract when a typed schema is practical.

Define explicit models for:

- time range
- evidence search
- evidence retrieval
- trace telemetry
- pagination
- tool context

Validate:

- UUIDs
- timestamps
- enums
- strings
- maximum lengths
- list sizes
- pagination tokens
- identifier keys
- identifier values

Reject invalid data before expensive provider calls.

---

# 22. Output schemas

Define explicit response models for:

- evidence item
- evidence provenance
- evidence freshness
- pagination
- code location
- database lineage
- trace telemetry
- MCP errors

Avoid exposing arbitrary domain internals when a stable API representation is more appropriate.

The MCP contract is an external agent-facing API and must remain stable even if internal domain models evolve.

---

# 23. Dependency injection

The MCP server must receive its dependencies through the application's existing dependency-injection/configuration mechanism.

At minimum:

```text
MCP server
  -> investigation service
      -> evidence gateway
          -> AsyncElasticAdapter
      -> incident/telemetry bridge
```

Do not instantiate `AsyncElasticsearch` inside individual MCP tool handlers.

Do not create a new Elastic client for every MCP request.

Ensure graceful lifecycle management.

---

# 24. Configuration

Add configuration for:

- MCP server enablement
- MCP transport
- bind host
- bind port if applicable
- authentication configuration
- Elastic provider configuration
- cursor signing secret/key
- request timeout
- maximum result limits
- raw SQL exposure policy
- evidence field projection policy

Use the project's existing settings/configuration conventions.

Never hard-code production secrets.

Document required environment/configuration variables.

---

# 25. Transport

Inspect the repository and existing deployment conventions before selecting the MCP transport.

If the platform is intended for local/stdio agent integration, support the appropriate stdio MCP transport.

If it is intended as a remotely hosted service, use the repository's supported HTTP MCP transport.

Do not add unnecessary transports.

If multiple transports are required, share the same tool/application layer so behavior remains identical.

---

# 26. Testing requirements

Create comprehensive tests.

## Unit tests

Test:

- MCP input validation
- MCP output serialization
- tenant enforcement
- investigation enforcement
- cursor validation
- cursor query binding
- limit enforcement
- time-window enforcement
- evidence projection
- raw attribute redaction
- raw SQL policy
- error mapping
- trace telemetry mapping
- code-location deduplication
- SQL parsing failure behavior
- missing timestamps
- provider exceptions

## Integration tests

Test:

```text
MCP tool
  -> application service
  -> evidence port
  -> mocked AsyncElasticAdapter
```

and:

```text
MCP tool
  -> ElasticIncidentBridge
  -> mocked AsyncElasticAdapter
```

Verify that MCP handlers never bypass the abstraction layer.

## Security tests

Explicitly test:

1. Cross-tenant evidence access.
2. Cross-investigation evidence access.
3. Tampered cursor.
4. Cursor from another tenant.
5. Cursor from another investigation.
6. Cursor reused with a different query.
7. Excessive limit.
8. Excessive time range.
9. Excessive keywords.
10. Excessive services.
11. Unauthorized identifier field.
12. Malformed UUID.
13. Malformed timestamp.
14. Elasticsearch error leakage.
15. Secret/token exposure in returned attributes.
16. SQL injection-like strings supplied as search terms.
17. Raw Elasticsearch DSL supplied as an input.
18. Arbitrary index name supplied as an input.
19. Script/aggregation injection attempts.

The expected behavior is rejection or safe handling, never execution outside the constrained contract.

---

# 27. Contract tests

Add MCP contract tests that validate the tool surface.

For every tool verify:

- exact tool name
- required fields
- optional fields
- schema constraints
- structured result format
- error behavior
- pagination behavior

Treat these as compatibility tests for the investigation agent.

---

# 28. Performance and cost controls

Agentic investigation can generate many iterative calls.

Implement safeguards against expensive investigation loops.

At minimum:

- bounded result sizes
- bounded time windows
- provider request timeout
- no arbitrary aggregation
- no deep pagination
- bounded keyword/service lists
- bounded raw response size
- bounded SQL/result payload size

Do not optimize by weakening correctness or isolation.

If the platform has request budgets or investigation budgets, integrate MCP calls with those budgets.

---

# 29. Determinism

Agent tools must produce predictable responses.

Avoid:

- random identifiers
- random ordering
- unstable serialization
- provider-dependent response shapes
- hidden query mutations

The Elastic search already sorts by timestamp descending and `_id` ascending. Preserve deterministic ordering. fileciteturn0file0L206-L218

If additional sorting is introduced, document and test it.

---

# 30. Documentation

Add documentation covering:

1. MCP server purpose.
2. Available tools.
3. Tool schemas.
4. Authentication/authorization.
5. Tenant isolation.
6. Investigation isolation.
7. Evidence classification.
8. Pagination.
9. Configuration.
10. Local development.
11. Running tests.
12. Example investigation workflow.
13. Security limitations.
14. Operational/deployment requirements.

Include examples that demonstrate an investigation workflow without exposing real credentials or production evidence.

---

# 31. Repository compatibility

Before implementation:

1. Inspect `pyproject.toml` / dependency configuration.
2. Inspect existing MCP dependencies, if any.
3. Inspect existing application services.
4. Inspect domain ports.
5. Inspect authentication/authorization infrastructure.
6. Inspect configuration conventions.
7. Inspect test conventions.
8. Inspect CLI/server entry points.
9. Inspect deployment configuration.
10. Search for existing evidence gateway implementations.
11. Search for existing investigation context models.
12. Search for existing MCP/tool abstractions.

Do not create duplicate abstractions when an existing one can be extended.

---

# 32. Required implementation sequence

Implement in this order:

### Phase 1 — Architecture discovery

Map the existing repository:

```text
domain
application
ports
infrastructure
configuration
authentication
observability
tests
deployment
```

Identify the correct extension points.

### Phase 2 — Application-level investigation API

Create/reuse application services for:

- runtime evidence search
- evidence retrieval
- trace telemetry

Keep these independent of MCP.

### Phase 3 — Harden existing Elastic boundaries

Address:

- cursor secret configuration
- cursor/query binding
- evidence ID semantics
- timestamp semantics
- bounded trace retrieval
- code-location deduplication
- SQL parsing behavior
- sensitive attribute projection

### Phase 4 — MCP schemas

Create explicit request/response schemas.

### Phase 5 — MCP tool handlers

Expose the investigation tools through MCP.

### Phase 6 — Security/error/observability integration

Add:

- authorization
- context propagation
- error mapping
- tracing
- structured logging
- data minimization

### Phase 7 — Tests

Implement unit, integration, security, and contract tests.

### Phase 8 — Documentation and operational wiring

Add configuration, entry points, local run instructions, and deployment integration.

---

# 33. Definition of done

The implementation is complete only when all of the following are true:

- [ ] MCP service starts successfully using repository-standard configuration.
- [ ] MCP tools are discoverable.
- [ ] Tool schemas are strongly typed.
- [ ] Runtime evidence search is exposed.
- [ ] Single evidence retrieval is exposed.
- [ ] Trace telemetry is exposed.
- [ ] MCP never performs direct Elasticsearch queries.
- [ ] Existing `AsyncElasticAdapter` remains the provider boundary.
- [ ] Existing `ElasticIncidentBridge` is reused or appropriately refactored.
- [ ] Tenant isolation is enforced.
- [ ] Investigation isolation is enforced.
- [ ] Cursor integrity is enforced.
- [ ] Cursor/query binding is enforced.
- [ ] Provider-side limits remain authoritative.
- [ ] Arbitrary Elasticsearch DSL is impossible through MCP.
- [ ] Arbitrary index selection is impossible through MCP.
- [ ] Arbitrary scripts/aggregations are impossible through MCP.
- [ ] Sensitive evidence fields are projected/redacted appropriately.
- [ ] Raw SQL is treated as untrusted evidence.
- [ ] SQL parsing failures do not abort trace investigation.
- [ ] Trace truncation is explicitly represented.
- [ ] Missing timestamps are not silently converted into misleading observation times.
- [ ] Evidence provenance is retained.
- [ ] Evidence freshness is retained.
- [ ] MCP errors are structured and do not leak infrastructure details.
- [ ] OpenTelemetry traces connect MCP calls to provider calls.
- [ ] Structured logging is implemented.
- [ ] Unit tests pass.
- [ ] Integration tests pass.
- [ ] Security tests pass.
- [ ] MCP contract tests pass.
- [ ] Documentation is complete.
- [ ] Existing tests remain green.
- [ ] Type checking passes.
- [ ] Linting/formatting passes.
- [ ] No production secrets are committed.
- [ ] No unrelated architectural rewrites are introduced.

---

# 34. Expected coding-agent behavior

Do not merely describe the changes. Implement them.

Before editing:

- inspect the repository;
- identify existing abstractions;
- understand dependency direction;
- understand configuration and test conventions.

While editing:

- make small coherent changes;
- preserve backward compatibility where practical;
- avoid unrelated refactors;
- add tests with each security-sensitive change;
- keep public MCP schemas stable;
- preserve existing domain semantics unless a defect described above requires correction.

After editing:

1. Run formatting.
2. Run linting.
3. Run type checking.
4. Run unit tests.
5. Run integration tests where available.
6. Run MCP contract tests.
7. Run security-focused tests.
8. Review the final diff for accidental API/security regressions.

If a required repository abstraction does not exist, create the smallest appropriate abstraction and explain why in the implementation notes.

If an assumption cannot be verified from the repository, do not invent it. Inspect further or clearly document the assumption.

---

# 35. Final implementation report

At completion, provide a concise report containing:

## Changed

List every changed/added file and its purpose.

## MCP tools

List each exposed tool and its input/output contract.

## Security

Describe:

- tenant isolation
- investigation isolation
- cursor protection
- input bounds
- sensitive-field handling
- SQL handling

## Tests

Report:

- unit tests
- integration tests
- security tests
- contract tests
- type checking
- linting

## Remaining limitations

Explicitly list anything that could not be implemented because the repository lacks a required dependency, abstraction, configuration, or external integration.

Do not claim a capability is implemented unless the code and tests demonstrate it.

---

# 36. Core design principle

The final architecture must make the investigation agent powerful enough to investigate real application failures while ensuring that the agent cannot turn MCP access into unrestricted infrastructure access.

The agent should receive **bounded, typed, provenance-aware investigation capabilities**, not a generic Elasticsearch interface.

The existing evidence infrastructure is the enforcement boundary. MCP is the controlled agent-facing interface over that infrastructure.
