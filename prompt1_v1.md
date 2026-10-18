# Implementation Prompt: Tenant-Safe, Revision-Aware Layer 3 Topology and Domain Attribution

You are implementing Layer 3 of the existing `investigation_agent_platform` repository. Integrate with the current hexagonal architecture; do not build a standalone Neo4j service and do not replace PostgreSQL, Temporal, the evidence gateway, or existing authorization boundaries.

## Objective

Implement a tenant-scoped, revision-aware organizational and static-code topology bounded context with an in-memory adapter first and a production Neo4j adapter second. The capability must ingest validated output from the existing Tree-sitter/code pipeline and resolve a source location to a provenanced domain attribution result.

The implementation must support this canonical ownership hierarchy:

```text
Domain
  <-[:BELONGS_TO]- GitOrganization
  <-[:BELONGS_TO]- Repository
  <-[:BELONGS_TO]- Package
  <-[:IN_PACKAGE]- SourceFile
  <-[:DECLARED_IN]- ASTNode
```

Ownership edges point from the owned child to its owning parent. AST call edges point from caller to callee.

## Mandatory Existing Contracts

Use the platform's existing identifiers and security semantics:

- `tenant_id`: authoritative security partition derived from verified authentication or trusted workflow context.
- `application_id`: tenant-owned application/profile identity.
- `investigation_id: UUID`: case/investigation identity.
- `repository_id`: new canonical repository identity linked from `CodeProfile`.
- `revision`: immutable Git commit SHA used for topology lookup and provenance.

Do not introduce `case_id` as a tenant key. Do not use `workspace_id` unless you add it explicitly as a child scope under `tenant_id` across all contracts and storage.

Preserve and reuse these existing boundaries:

- `ports/evidence/code.py`
- `ports/evidence/gateway.py`
- `ports/persistence/repositories.py`
- `ports/security/redactor.py`
- `application/evidence/gateway.py`
- `domain/provenance/models.py`
- `domain/evidence/models.py`
- `domain/code/models.py`
- `infrastructure/evidence/code/parser.py`
- `infrastructure/evidence/code/pipeline.py`
- `infrastructure/evidence/code/intelligence.py`
- `bootstrap/__init__.py`

## Hard Prerequisite

Before integrating topology into workflow activities, correct the current workflow identity mismatch:

- The API creates investigation A and starts `wf-investigation-{A}`.
- The workflow currently runs `create_investigation_activity`, creating investigation B.
- Later activities use B while the API and workflow ID refer to A.

Change the workflow input to include the existing `investigation_id`. Load that investigation in the workflow/activity path; do not create a second aggregate. The following identities must remain equal throughout a run:

```text
API investigation_id
= workflow input investigation_id
= checkpoint investigation_id
= evidence investigation_id
= topology attribution investigation_id
```

Add regression tests proving this invariant.

## Scope

Implement these vertical slices:

1. Canonical topology domain models and ports.
2. In-memory topology adapter and application services.
3. Neo4j topology adapter with schema constraints and bounded ingestion.
4. Evidence-gateway integration that converts attribution to provenanced evidence.
5. Production configuration, bootstrap, readiness, migrations, and tests.

Do not implement Mem0, Graphiti, or Cognee in this change. Cognee may be evaluated later as an alternate code-graph adapter. There must be one static topology authority at a time.

## Required Domain Models

Create a new bounded context under `src/investigation_agent_platform/domain/topology/`. Use Pydantic v2 models with `ConfigDict(frozen=True, extra="forbid")`. Separate write/input models from read/result models.

### Enums

Define:

- `RepositoryType`: `SERVICE`, `SHARED_LIBRARY`, `APPLICATION`, `MONOREPO`, `INFRASTRUCTURE`, `TOOLING`, `UNKNOWN`.
- `TopologyNodeType`: `MODULE`, `CLASS`, `INTERFACE`, `METHOD`, `FUNCTION`, `CONSTRUCTOR`, `FIELD`, `ENUM`, `ROUTE`, `MESSAGE_HANDLER`.
- `TopologySnapshotStatus`: `PENDING`, `INGESTING`, `READY`, `FAILED`, `SUPERSEDED`.
- `CallResolutionStatus`: `RESOLVED`, `AMBIGUOUS`, `UNRESOLVED`, `EXTERNAL`.
- `AttributionClassification`: `LIBRARY_DEFECT`, `CALLER_MISUSE`, `NON_LIBRARY_DEFECT`, `INCONCLUSIVE`.
- `AttributionFallbackLevel`: `AST_NODE`, `SOURCE_FILE`, `REPOSITORY`, `UNATTRIBUTED`.

