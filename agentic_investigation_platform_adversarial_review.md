# Adversarial Code Review & GPT-SOL Remediation Specification

## Target package

**Package:** `investigation_agent_platform`  
**Source artifact reviewed:** `src_20260925_101545.txt`  
**Review mode:** adversarial, production-readiness, security, correctness, distributed-systems, agentic-AI, persistence, and operability review.

## How GPT-SOL should use this document

Treat every finding below as an actionable engineering requirement. Do not merely patch the symptom.

For each finding:

1. Inspect the current implementation and all call sites.
2. Preserve the existing ports/domain contracts where reasonable; change contracts when the current abstraction is unsafe or incomplete.
3. Implement the durable production path rather than extending test/dev stubs.
4. Add or update unit, integration, contract, security, and failure-injection tests.
5. Preserve tenant isolation across every read, write, cache, workflow, event, object-storage key, repository, and external-provider call.
6. Do not silently fall back from production dependencies to permissive/in-memory/stub behavior.
7. Do not log secrets, credentials, raw evidence, authorization tokens, SQL, prompts, or sensitive payloads.
8. Make failure states explicit and observable.
9. Where distributed consistency is required, use transactional/outbox/idempotent patterns rather than process-local locks.
10. Do not claim a workflow started, evidence was collected, a conclusion was verified, or an action was authorized unless the underlying operation actually succeeded.

---

# Executive assessment

The codebase has a substantial architectural skeleton: domain models, ports, FastAPI routes, Temporal workflow/activity adapters, SQLAlchemy repositories, evidence providers, correlation, sanitization, LLM adapters, and observability contracts.

However, the implementation is not yet a trustworthy production agentic investigation platform. The dominant risk is **semantic incompleteness hidden behind successful-looking APIs**. Several paths return success with empty/stub data, swallow infrastructure failures, use process-local state where durable state is required, or permit security controls to degrade to permissive behavior.

The highest-risk themes are:

- production can still construct an in-memory `AppContext`;
- tenant authentication is header-based and JWT signatures are explicitly not verified;
- the Temporal start endpoint can return `RUNNING` after workflow-start failure;
- pause/resume endpoints do not actually pause/resume a Temporal workflow;
- the LLM reasoning layer has a hard-coded fallback conclusion and can fall back to an allow-all prompt policy;
- evidence retrieval can succeed with zero evidence when the gateway is absent;
- action execution lacks a demonstrated mandatory authorization gate;
- SQLAlchemy/RLS tenant context is incomplete and one repository reconstructs `tenant_id="unknown"`;
- idempotency is process-local and therefore not safe across replicas;
- checkpoint/transition repositories in the API context are no-ops;
- configuration/bootstrap contains broad exception fallbacks that can hide production wiring failures;
- no test tree is present in the supplied source artifact, so critical invariants are not demonstrated.

The remediation priority should be to make the platform **fail closed, durable, tenant-safe, and truth-preserving** before adding more agent capabilities.

---

# Severity model

- **P0 — Critical:** security boundary bypass, false-success behavior, data loss/cross-tenant exposure, or fundamental production correctness failure.
- **P1 — High:** major functional gap or distributed-systems flaw likely to break real investigations or undermine trust.
- **P2 — Medium:** important reliability, maintainability, observability, or completeness gap.
- **P3 — Low:** quality, ergonomics, cleanup, or non-blocking technical debt.

---

# Findings

## F-001 — Production dependency container still defaults to in-memory repositories

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/dependencies.py`
- **Severity:** **P0**
- **Details:** `Container` is explicitly a minimal stub and `AppContext` defaults to in-memory repositories. The source also describes the API as DB-free until the production SQLAlchemy session factory is wired. This means the API composition root can run without durable persistence.
- **Impact:** Restart loses investigations/evidence/hypotheses/timeline state. Multiple replicas have divergent state. A successful API response can be backed only by process memory.
- **Resolution:** Make production startup require the real SQLAlchemy repositories, Temporal client, evidence gateway, publisher, checkpoint repository, transition repository, and other mandatory dependencies. Permit in-memory implementations only under an explicit `development`/`test` profile. Add a startup dependency validation that fails hard in production.
- **Acceptance tests:** production config with missing DB must fail startup; production `AppContext` must contain no in-memory repositories; two API replicas must observe identical persisted state.

## F-002 — Worker production bootstrap still constructs `AppContext()` without real infrastructure

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/bootstrap/worker.py`
- **Severity:** **P0**
- **Details:** `build_app_context()` logs that it is building production context, but constructs `AppContext()` exactly like non-production. The worker therefore can operate with in-memory dependencies despite production environment.
- **Impact:** Temporal activities can read/write state different from the API process and lose data on worker restart.
- **Resolution:** Implement one authoritative dependency composition root shared by API and workers. Production must construct concrete SQLAlchemy repositories, evidence gateway, reasoning coordinator, authorization policy, event publisher, checkpoint store, and telemetry adapter. Dependency construction failure must abort startup.

## F-003 — Tenant authentication is spoofable

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/tenant.py`
- **Severity:** **P0**
- **Details:** `X-Tenant-ID` is accepted as the primary identity source. The JWT decoder explicitly does not verify the signature. If a Bearer token is supplied, the code only compares an unverified tenant claim with the header.
- **Impact:** An attacker can select another tenant simply by changing `X-Tenant-ID`, or craft an unsigned/forged JWT with a desired tenant claim.
- **Resolution:** Replace header identity with verified authentication. Validate JWT signature using configured issuer/JWKS, algorithm allow-list, audience, expiry/not-before, issuer, subject, and tenant claim. Derive tenant and principal exclusively from the verified token. Treat `X-Tenant-ID` as an optional consistency check, never as authority. Add negative tests for forged, expired, wrong-issuer, wrong-audience, `alg=none`, missing-tenant, and mismatched-tenant tokens.

## F-004 — Agent action authorization is defined as a port but not demonstrably enforced

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/application/worker/activities.py`
- **Severity:** **P0**
- **Details:** `ActionAuthorizerPort` exists, but `execute_action_activity` dispatches actions through the evidence gateway without a clearly mandatory authorization/policy gate in the shown execution path.
- **Impact:** A model-generated action can become an executable operation without a centralized policy decision.
- **Resolution:** Introduce an `ActionExecutionService` that requires `ActionAuthorizerPort.authorize_action()` before every action. Bind action type, tenant, investigation, application, target resource, parameters, principal, capabilities, and risk level into the authorization context. Deny by default. Record the authorization decision and policy version. Never let the LLM directly select an arbitrary executable tool.