### Organizational models

Define complete models for:

- `DomainIdentity`: `tenant_id`, `domain_id`, `name`, `owner_team_id`, optional contact metadata, ownership source, effective timestamps.
- `GitOrganizationIdentity`: `tenant_id`, `git_org_id`, `name`, provider, `domain_id`.
- `RepositoryIdentity`: `tenant_id`, `repository_id`, locator/name, `git_org_id`, `repository_type`, default branch, active status.
- `PackageIdentity`: tenant, repository, revision, normalized package path, language/package type.
- `SourceFileIdentity`: tenant, repository, revision, normalized path, language, content hash.

Do not use `owner_email` as the ownership identity. Use `owner_team_id`; email may be optional contact metadata.

### Canonical AST model

Do not create an unrelated fourth symbol model. Reconcile the existing domain, port, and parser symbol shapes by defining a canonical topology `ASTNodeIdentity` and explicit adapters from existing parser/provider DTOs.

`ASTNodeIdentity` must include:

- tenant, repository, and revision;
- deterministic `node_id` generated with UUIDv5 or SHA-256 from tenant, repository, revision, normalized file path, qualified name, node type, and full source range;
- name and qualified name;
- node type;
- normalized file path;
- start/end line and column;
- signature hash, not unsanitized source content;
- optional parent node ID;
- parser version.

### Edge models

Define validated input models for:

- `CallEdgeInput`: caller ID, callee ID or unresolved target, call-site file/line, resolution status, confidence.
- `ReferenceEdgeInput`: source/target, reference type, file/line, confidence.
- optional sanitized database-access edges.

All endpoints must be in the same tenant and topology snapshot. Cross-repository calls are allowed only when both snapshots are explicitly identified.

### Versioned ingestion input

Define `ASTTopologyPayload` with:

- `schema_version`;
- `parser_version`;
- `tenant_id`;
- `application_id`;
- `repository_id`;
- immutable `revision`;
- `snapshot_id`;
- source files;
- AST nodes;
- calls and references;
- bounded parse diagnostics;
- canonical payload hash.

Reject unknown fields. Do not accept an unvalidated `dict` at the service boundary.

### Attribution output

Define `DomainAttributionResult` with:

- tenant/application/investigation/repository/revision identity;
- graph snapshot ID;
- matched AST node and exact source range;
- repository type and full ownership path;
- failure-frame and caller-frame domain IDs;
- candidate culprit and victim domains;
- attribution classification and confidence;
- alternatives and limitations;
- supporting and contradicting evidence IDs;
- attribution rule version;
- fallback level.

## Required Ports

Create application-facing protocols under `src/investigation_agent_platform/ports/topology/`:

### `TopologyIngestionPort`

```python
async def ingest(self, payload: ASTTopologyPayload) -> TopologyIngestionResult: ...
```

### `CodeTopologyRepository`

Provide operations to:

- resolve a `READY` snapshot by tenant/repository/revision;
- read snapshot status;
- supersede prior snapshots;
- query symbols and call edges within bounded limits.

### `DomainAttributionPort`

```python
async def resolve_source_location(
    self,
    tenant_id: str,
    application_id: str,
    investigation_id: UUID,
    repository_id: str,
    revision: str,
    file_path: str,
    line_number: int,
) -> StaticOwnershipResult: ...
```

### `RepositoryRegistryPort`

Resolve a tenant/application profile to the canonical repository identity and verify repository ownership. Extend `CodeProfile` with `repository_id`; retain its existing repository locator and access/build configuration.

## In-Memory Adapter

Implement the in-memory adapter before Neo4j. It must use the same ports and tenant/revision-qualified keys. Use it to prove:

- data contracts;
- tenant isolation;
- idempotent ingestion;
- most-specific source-range lookup;
- revision separation;
- dual-frame attribution behavior.

The in-memory adapter is allowed only in explicit development/test profiles.

## Neo4j Adapter

Create the implementation under `src/investigation_agent_platform/infrastructure/topology/neo4j/` using the official async Neo4j driver.

### Configuration

Add typed settings to `ApplicationConfig`:

- URI as a secret-bearing configuration value;
- username and password as `SecretStr`;
- database name;
- TLS requirement;
- connection and query timeouts;
- pool size;
- batch size;
- maximum lookup nodes/edges;
- required/optional deployment flag.

Never log credentials or connection URIs. Production startup must fail if topology is required and configuration, connectivity, or graph constraints are unavailable.

### Neo4j uniqueness constraints

Install constraints in an explicit graph-schema migration/admin command, not per request:

- `Domain(tenant_id, domain_id)`
- `GitOrganization(tenant_id, git_org_id)`
- `Repository(tenant_id, repository_id)`
- `TopologySnapshot(tenant_id, repository_id, revision)`
- `Package(tenant_id, repository_id, revision, path)`
- `SourceFile(tenant_id, repository_id, revision, path)`
- `ASTNode(tenant_id, repository_id, revision, node_id)`

Every `MATCH`, `MERGE`, and relationship endpoint includes tenant and revision predicates from the first clause.

### Ingestion semantics

Implement `Neo4jTopologyIngestor` behind `TopologyIngestionPort`:

1. Authorize `INGEST_CODE_TOPOLOGY` using existing security ports.
2. Verify repository ownership through `RepositoryRegistryPort`.
3. Validate schema/parser version and payload checksum.
4. Create a `PENDING` snapshot audit record in PostgreSQL.
5. Move it to `INGESTING`.
6. Upsert topology in bounded `UNWIND` batches using transaction functions.
7. Verify all relationship endpoints.
8. Mark the snapshot `READY` only after all batches succeed.
9. On failure, mark it `FAILED` with a bounded sanitized summary; a failed/partial snapshot must never be queryable.
10. Make retries idempotent by `(tenant_id, repository_id, revision, payload_hash)`.

`MERGE` alone is not sufficient. Support partial-failure recovery, retry classification, and stale/superseded snapshot handling.

### Source-location lookup

Implement parameterized Cypher that starts with tenant/repository/revision/file predicates and finds nodes where:

```text
start_line <= requested_line <= end_line
```

Select the most specific enclosing symbol using:

```text
source range size ascending
nesting depth descending
stable node_id ascending
```

If equally specific candidates remain, return `TOPOLOGY_AMBIGUOUS_MATCH`; do not choose arbitrarily. If no AST node matches, fall back explicitly to `SourceFile`, then `Repository`, recording the fallback level.

Bound every query by timeout, maximum path depth, maximum returned rows, and output size.

## Failure Attribution Service

Create `FailureAttributionService` in the application layer. It receives:

- authenticated tenant/application/investigation scope;
- failure frame and caller frame, each with repository, revision, file, and line;
- supporting runtime evidence IDs;
- optional contract/payload evidence IDs.

It must:

1. Resolve both frames through `DomainAttributionPort`.
2. If the failure repository is not a shared library, propose its owner as the culprit with `NON_LIBRARY_DEFECT` only when runtime evidence supports it.
3. If it is a shared library, distinguish internal invariant/uncaught defects from caller contract misuse using runtime evidence.
4. Return `INCONCLUSIVE` when evidence is missing or contradictory.
5. Record alternatives and limitations.
6. Convert the result to standard `Evidence` with full `EvidenceProvenance`.
7. Persist through `AsyncEvidenceGateway` and the existing evidence repository.
8. Never publish/finalize a conclusion directly; `ConclusionGate` remains authoritative.

## Evidence Provenance

Topology-derived evidence must record:

- tenant, investigation, application, repository, and revision;
- graph snapshot ID;
- AST parser and topology adapter versions;
- query fingerprint;
- matched node and line range;
- traversed ownership path;
- attribution policy version;
- caller and failure-frame evidence IDs;
- fallback level;
- retrieval timestamp and freshness.

Do not store raw source, prompts, payloads, credentials, or Cypher parameters in logs or audit rows.

## API and Workflow Integration

Do not add a public endpoint until the application port and evidence integration are complete. If an operator-only ingestion endpoint is required, it must:

- require verified service/admin identity;
- derive tenant from authentication;
- authorize `INGEST_CODE_TOPOLOGY`;
- accept only the versioned payload schema;
- enforce body and batch limits;
- be idempotent;
- return stable public error codes and correlation ID.