## F-005 — Prompt-safety can degrade to an allow-all implementation

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/application/worker/activities.py`
- **Severity:** **P0**
- **Details:** `reason_activity` contains a fallback `_AllowAllSafety` implementation when configuration/coordinator construction fails.
- **Impact:** A security control failure is converted into permission to process prompts/evidence rather than fail closed.
- **Resolution:** Delete the permissive fallback. Security-policy initialization failure must raise a non-retryable configuration/security error and prevent reasoning. Provide an explicit test double only in test wiring.

## F-006 — Hard-coded reasoning conclusion can create fabricated investigative results

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/application/investigation/reasoning.py`
- **Severity:** **P0**
- **Details:** When no usable LLM result is returned, the coordinator returns hard-coded observations, an information gap, a hypothesis, `conclusion_readiness=0.85`, and a conclusion asserting that evidence confirms pool exhaustion.
- **Impact:** The platform can manufacture a root-cause-looking conclusion unrelated to actual evidence.
- **Resolution:** Replace the fallback with an explicit `REASONING_UNAVAILABLE`/`INCONCLUSIVE` result. Readiness must be zero unless computed from actual state/evidence. Never invent observations, hypotheses, causal claims, or confirmation. Store provenance for every reasoning output.

## F-007 — LLM failure silently falls back to a fabricated/stub decision

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/application/investigation/reasoning.py`
- **Severity:** **P0**
- **Details:** Invalid LLM schema and gateway exceptions are caught and the code falls back to the hard-coded stub decision.
- **Impact:** Provider outages become false investigative certainty rather than an explicit failure.
- **Resolution:** Distinguish retryable provider failure, invalid model output, policy rejection, and unavailable provider. Retry only transient errors. After bounded retries, persist a non-conclusive state and let the workflow decide whether to replan or terminate safely.

## F-008 — Temporal workflow start reports success after start failure

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/v1/routers/investigations.py`
- **Severity:** **P0**
- **Details:** `start_investigation_workflow()` catches all exceptions from `start_workflow()`, logs a warning, and still returns HTTP 202 with `"status": "RUNNING"` and `"message": "Workflow started successfully"`.
- **Impact:** The control plane lies to callers. The investigation may remain persisted but never execute.
- **Resolution:** On workflow-start failure, return a 5xx/appropriate domain error and keep the investigation in a non-running state. Use Temporal's workflow ID/idempotency semantics. Persist a dispatch state only after confirmed start or use an outbox/command dispatcher.

## F-009 — Pause endpoint does not pause a Temporal workflow

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/v1/routers/events.py`
- **Severity:** **P0**
- **Details:** `/pause` only loads the investigation and returns its current status. It does not signal, update, or pause Temporal execution.
- **Impact:** API contract says pause but no pause occurs.
- **Resolution:** Implement a Temporal signal/update or a durable control command consumed by the workflow. Persist the requested state transition atomically. Make pause idempotent and define behavior for already-completed/cancelled workflows.

## F-010 — Resume endpoint does not resume a Temporal workflow

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/v1/routers/events.py`
- **Severity:** **P0**
- **Details:** `/resume` also only reads the investigation and returns status.
- **Resolution:** Implement a real workflow resume control path, including durable state transition, Temporal signal/update, idempotency, authorization, and race handling.

## F-011 — Checkpoint repository used by API service is a no-op

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/dependencies.py`
- **Severity:** **P1**
- **Details:** `_InMemoryCheckpointRepository.save_checkpoint()` and `get_latest_checkpoint()` do nothing/return `None`.
- **Impact:** Resume/recovery cannot actually restore workflow state in this composition.
- **Resolution:** Provide a durable checkpoint repository backed by PostgreSQL/object storage as appropriate. Include investigation ID, tenant, workflow/run ID, step, schema version, state hash, timestamp, and checksum. Make writes idempotent and monotonic.

## F-012 — Transition repository used by API service is a no-op

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/dependencies.py`
- **Severity:** **P1**
- **Details:** `_InMemoryTransitionRepository.record_transition()` returns `None` without persisting.
- **Impact:** Lifecycle audit trail is incomplete.
- **Resolution:** Persist state transitions transactionally with actor/principal, reason, previous state, new state, correlation ID, workflow ID, timestamp, and expected version.

## F-013 — Process-local idempotency cannot protect a multi-replica API

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/dependencies.py`
- **Severity:** **P1**
- **Details:** `_InMemoryIdempotencyStore` uses an `asyncio.Lock` and process-local dictionary.
- **Impact:** Two replicas can execute the same request. Restart loses keys. A 24-hour TTL has no durable enforcement.
- **Resolution:** Move idempotency to PostgreSQL/Redis with a unique `(tenant_id, idempotency_key)` constraint and stored response hash/payload/status. Reject reuse with a different request body. Define expiry semantics and concurrent request behavior.

## F-014 — Idempotency key is not bound to request payload

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/v1/routers/investigations.py`
- **Severity:** **P1**
- **Details:** The key is tenant + supplied idempotency key; the request body is not fingerprinted.
- **Impact:** A client can reuse a key for a different investigation request and receive the first response.
- **Resolution:** Persist a canonical request hash with the key. Same key + different hash => 409 conflict. Same key + same hash => replay stored response.

## F-015 — Untrusted principal identity is accepted from `X-Principal-ID`

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/v1/routers/investigations.py`
- **Severity:** **P0**
- **Details:** `X-Principal-ID` is caller-controlled and becomes `requested_by`.
- **Impact:** Audit attribution can be forged.
- **Resolution:** Derive principal from verified authentication. Accept an override only for privileged service-to-service identities with explicit authorization.

## F-016 — Correlation ID is caller-controlled without validation

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/app.py`
- **Severity:** **P2**
- **Details:** Any `X-Correlation-ID` is accepted and placed in logs/context.
- **Impact:** Log injection, excessive identifier size, invalid formats, and cross-request confusion are possible.
- **Resolution:** Validate length/character set/UUID/traceparent semantics. Generate a canonical ID if invalid. Never log arbitrary header values without bounded sanitization.

## F-017 — Tenant ID is copied into request state before authentication

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/app.py`
- **Severity:** **P1**
- **Details:** Middleware sets `request.state.tenant_id` directly from the untrusted header.
- **Impact:** Other middleware/instrumentation may treat an unauthenticated tenant header as trusted identity.
- **Resolution:** Put authenticated principal/tenant in request state only after authentication middleware/dependency succeeds. Separate `requested_tenant` from `authenticated_tenant`.

## F-018 — Readiness checks can report dependency state that does not correspond to the actual runtime wiring

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/v1/routers/health.py`
- **Severity:** **P1**
- **Details:** The database probe calls an in-memory repository `exists()` and labels it `CONNECTED`. Temporal and broker checks can be based on configuration/object presence rather than the exact production client used by the app.
- **Impact:** Kubernetes/load balancer may send traffic to a process that has no durable database or usable execution path.
- **Resolution:** Readiness must probe the actual initialized production components. Separate `live` from `ready`. Add dependency-specific bounded health checks and expose degraded state without leaking credentials.

## F-019 — Temporal dependency can be considered unavailable when optional, but readiness still fails unconditionally

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/v1/routers/health.py`
- **Severity:** **P2**
- **Details:** Missing `temporalio` is treated as a failed readiness condition. This may be valid for a combined API/worker deployment but is incorrect for an API-only topology.
- **Resolution:** Make readiness dependency requirements deployment-profile driven. API-only, worker-only, and all-in-one profiles should have explicit dependency matrices.

## F-020 — Workflow/worker task-queue defaults are inconsistent

- **Package:** `investigation_agent_platform`
- **Source files:** `src/investigation_agent_platform/application/worker/__init__.py`, `src/investigation_agent_platform/api/v1/routers/investigations.py`, `src/investigation_agent_platform/infrastructure/configuration/config.py`
- **Severity:** **P1**
- **Details:** Defaults include `investigations` and `investigation-tasks`.
- **Impact:** API may start workflows on a queue with no worker.
- **Resolution:** Define one configuration object as the source of truth. Eliminate hard-coded queue defaults in individual components. Add startup validation that API and worker configuration agree where required.

## F-021 — Evidence retrieval can return success with zero evidence when gateway is missing

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/application/worker/activities.py`
- **Severity:** **P1**
- **Details:** Missing evidence gateway returns `success=True, items_count=0`.
- **Impact:** The workflow can proceed as if retrieval succeeded when nothing was queried.
- **Resolution:** Missing gateway is a configuration error in production. Return an explicit `EVIDENCE_PROVIDER_UNAVAILABLE` state. Distinguish zero matching records from provider unavailable.

## F-022 — Generic evidence query uses `query_string="*"` and hard-coded production environment

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/application/worker/activities.py`
- **Severity:** **P1**
- **Details:** Retrieval constructs `RuntimeEvidenceRequest(environment="production", query_string="*")`.
- **Impact:** Potentially enormous data scans and incorrect environment selection.
- **Resolution:** Build bounded queries from the investigation/profile/time window. Enforce maximum result size, time range, tenant/application filters, and provider-side pagination. Never use wildcard full-environment scans as the default agent action.

## F-023 — Generic action dispatch silently ignores unsupported actions

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/application/worker/activities.py`
- **Severity:** **P1**
- **Details:** The execution path has special handling for `QUERY_STATE` and otherwise has fallback behavior rather than a strongly typed exhaustive action dispatcher.
- **Resolution:** Use an enum-to-handler registry. Unknown action => non-retryable validation error. Every action must declare required capability, authorization policy, target scope, cost budget, timeout, and evidence output contract.

## F-024 — Agentic loop lacks demonstrated end-to-end evidence → reasoning → action → verification integrity

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/application/worker/workflows.py`
- **Severity:** **P1**
- **Details:** The platform has workflow/activity components, but the supplied implementation contains stub/fallback behavior and does not establish that every reasoning decision is grounded in a current evidence manifest and that every action result is fed back into verification.
- **Resolution:** Define an explicit state machine with immutable evidence manifests and step records. Each action must reference input evidence IDs and produce output evidence IDs. Verification must reject conclusions whose evidence references are absent/stale/untrusted.

## F-025 — Workflow state/checkpoint schema versioning is insufficiently demonstrated

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/application/worker/activities.py`, `src/investigation_agent_platform/api/dependencies.py`
- **Severity:** **P2**
- **Details:** Checkpoint hydration calls `InvestigationState.model_validate(snap)` without an explicit checkpoint schema version/migration path.
- **Resolution:** Persist schema version, workflow version, application version, and state checksum. Implement migrations and backward compatibility. Add replay tests across versions.

## F-026 — Broad exception swallowing hides observability configuration failures

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/app.py`
- **Severity:** **P2**
- **Details:** Global `structlog.configure()` is wrapped in `except Exception: pass`.
- **Resolution:** Configure logging once at process startup. If configuration fails, use a minimal safe fallback and emit a deterministic startup error. Do not silently discard configuration failures.

## F-027 — LLM structured-output request is not actually using the supplied JSON schema

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/infrastructure/reasoning/openai_adapter.py`
- **Severity:** **P1**
- **Details:** When a response schema exists, the adapter sets a generic JSON-object response format rather than enforcing the specific schema at the provider layer. Validation is deferred until after the response.
- **Resolution:** Use the provider's current structured-output/schema mechanism where supported, with strict schema. Retain Pydantic validation as a second boundary. Reject additional fields and malformed structures.

## F-028 — LLM gateway exposes potentially sensitive prompt/evidence content without a formal data policy boundary

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/ports/reasoning/llm_gateway.py`, `src/investigation_agent_platform/application/investigation/reasoning.py`
- **Severity:** **P1**
- **Details:** The gateway contract accepts arbitrary prompt content, while sanitization and data-classification constraints are not part of the gateway request contract.
- **Resolution:** Introduce a redacted/classified prompt envelope containing data classification, allowed provider, tenant, investigation ID, evidence IDs, retention policy, and authorization decision. Block data classes not permitted for the selected model/provider.

## F-029 — LLM cost accounting uses approximate static rates

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/infrastructure/reasoning/openai_adapter.py`
- **Severity:** **P2**
- **Details:** Cost estimation uses hard-coded approximate per-token rates.
- **Impact:** Budget enforcement and financial telemetry can be materially inaccurate as model pricing changes.
- **Resolution:** Make pricing a versioned configuration/service keyed by provider/model/input-output modality. Record actual provider usage and currency. Treat estimates as estimates and never use stale estimates as hard spend limits.

## F-030 — LLM provider selection/factory requires stronger production policy controls

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/infrastructure/reasoning/factory.py`
- **Severity:** **P1**
- **Details:** Provider/model configuration comes from environment variables, but the review surface does not demonstrate tenant-specific model allow-lists, data residency restrictions, or provider capability policy.
- **Resolution:** Add a model registry and policy layer. Validate provider/model against tenant capabilities, region/data-classification constraints, context limits, and cost ceilings before every call.

## F-031 — SQL/RLS session helper relies on transaction-local state but has incomplete rollback/error handling

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/infrastructure/persistence/rls.py`
- **Severity:** **P1**
- **Details:** `rls_session()` sets `SET LOCAL app.tenant_id` and commits on successful exit, but does not explicitly rollback on exceptions.
- **Resolution:** Use `try/except/finally` with rollback on error. Ensure the session cannot be returned to the pool with an unexpected transaction state. Add tests proving cross-tenant queries cannot succeed through pooled connections.