Topology queries from workflows must run through an activity with bounded timeouts and typed retry classification. Missing topology yields `TOPOLOGY_NOT_CONFIGURED` or `ATTRIBUTION_INCONCLUSIVE`, never fabricated ownership.

## PostgreSQL Migration

Use the project's Alembic setup. Add a tenant-scoped topology snapshot/audit table containing:

- UUID primary key;
- tenant/application/repository/revision;
- schema/parser version;
- payload hash;
- status;
- node/edge counts;
- bounded error summary;
- timestamps;
- unique `(tenant_id, repository_id, revision, payload_hash)` constraint;
- tenant/repository/revision indexes;
- PostgreSQL RLS.

Do not auto-create or alter the production schema at runtime.

## Production Composition

Wire topology dependencies in the shared production composition root used by API and worker. Add:

- Neo4j driver lifecycle management;
- startup connectivity and constraint validation;
- readiness probe against the actual initialized driver;
- graceful driver shutdown;
- topology provider registration in the existing selector;
- explicit in-memory adapter only for dev/test.

An implemented but unwired adapter is incomplete.

## Error Taxonomy

Define typed errors with stable public codes:

- `TOPOLOGY_NOT_CONFIGURED` — non-retryable.
- `TOPOLOGY_SNAPSHOT_NOT_READY` — retryable only while `PENDING/INGESTING`.
- `TOPOLOGY_NODE_NOT_FOUND` — non-retryable.
- `TOPOLOGY_AMBIGUOUS_MATCH` — non-retryable.
- `TOPOLOGY_ACCESS_DENIED` — non-retryable.
- `TOPOLOGY_SCHEMA_MISMATCH` — non-retryable.
- `TOPOLOGY_PROVIDER_UNAVAILABLE` — retryable with bounded attempts.
- `ATTRIBUTION_INCONCLUSIVE` — non-retryable investigation result, not infrastructure failure.

Unknown exceptions default to non-retryable and are translated at boundaries without leaking internal messages.

## Testing Requirements

Add:

- domain model tests;
- port contract tests for in-memory and Neo4j adapters;
- cross-tenant node/edge/lookup denial tests;
- revision isolation tests;
- duplicate ingestion and concurrent retry tests;
- partial batch failure/recovery tests;
- overlapping and nested source-range resolution tests;
- unresolved/ambiguous call tests;
- path traversal and symlink escape tests;
- topology authorization/capability tests;
- Neo4j timeout/auth/constraint failure classification tests;
- provenance completeness tests;
- dual-frame policy tests for library defect, caller misuse, contradictory evidence, and inconclusive cases;
- workflow identity-invariant test;
- end-to-end test from AST payload to persisted attribution evidence;
- migration upgrade/downgrade tests.

Use a real Neo4j test container for integration tests. Do not mock Cypher behavior in the integration suite.

## Quality Gates

Run and pass:

```bash
uv run ruff check src tests migrations
uv run ruff format --check src tests migrations
uv run mypy src/investigation_agent_platform
uv run pytest -q --cov=src/investigation_agent_platform --cov-fail-under=70
uv run bandit -r src -x tests -q -ll
uv run pip-audit
uv build
```

## Explicit Non-Goals

- Do not add Mem0, Graphiti, or Cognee in this implementation.
- Do not make Neo4j authoritative for investigations, evidence, hypotheses, timelines, or checkpoints.
- Do not expose raw Cypher to callers or models.
- Do not accept caller-controlled tenant or graph namespace IDs as authority.
- Do not duplicate active hypotheses or workflow state in the topology graph.
- Do not claim attribution certainty without supporting runtime evidence.

## Completion Criteria

The change is complete only when:

1. One investigation ID is preserved end-to-end through API, workflow, evidence, checkpoint, and attribution paths.
2. Tenant/repository/revision isolation is demonstrated against both adapters.
3. Repeated ingestion is idempotent and partial snapshots are never queryable.
4. Source-location lookup is deterministic and revision-correct.
5. Attribution is persisted as provenanced evidence through the evidence gateway.
6. `ConclusionGate` remains the only finalization authority.
7. Production bootstrap, health checks, shutdown, migrations, and tests include Neo4j.
8. All quality gates pass.