## F-032 — Timeline persistence reconstructs tenant identity as `"unknown"`

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/infrastructure/persistence/timeline_repository.py`
- **Severity:** **P0**
- **Details:** `_from_orm()` constructs `TimelineEvent(tenant_id="unknown", ...)`.
- **Impact:** Tenant identity is corrupted in the domain object and downstream authorization/audit logic can be wrong.
- **Resolution:** Persist tenant ID explicitly or derive it only from an authoritative, transaction-bound context. Never manufacture `"unknown"` for a security-relevant field.

## F-033 — Profile repository contract and implementation signatures are inconsistent

- **Package:** `investigation_agent_platform`
- **Source files:** `src/investigation_agent_platform/ports/persistence/repositories.py`, `src/investigation_agent_platform/infrastructure/persistence/profile_repository.py`, `src/investigation_agent_platform/api/dependencies.py`
- **Severity:** **P1**
- **Details:** The port is tenant-aware, while the concrete profile repository implementation shown uses `save(self, profile)` and other code calls `save(tenant_id, profile)`. This indicates contract drift.
- **Resolution:** Make repository protocols authoritative and run static type checking against concrete implementations. Add contract tests for every repository implementation.

## F-034 — Profile retrieval ignores requested profile version

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/dependencies.py`
- **Severity:** **P1**
- **Details:** `InMemoryApplicationProfileRepository.get_by_application_id()` accepts `version` but ignores it.
- **Impact:** Investigations may execute against a different profile than the one requested/audited.
- **Resolution:** Make profile versions first-class immutable revisions. Resolve an exact version or explicitly resolve the active version and persist the resolved version into the investigation.

## F-035 — Evidence deduplication is process-local and fingerprint collision behavior is weak

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/application/evidence/gateway.py`, `src/investigation_agent_platform/api/dependencies.py`
- **Severity:** **P1**
- **Details:** In-memory fingerprint maps are used, and duplicate saves can silently return.
- **Resolution:** Enforce a durable unique constraint `(tenant_id, fingerprint)` or an appropriate content-addressed identity. Return the canonical evidence ID. Define behavior when semantically identical evidence differs in provenance.

## F-036 — In-memory evidence indexes are not tenant-partitioned at the data-structure level

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/dependencies.py`
- **Severity:** **P2**
- **Details:** Some maps are keyed only by UUID and investigation ID, with tenant checks performed later.
- **Resolution:** Use tenant-scoped composite keys consistently. Treat tenant as a mandatory partition key rather than a post-filter.

## F-037 — Correlation traversal needs stronger tenant/application integrity checks

- **Package:** `investigation_agent_platform/application/correlation/engine.py`
- **Severity:** **P1**
- **Details:** The engine trusts relationship records returned by the repository and builds graph nodes from IDs. The security invariant that every relationship and evidence node belongs to the requested tenant/application must be enforced before graph construction.
- **Resolution:** Validate relationship provenance, tenant, application, evidence existence, and maximum node/edge counts before adding to the graph. Reject or quarantine inconsistent records rather than traversing them.

## F-038 — Correlation graph limits are not consistently propagated from profile/budget policy

- **Package:** `investigation_agent_platform`
- **Source files:** `src/investigation_agent_platform/application/correlation/engine.py`, `src/investigation_agent_platform/domain/profile/models.py`
- **Severity:** **P2**
- **Details:** Engine-level defaults such as `max_node_limit=500` can diverge from investigation-specific budget/policy limits.
- **Resolution:** Pass an immutable execution budget into the engine. Enforce depth, nodes, edges, provider calls, time, and memory limits centrally.

## F-039 — Code intelligence uses deterministic synthetic investigation IDs for searches

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/infrastructure/evidence/code/intelligence.py`
- **Severity:** **P2**
- **Details:** `search_code()` derives a UUID from `tenant_id:query` when no investigation ID is supplied.
- **Impact:** Provenance can be ambiguous and repeated identical queries can share an identifier.
- **Resolution:** Require a real investigation/run ID in evidence-producing operations. If ad-hoc search is supported, create an explicit query-run ID and store it as provenance rather than pretending it is an investigation.

## F-040 — Code intelligence methods discard tenant ID during execution

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/infrastructure/evidence/code/intelligence.py`
- **Severity:** **P1**
- **Details:** Several methods assign `_ = tenant_id`, meaning tenant is not used to constrain filesystem/repository access.
- **Impact:** A shared worker could read another tenant's repository if profile/path resolution is compromised.
- **Resolution:** Resolve repository roots through a tenant-authorized profile registry. Validate canonical paths, repository identity, credentials, and tenant ownership before every code operation.

## F-041 — Repository path traversal defenses need to be centralized and fail closed

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/infrastructure/evidence/code/git.py` / `intelligence.py`
- **Severity:** **P1**
- **Details:** The source contains multiple fallback path-resolution branches. Adversarial path inputs can become difficult to reason about when exceptions trigger alternate roots.
- **Resolution:** Canonicalize path with `resolve()`, require it to remain under an authorized repository root, reject symlink escapes, reject absolute paths unless explicitly authorized, and remove fallback-to-base behavior that changes the security boundary.

## F-042 — MCP tool failures are not enough to establish safe tool isolation

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/infrastructure/evidence/mcp.py`
- **Severity:** **P1**
- **Details:** MCP invocation is present, but the review surface does not establish mandatory per-tool capability authorization, schema validation, timeout, output-size limits, or sandboxing.
- **Resolution:** Put MCP behind a typed tool broker. Validate tool name against tenant capability registry, validate arguments with strict schemas, enforce timeout/concurrency/output limits, redact output, and record immutable tool execution provenance.

## F-043 — Runtime evidence provider needs explicit query-size/time bounds independent of caller

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/infrastructure/evidence/runtime/elastic.py`
- **Severity:** **P1**
- **Details:** A robust agent platform must defend against expensive or broad LLM-generated queries even when a caller is authenticated.
- **Resolution:** Enforce provider-side query DSL allow-list, time window ceiling, maximum buckets/hits, maximum source fields, timeout, circuit breaker, and pagination. Do not rely solely on prompt-level instructions.

## F-044 — State-query safety must not depend only on predefined template names

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/infrastructure/evidence/state/oracle.py`
- **Severity:** **P1**
- **Details:** SQL template validation exists, but production safety also requires parameter typing, tenant/application scope, result-size limits, timeout, and DB role restrictions.
- **Resolution:** Execute using a read-only database identity, bind parameters, validate AST, enforce allowed tables/columns, add tenant predicates where applicable, set statement timeout, and cap result rows. Add database-level least-privilege tests.

## F-045 — Object-storage evidence path design needs lifecycle, encryption, and authorization guarantees

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/infrastructure/sanitization/ingress.py`
- **Severity:** **P1**
- **Details:** Evidence is uploaded under a tenant/investigation key, but the code shown does not establish encryption-at-rest policy, retention/deletion, object ACL policy, versioning, or authorization checks on retrieval.
- **Resolution:** Use private buckets, KMS/customer-managed encryption where required, server-side encryption enforcement, retention/lifecycle rules, immutable provenance metadata, and signed/authorized retrieval through the platform rather than direct public object access.

## F-046 — Sanitization output is stored, but raw-vs-sanitized provenance is not sufficiently modeled

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/infrastructure/sanitization/ingress.py`
- **Severity:** **P2**
- **Details:** The artifact records a sanitized flag and content reference but does not establish a complete chain linking raw source, sanitizer version/policy, transformation hash, and downstream usage.
- **Resolution:** Persist provenance fields: source URI, raw content hash, sanitized content hash, sanitizer version, policy version, timestamp, operator/service identity, and classification.

## F-047 — Prompt escaping is useful but is not a complete prompt-injection defense

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/infrastructure/security/prompt.py`
- **Severity:** **P1**
- **Details:** HTML/XML escaping and delimiters do not prevent a capable model from following malicious instructions contained in evidence.
- **Resolution:** Treat evidence as untrusted data at the architecture level. Separate tool instructions from evidence, use structured input fields, minimize tool authority, require policy checks outside the model, and validate every proposed action independently of model text.

## F-048 — Evidence content can still become a trusted instruction through reasoning context composition

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/application/investigation/reasoning.py`
- **Severity:** **P1**
- **Details:** Evidence is formatted into the reasoning context. Escaping alone does not guarantee instruction/data separation across providers and future prompt templates.
- **Resolution:** Make the LLM contract explicitly structured: system policy, investigation facts, evidence records, candidate actions, and constraints as separate typed fields. Never concatenate arbitrary evidence into policy/instruction strings.

## F-049 — Verification confidence contains hard-coded heuristic scores

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/application/investigation/verification.py`
- **Severity:** **P1**
- **Details:** Reliability/coverage/causal scores include constants such as `0.9`, `0.85`, `0.8`, etc.
- **Impact:** Confidence can appear mathematically precise without a calibrated evidence model.
- **Resolution:** Define explicit scoring inputs and provenance. Prefer deterministic rule-based verification where possible. If confidence is heuristic, label it as such, expose component evidence, calibrate against historical evaluation data, and prevent a confidence number from becoming proof by itself.

## F-050 — Cross-layer evidence verification can be satisfied by evidence-type count alone

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/application/investigation/verification.py`
- **Severity:** **P1**
- **Details:** `sources = {e.evidence_type ...}` and `len(sources) >= 2` treats two evidence types as cross-layer corroboration without proving independence, temporal alignment, causal relevance, or source reliability.
- **Resolution:** Model evidence source identity, independence, reliability, temporal relation, causal relation, and contradiction status. Require corroboration rules that prevent duplicate/derived data from counting as independent evidence.

## F-051 — Conclusion readiness can be detached from actual evidence quality

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/application/investigation/reasoning.py`, `verification.py`
- **Severity:** **P1**
- **Details:** Readiness is produced by the reasoner and verification has separate confidence logic. There is no demonstrated single gate ensuring conclusion publication requires all mandatory verification criteria.
- **Resolution:** Create a `ConclusionGate` domain service that evaluates evidence coverage, contradictions, causal chain, policy requirements, freshness, provenance, and confidence. The LLM may recommend readiness but cannot authorize finalization.

## F-052 — No explicit contradiction-resolution lifecycle is visible

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/domain/hypothesis/models.py`, `application/investigation/verification.py`
- **Severity:** **P2**
- **Details:** Contradictions are represented, but the review surface does not show a durable lifecycle for identifying, resolving, superseding, or accepting contradictions.
- **Resolution:** Add contradiction entities/events with evidence IDs, competing claims, severity, resolution status, resolver rationale, and provenance. Require unresolved high-severity contradictions to block final conclusion.

## F-053 — Planner can repeatedly append gap-repair steps without a hard plan-size guard

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/application/investigation/planner.py`
- **Severity:** **P1**
- **Details:** `replan()` appends a step for every evidence gap. Repeated replanning can cause plan explosion.
- **Resolution:** Enforce maximum steps, revision count, depth, estimated cost, and wall-clock budget. Deduplicate semantically equivalent gaps and detect replan loops.

## F-054 — Agent budget controls are not demonstrated as hard execution gates

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/domain/investigation/budget.py`, `application/worker/activities.py`
- **Severity:** **P1**
- **Details:** Configuration includes maximum tool/reasoning calls and duration, but activity-level code must enforce them durably and atomically.
- **Resolution:** Create a durable per-investigation budget ledger. Increment usage transactionally before execution, reserve budget before expensive operations, and reject operations that would exceed limits.

## F-055 — Duration limits cannot rely solely on application timestamps

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/domain/investigation/budget.py`, `application/worker/workflows.py`
- **Severity:** **P1**
- **Details:** Long-running provider calls, retries, and worker restarts can bypass simplistic elapsed-time checks.
- **Resolution:** Use Temporal workflow deadlines/timeouts as the outer boundary plus provider/activity timeouts and a persisted investigation deadline. Propagate cancellation to providers.

## F-056 — Activity retry classification is too generic

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/application/worker/activities.py`
- **Severity:** **P1**
- **Details:** Generic exceptions become retryable by default.
- **Impact:** Validation/security errors may be retried indefinitely or consume budget.
- **Resolution:** Define explicit retry taxonomy: transient infrastructure, provider throttling, auth, policy, invalid input, not-found, conflict, data-integrity, and bug. Only known transient categories retry. Set bounded attempts/backoff.

## F-057 — External side effects lack explicit idempotency contracts for Temporal retries

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/application/worker/activities.py`
- **Severity:** **P1**
- **Details:** Temporal activities can execute more than once. Evidence writes, event publication, action execution, and external calls need idempotency.
- **Resolution:** Every side-effecting activity must accept a stable activity operation ID and persist a durable execution record. Use provider idempotency keys where available. Return the prior result on retry.

## F-058 — Event publication lacks demonstrated transactional outbox semantics

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/infrastructure/messaging/faststream.py`
- **Severity:** **P1**
- **Details:** Publishing and database state changes are separate distributed operations in the architecture.
- **Impact:** State can commit without an event, or event can publish before state commits.
- **Resolution:** Persist domain events/outbox records in the same DB transaction as state changes. A durable dispatcher publishes them and marks them sent. Consumers must be idempotent.

## F-059 — Event consumer idempotency/deduplication is not demonstrated

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/infrastructure/messaging/faststream.py`
- **Severity:** **P2**
- **Resolution:** Define event IDs, aggregate IDs, tenant IDs, schema versions, ordering keys, retry/DLQ semantics, and durable consumer offsets/deduplication.

## F-060 — API has no demonstrated rate limiting/backpressure

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/app.py` and routers
- **Severity:** **P1**
- **Details:** Investigation creation and evidence endpoints can be called without a platform-level rate limit.
- **Resolution:** Add tenant/principal/IP-aware rate limiting with durable shared state. Apply separate quotas to investigation creation, tool calls, evidence retrieval, and expensive endpoints.

## F-061 — API lacks payload-size and query-cost controls at the edge

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/app.py`, routers
- **Severity:** **P2**
- **Resolution:** Enforce request body limits, parameter lengths, list sizes, pagination ceilings, reason length, problem-description length, and maximum custom-parameter depth/size.

## F-062 — Error responses expose internal exception messages

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/app.py`
- **Severity:** **P1**
- **Details:** Domain exception messages are returned directly.
- **Impact:** SQL/provider/internal implementation details may leak.
- **Resolution:** Map internal exceptions to stable public error codes/messages. Keep detailed diagnostics only in secure logs/traces. Include a correlation ID and optionally a support reference.

## F-063 — Logging may expose tenant IDs, IDs, errors, or sensitive operational metadata excessively

- **Package:** `investigation_agent_platform`
- **Source files:** multiple API/application/infrastructure files
- **Severity:** **P2**
- **Details:** Numerous broad `extra={"error": str(exc)}` and request-related logs exist.
- **Resolution:** Establish a logging policy with field classification, redaction, maximum lengths, structured event names, and allow-listed fields. Never log tokens, prompts, evidence content, SQL parameters, or secrets.

## F-064 — CORS, security headers, and transport security policy are not visible

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/app.py`
- **Severity:** **P2**
- **Resolution:** Configure trusted hosts, CORS allow-list, HSTS where TLS terminates appropriately, X-Content-Type-Options, CSP for docs if exposed, and secure proxy/header handling. Do not trust forwarded headers without configured proxy boundaries.

## F-065 — API documentation exposure policy is too permissive by default

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/app.py`
- **Severity:** **P2**
- **Details:** Docs are enabled by default and can be enabled in production through configuration.
- **Resolution:** Make production docs explicitly opt-in, protect them with authentication, or disable them. Ensure OpenAPI does not expose internal schemas/secrets.

## F-066 — Health endpoint can reveal infrastructure topology

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/v1/routers/health.py`
- **Severity:** **P3**
- **Resolution:** Keep public liveness minimal. Restrict detailed dependency health to internal monitoring/authenticated operators.

## F-067 — Repository concurrency/version semantics need end-to-end verification

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/infrastructure/persistence/investigation_repository.py`
- **Severity:** **P1**
- **Details:** The domain/repository exposes expected-version concurrency, but all aggregate updates and lifecycle commands must use it consistently.
- **Resolution:** Implement `UPDATE ... WHERE id=? AND tenant_id=? AND version=?`, verify affected row count, increment version atomically, and convert zero-row updates into `ConcurrencyError`. Add concurrent mutation tests.

## F-068 — Database schema/model coverage is incomplete for an agentic audit trail

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/infrastructure/persistence/models.py`
- **Severity:** **P1**
- **Details:** The platform needs durable records for workflow runs, tool calls, authorization decisions, evidence provenance, model calls, budgets, checkpoints, transitions, conclusions, and outbox events.
- **Resolution:** Add normalized audit/event entities or append-only event tables. Define indexes/retention and tenant partitioning. Ensure every externally observable action is traceable to an investigation/run/step.

## F-069 — No migration source is present in the supplied artifact

- **Package:** `investigation_agent_platform`
- **Source:** supplied artifact contains ORM models but no migration tree
- **Severity:** **P1**
- **Details:** Production ORM models alone do not provide safe schema evolution.
- **Resolution:** Add Alembic migrations, migration CI, downgrade/forward-compatibility policy, startup version checks, and migration tests. Never auto-create/alter production schema at runtime.

## F-070 — No test suite is present in the supplied source artifact

- **Package:** `investigation_agent_platform`
- **Source:** supplied artifact
- **Severity:** **P0**
- **Details:** The artifact is a source concatenation and does not contain a `tests/` tree. Critical security and distributed-system invariants therefore have no demonstrated executable regression suite.
- **Resolution:** Add a comprehensive test hierarchy:
  - domain unit tests;
  - repository contract tests;
  - API tests;
  - tenant-isolation tests;
  - Temporal workflow tests;
  - activity retry/idempotency tests;
  - provider contract tests;
  - LLM structured-output tests;
  - prompt-injection tests;
  - authorization policy tests;
  - concurrency tests;
  - database/RLS integration tests;
  - outbox/messaging tests;
  - failure-injection tests;
  - end-to-end investigation tests.

## F-071 — No static type/lint/security quality gate is demonstrated

- **Package:** `investigation_agent_platform`
- **Severity:** **P2**
- **Resolution:** Add CI gates for Ruff, mypy/pyright, pytest, coverage threshold, Bandit/Semgrep-equivalent checks, dependency vulnerability scanning, secret scanning, and build/package validation.

## F-072 — Exception handling contains many broad catches that can convert correctness failures into fallback behavior

- **Package:** `investigation_agent_platform`
- **Source:** multiple files, especially bootstrap, activities, evidence providers, reasoning, API startup
- **Severity:** **P1**
- **Details:** Broad `except Exception` is common. In several cases it is followed by a fallback or success response.
- **Resolution:** Replace broad catches with typed exception taxonomy. At system boundaries catch broadly only to translate/log/rethrow. Never convert unknown exceptions into success.

## F-073 — Bootstrap can silently fall back to in-memory mode after configuration failure

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/main.py`
- **Severity:** **P0**
- **Details:** Configuration/bootstrap exceptions can result in a logged message followed by an in-memory fallback.
- **Impact:** A production misconfiguration can produce a running but unsafe/degraded system.
- **Resolution:** In production, configuration failure must terminate startup. Only development/test may use explicit fallback, and the mode must be visible in startup telemetry.

## F-074 — `set_app_context()` accepts arbitrary objects and replaces them with a new in-memory context

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/dependencies.py`
- **Severity:** **P1**
- **Details:** If the supplied object is not an `AppContext`, the function silently creates a fresh `AppContext`.
- **Impact:** Dependency wiring failures become silent dependency replacement.
- **Resolution:** Require a concrete `AppContext` or a typed protocol. Raise `TypeError` on invalid context. Never silently substitute dependencies.

## F-075 — Global module-level application context creates lifecycle/test isolation problems

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/dependencies.py`
- **Severity:** **P2**
- **Details:** `_context` is global process state.
- **Resolution:** Use FastAPI dependency injection/app state for API and explicit dependency objects for workers. Avoid hidden mutable singleton state.

## F-076 — Default profile contains production-looking database/query configuration in application source

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/dependencies.py`
- **Severity:** **P2**
- **Details:** `_DEFAULT_PROFILE` includes database name, tables, and a query template.
- **Resolution:** Remove environment-specific infrastructure metadata from code. Load profiles from durable configuration with secret-free references. Never embed credentials or sensitive topology in source.

## F-077 — SQL template uses string interpolation placeholders for identifiers

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/dependencies.py`
- **Severity:** **P1**
- **Details:** `SELECT * FROM {table} WHERE {pk} = :id` is safe only if identifiers are selected from a strict allow-list before formatting.
- **Resolution:** Store structured query definitions rather than free-form SQL strings where possible. If SQL templates remain, validate identifier names against a closed schema and never accept caller-provided table/column names.

## F-078 — Evidence API lacks cursor pagination and stable ordering

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/v1/routers/evidence.py`
- **Severity:** **P2**
- **Details:** It loads all evidence for an investigation and slices in memory.
- **Impact:** Large investigations become memory/latency hotspots and page contents can shift.
- **Resolution:** Implement database-level keyset pagination with stable `(timestamp,id)` ordering and tenant/investigation predicates.

## F-079 — Timeline API loads all events before slicing

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/v1/routers/timeline.py`
- **Severity:** **P2**
- **Resolution:** Add repository-level pagination and stable ordering. Avoid `all_events[offset:...]` for production data.

## F-080 — Evidence endpoint fails to validate evidence UUID syntax before repository call

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/v1/routers/evidence.py`
- **Severity:** **P2**
- **Details:** `UUID(evidence_id)` is called without converting `ValueError` into the API's normal 400 response.
- **Resolution:** Validate path IDs consistently through typed FastAPI UUID parameters or explicit conversion with stable error handling.

## F-081 — API status codes are inconsistent with domain exception semantics

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/app.py`
- **Severity:** **P2**
- **Details:** `UnauthorizedError` is mapped to 403, while missing authentication is 401. The aliasing of domain exceptions also blurs distinctions.
- **Resolution:** Establish a stable error taxonomy: authentication=401, authorization=403, not found=404, validation=422/400, conflict=409, rate limit=429, upstream dependency=502/503, timeout=504.

## F-082 — API models accept arbitrary `parameters: dict[str, Any]`

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/v1/routers/investigations.py`
- **Severity:** **P2**
- **Details:** Unbounded nested arbitrary parameters increase injection, resource, logging, and schema risks.
- **Resolution:** Define typed per-action parameter models, maximum depth/size, allowed keys, and reject unknown fields where feasible.

## F-083 — Priority is an unconstrained string

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/v1/routers/investigations.py`
- **Severity:** **P2**
- **Resolution:** Use the domain `Priority` enum or a strict Literal. Validate at API boundary.

## F-084 — Session ID default is predictable

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/v1/routers/investigations.py`
- **Severity:** **P2**
- **Details:** `sess_{application_id}` can collide across investigations.
- **Resolution:** Require a true correlation/session identifier or generate a cryptographically random UUID/ULID. Preserve external session IDs only as caller-provided correlation metadata.

## F-085 — Workflow ID construction needs collision and tenant semantics

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/v1/routers/investigations.py`
- **Severity:** **P1**
- **Details:** `wf-investigation-{investigation_id}` is globally unique only if investigation UUIDs are globally unique, but tenant/workflow namespace policy should be explicit.
- **Resolution:** Use a canonical workflow ID schema and enforce uniqueness in the persistence model. Include tenant only if it is part of the intended namespace; never rely on an unverified tenant string for identity.

## F-086 — Workflow control operations do not clearly verify current Temporal execution state

- **Package:** `investigation_agent_platform`
- **Source file:** investigation lifecycle routers/services
- **Severity:** **P1**
- **Resolution:** Define a state synchronization model between DB aggregate state and Temporal execution state. Use Temporal describe/query/update APIs as needed, but persist authoritative control commands and reconcile asynchronously.

## F-087 — Cancellation path can update persistence without confirming workflow cancellation

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/api/v1/routers/investigations.py`, cancellation service
- **Severity:** **P1**
- **Resolution:** Use durable cancellation command semantics. Record `CANCELLATION_REQUESTED`, signal Temporal, then transition to `CANCELLED` only when the workflow confirms cancellation/termination.

## F-088 — API has no explicit optimistic-concurrency token on user-visible lifecycle mutations

- **Package:** `investigation_agent_platform`
- **Severity:** **P1**
- **Resolution:** Expose aggregate version/ETag and support `If-Match` or equivalent for lifecycle mutations. Return 409 on stale commands.

## F-089 — Provider credentials/configuration lack centralized secret-management integration

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/infrastructure/configuration/config.py`
- **Severity:** **P1**
- **Resolution:** Support secret references from the deployment platform/secret manager rather than requiring long-lived plaintext environment secrets. Rotate credentials and never print them.

## F-090 — Provider connectivity is not circuit-broken

- **Package:** `investigation_agent_platform`
- **Source files:** runtime/state/code/LLM providers
- **Severity:** **P2**
- **Resolution:** Add bounded retries, exponential backoff with jitter, circuit breakers, concurrency limits, and per-provider health metrics. Ensure retries respect investigation budget.

## F-091 — Provider timeouts are not consistently propagated

- **Package:** `investigation_agent_platform`
- **Severity:** **P1**
- **Resolution:** Define a timeout budget from workflow → activity → gateway → provider. Use remaining-deadline propagation rather than fixed nested timeouts that can exceed the workflow budget.

## F-092 — Observability adapter dynamically creates metric instruments per call

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/infrastructure/observability/telemetry.py`
- **Severity:** **P2**
- **Details:** `record_metric()` creates a counter dynamically.
- **Resolution:** Pre-register metric instruments. Restrict metric names/tags to an allow-list to prevent high-cardinality or unbounded instrument creation.

## F-093 — Observability spans do not consistently include the provided context/attributes

- **Package:** `investigation_agent_platform`
- **Source file:** `src/investigation_agent_platform/infrastructure/observability/telemetry.py`
- **Severity:** **P2**
- **Details:** `start_span()` returns a tracer span but does not visibly apply context attributes.
- **Resolution:** Attach tenant/investigation/workflow/run/tool/provider attributes with strict privacy filtering. Use trace context propagation across Temporal and external providers.

## F-094 — High-cardinality tenant/evidence identifiers may become metric labels

- **Package:** `investigation_agent_platform`
- **Severity:** **P2**
- **Resolution:** Keep tenant/investigation IDs in traces/logs, not metric dimensions unless cardinality is intentionally bounded.

## F-095 — No explicit data retention/deletion model is visible

- **Package:** `investigation_agent_platform`
- **Severity:** **P1**
- **Resolution:** Define retention by evidence type, investigation state, audit event, LLM interaction, object artifact, and tenant policy. Implement scheduled deletion/tombstoning and cryptographic erasure where required.

## F-096 — No explicit tenant offboarding/data deletion workflow

- **Package:** `investigation_agent_platform`
- **Severity:** **P1**
- **Resolution:** Implement tenant lifecycle operations covering DB rows, object storage, caches, message topics, search indexes, Temporal executions, and telemetry where applicable.

## F-097 — Evidence freshness is modeled but not enforced consistently

- **Package:** `investigation_agent_platform`
- **Severity:** **P2**
- **Resolution:** Attach retrieval timestamp and freshness policy to every evidence item. Verification must reject stale evidence when the investigation profile requires current state.

## F-098 — Source provenance needs tamper-evidence

- **Package:** `investigation_agent_platform`
- **Source files:** `domain/provenance/models.py`, evidence providers
- **Severity:** **P2**
- **Resolution:** Hash source payload/normalized evidence, record provider request fingerprint, source revision, retrieval time, and transformation chain. For high-integrity investigations, append to immutable audit storage.

## F-099 — Model/tool/evidence lineage is not a first-class investigation graph

- **Package:** `investigation_agent_platform`
- **Severity:** **P1**
- **Resolution:** Persist a lineage graph connecting investigation → step → model call → tool call → query → evidence → hypothesis → verification → conclusion. This is required for explainability and forensic replay.

## F-100 — No explicit human-approval gate for potentially consequential actions is demonstrated

- **Package:** `investigation_agent_platform`
- **Severity:** **P1**
- **Details:** Agentic systems should distinguish read-only investigation from consequential remediation.
- **Resolution:** Classify actions by risk. Require explicit human approval for write/remediation/destructive/external side effects. Persist approval actor, timestamp, scope, expiration, and exact action hash.

---

# Cross-cutting architectural requirements for GPT-SOL

## 1. Establish a single authoritative composition root

Create a production `ApplicationContainer` that owns:

- verified authentication/authorization;
- SQLAlchemy async session factory;
- all tenant-aware repositories;
- profile registry;
- evidence gateway;
- evidence sanitizer;
- query policy;
- action authorizer;
- capability registry;
- LLM gateway/reasoner;
- Temporal client;
- event publisher/outbox dispatcher;
- checkpoint store;
- observability;
- rate limiter;
- object storage;
- audit store.

The API and worker must use the same dependency contracts but may instantiate only the components appropriate to their process role.

## 2. Make production fail closed

Forbidden production fallbacks:

- in-memory persistence;
- allow-all security policy;
- fabricated LLM decision;
- empty evidence treated as successful retrieval when provider is unavailable;
- successful API response after Temporal dispatch failure;
- arbitrary tenant/principal headers;
- silently ignored repository wiring errors.

## 3. Define the investigation state machine

At minimum, make lifecycle transitions explicit and persisted:

`CREATED -> DISPATCH_REQUESTED -> RUNNING -> PAUSED -> RUNNING -> VERIFYING -> COMPLETED`

with terminal/error states such as:

`CANCEL_REQUESTED -> CANCELLED`
`FAILED`
`BLOCKED`
`INCONCLUSIVE`

Every transition requires:

- tenant;
- investigation ID;
- previous state;
- next state;
- expected version;
- principal/service identity;
- reason;
- correlation ID;
- workflow ID/run ID;
- timestamp.

## 4. Define a strict agent execution envelope

Every agent step should carry:

```text
tenant_id
principal_id
investigation_id
workflow_id
run_id
step_id
budget_snapshot
deadline
allowed_capabilities
authorization_context
input_evidence_ids
model_policy
output_schema
```

No tool may execute outside this envelope.

## 5. Make evidence the source of truth

A conclusion must reference immutable evidence IDs.

A model statement such as:

> "Evidence confirms X"

must be rejected unless the system can resolve the referenced evidence and verification policy confirms the claim.

## 6. Separate model judgment from platform authority

The LLM can propose:

- observations;
- hypotheses;
- evidence gaps;
- candidate actions;
- explanations.

The platform must decide:

- whether the action is allowed;
- whether the evidence is trusted;
- whether the budget permits the action;
- whether the conclusion meets verification criteria;
- whether a human approval is required;
- whether the investigation can transition state.

## 7. Add adversarial test suites

Minimum security tests:

- forged JWT;
- unsigned JWT;
- expired JWT;
- wrong issuer/audience;
- tenant header spoof;
- principal spoof;
- cross-tenant object ID;
- cross-tenant evidence ID;
- path traversal;
- symlink traversal;
- SQL template injection;
- oversized query;
- wildcard evidence query;
- prompt injection in logs;
- prompt injection in code;
- prompt injection in database rows;
- malicious MCP tool output;
- tool argument smuggling;
- duplicate idempotency key with changed body;
- concurrent idempotency requests;
- Temporal activity retry;
- worker restart after checkpoint;
- DB failure after event creation;
- event publish failure after DB commit;
- stale optimistic-concurrency update.

Minimum agentic correctness tests:

1. No evidence => no conclusion.
2. Provider unavailable => not success.
3. Invalid model output => no conclusion.
4. Contradictory evidence => blocked/inconclusive.
5. Stale evidence => blocked when freshness policy requires current data.
6. Unauthorized tool => never executed.
7. Budget exhausted => no additional tool/LLM call.
8. Human approval required => action remains pending.
9. Temporal retry => external side effect occurs once.
10. Replay from checkpoint => deterministic state recovery.
11. Same idempotency key + same request => same result.
12. Same idempotency key + different request => conflict.

---

# Recommended implementation sequence

## Phase 0 — Safety gate

Fix first:

- F-001
- F-002
- F-003
- F-004
- F-005
- F-006
- F-007
- F-008
- F-009
- F-010
- F-073
- F-074

Do not deploy the platform as an autonomous investigator until these are closed.

## Phase 1 — Durable execution

Then implement:

- durable checkpoints;
- lifecycle transitions;
- Temporal state/control;
- durable idempotency;
- optimistic concurrency;
- outbox;
- action execution records;
- budget ledger;
- provider timeouts/retries;
- repository contract tests.

## Phase 2 — Evidence integrity

Implement:

- immutable evidence manifests;
- provenance/lineage;
- freshness;
- source integrity;
- durable deduplication;
- tenant-scoped evidence;
- query budgets;
- cross-layer verification.

## Phase 3 — Agent security

Implement:

- capability registry;
- typed tool broker;
- action authorization;
- prompt/data boundary;
- model/provider policy;
- human approval;
- adversarial prompt-injection tests.

## Phase 4 — Production hardening

Implement:

- migrations;
- CI quality/security gates;
- rate limiting;
- observability;
- retention/deletion;
- operational dashboards;
- backup/restore tests;
- chaos/failure injection;
- deployment health contracts.

---

# Definition of done

GPT-SOL should not consider the remediation complete merely because tests pass.

The platform is ready for the next review only when all of the following are true:

- production cannot start with in-memory repositories;
- tenant identity comes only from verified authentication;
- every repository is tenant-scoped and contract-tested;
- every Temporal activity is retry-safe and idempotent;
- lifecycle operations actually control Temporal execution;
- workflow start failures are visible to callers;
- checkpoints survive worker restart;
- action authorization is mandatory and deny-by-default;
- prompt safety failures fail closed;
- LLM failures never produce fabricated conclusions;
- no conclusion can exist without evidence provenance;
- verification blocks unresolved critical contradictions;
- budgets are durable and enforced;
- DB state and events use transactional/outbox semantics;
- object storage is private, encrypted, retained, and tenant-scoped;
- rate limits and payload limits are enforced;
- migrations exist and are tested;
- security and integration tests are executable in CI;
- observability provides end-to-end lineage without leaking sensitive content;
- API and worker use one authoritative production dependency graph;
- cross-tenant, retry, restart, and partial-failure tests all pass.

---

# Reviewer evidence notes

The source artifact itself explicitly identifies several implementation shortcuts, including an in-memory API composition root, a JWT decoder that intentionally omits signature verification, an LLM stub fallback, and production bootstrap paths that can fall back to in-memory state. Those are not inferred design concerns; they are direct implementation signals that should be treated as blockers until replaced with production-safe behavior.

