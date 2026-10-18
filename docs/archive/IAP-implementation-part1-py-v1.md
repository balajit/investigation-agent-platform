# Investigation Agent Platform (IAP) — Architectural Specification

## Part 1: Core Platform Foundation, Domain Models, Hexagonal Boundaries & State Engine (Python-Native)

### 1. Executive Summary & Ecosystem Strategy

Part 1 defines the foundational domain models, port abstractions, state machine dynamics, application profiles, security boundaries, and persistence mechanisms for the Python-native Investigation Agent Platform (IAP).

The platform isolates LLM reasoning from execution and state persistence. The LLM operates purely as a decision-making engine proposing typed intents, while execution, policy evaluation, state persistence, deterministic correlation, and memory management remain strictly under platform control.

#### Open-Source Ecosystem Mapping (Java vs. Modern Python)

| Platform Capability | Java Architectural Reference | Python Ecosystem Standard | Strategic Rationale & Integration Mechanism |
| --- | --- | --- | --- |
| **Domain Schemas & Data Contracts** | Java 21 Records / POJOs

 | Pydantic v2 (`BaseModel`, `Field`)

 | Provides Rust-backed schema validation, native JSON Schema extraction for LLM tool binding, and high-performance serialization. Granted as explicit domain exception.

 |
| **Port Interfaces** | Java Interfaces / Abstract Classes

 | `typing.Protocol` / `abc.ABC`<br> | Enables compile/type-check time structural subtyping (static typing via mypy/pyright) and strict decoupling.

 |
| **Concurrency & Async I/O** | Java Virtual Threads (Loom)

 | Python `asyncio` + `uvloop`<br> | Non-blocking concurrent execution of I/O-heavy tool calls (Elasticsearch, Oracle, Git APIs) and LLM streaming endpoints.

 |
| **State Orchestration & Graph FSM** | Custom Execution Loop / Thread Managers

 | Temporal Python SDK Workflows | Operates deterministic Temporal Python Workflows as the single orchestrator and authoritative execution state engine, managing activity retries, state persistence, and signals. |
| **Persistence & ORM** | Hibernate / JPA / JDBC

 | SQLAlchemy 2.0 (Async Engine) + Alembic

 | Async ORM providing transactional integrity, database pooling, append-only event logging, and schema migrations over PostgreSQL executed strictly inside idempotent Temporal Activities. |
| **Heavy Payload Offloading** | Memory-bounded Byte Arrays / Streams

 | `fsspec` / `aioboto3` / `minio`<br> | Storage abstraction interface routing sanitized evidence payloads to Object Storage (S3 / MinIO), keeping core state lightweight. |
| **PII / Secret Redaction** | Custom Regex / Java Security Filters

 | Microsoft Presidio Analyzer + Guardrails AI

 | Enterprise-grade NLP entity detection for redacting API keys, bearer tokens, credentials, and PII at ingress prior to storage and LLM egress. |
| **Observability & Tracing** | Micrometer / OpenTelemetry Java Agent

 | OpenTelemetry Python SDK + Structlog

 | OpenInference-compliant tracing tracking tool invocations, prompt token counts, state diffs, structured logging, and execution latency.

 |

---

### 2. Python Source Package & Project Layout

The codebase strictly adheres to standard Python packaging conventions (`src/` layout), maintaining clear separation between domain models, application orchestration, port interfaces, infrastructure adapters, and platform configuration.

```text
investigation-platform/
├── README.md
├── LICENSE
├── CONTRIBUTING.md
├── CODE_OF_CONDUCT.md
├── ARCHITECTURE.md
├── SECURITY.md
├── pyproject.toml
├── uv.lock
├── docs/
│   ├── architecture/
│   │   ├── overview.md
│   │   ├── domain-model.md
│   │   ├── investigation-lifecycle.md
│   │   ├── evidence-model.md
│   │   ├── evidence-graph.md
│   │   ├── adapter-model.md
│   │   ├── application-profiles.md
│   │   └── adr/
│   │       ├── ADR-001-reasoning-evidence-separation.md
│   │       ├── ADR-002-evidence-as-domain-object.md
│   │       ├── ADR-003-deterministic-correlation.md
│   │       └── ADR-004-mcp-as-adapter.md
│   └── development/
│       ├── local-development.md
│       ├── coding-standards.md
│       └── testing-strategy.md
├── config/
│   ├── application.yaml
│   ├── logging.yaml
│   └── profiles/
│       ├── example.yaml
│       └── payments.yaml
├── scripts/
│   ├── bootstrap.sh
│   ├── dev-start.sh
│   └── dev-stop.sh
├── src/
│   └── iap/
│       ├── __init__.py
│       ├── domain/
│       │   ├── __init__.py
│       │   ├── investigation/
│       │   ├── evidence/
│       │   ├── hypothesis/
│       │   ├── correlation/
│       │   ├── entity/
│       │   ├── timeline/
│       │   ├── finding/
│       │   ├── profile/
│       │   └── common/
│       ├── application/
│       │   ├── __init__.py
│       │   ├── investigation/
│       │   ├── evidence/
│       │   ├── correlation/
│       │   └── profile/
│       ├── ports/
│       │   ├── __init__.py
│       │   ├── evidence/
│       │   ├── persistence/
│       │   ├── reasoning/
│       │   ├── correlation/
│       │   └── profile/
│       ├── infrastructure/
│       │   ├── __init__.py
│       │   ├── persistence/
│       │   ├── configuration/
│       │   ├── messaging/
│       │   └── observability/
│       └── bootstrap/
│           ├── __init__.py
│           └── main.py
├── migrations/
│   └── env.py
├── tests/
│   ├── unit/
│   │   ├── domain/
│   │   └── application/
│   ├── integration/
│   └── fixtures/
├── docker/
│   └── Dockerfile
└── deployment/
    └── k8s/

```

---

### 3. Core Domain Architecture & Entity Specifications

The domain layer is decoupled from infrastructure frameworks, databases, and LLM SDKs. All entities, aggregates, and value objects are defined using immutable Pydantic v2 schemas (`frozen=True`).

```text
                                  +-----------------------+
                                  |     Investigation     |
                                  |    (Aggregate Root)   |
                                  +-----------+-----------+
                                              |
      +-------------------+-------------------+-------------------+-------------------+
      |                   |                   |                   |                   |
      v                   v                   v                   v                   v
+-----------+       +-----------+       +---------------+   +-----------+       +---------------+
|  Evidence |       | Hypothesis|       | Contradiction |   |  Finding  |       | Application   |
+-----------+       +-----------+       +---------------+   +-----------+       | Profile       |
      |                   |                                                   +---------------+
      v                   v
[Sanitized Offload Payload] [Action Plan]
   (S3 / MinIO)          (Tool Requests)

```

#### 3.1 Investigation Aggregate Root & State Schemas

##### `Investigation` (Aggregate Root)

* **Purpose**: Aggregate root managing the identity, lifecycle transitions, state snapshots, and accumulated findings of an investigation.


* **Fields**:
* `id` (`UUID4`): Globally unique investigation ID.


* `session_id` (`str`): Correlation anchor for external application sessions.


* `application_id` (`str`): Target application profile ID.


* `tenant_id` (`str`): Multi-tenant isolation boundary.


* `request` (`InvestigationRequest`): Original user request and triggering constraints.


* `context` (`InvestigationContext`): Evolving runtime context parameters.


* `status` (`InvestigationStatus`): Enumerated lifecycle status.


* `created_at` (`datetime`): UTC timestamp of creation.


* `updated_at` (`datetime`): UTC timestamp of last modification.


* `started_at` (`datetime | None`): Operational start timestamp.


* `completed_at` (`datetime | None`): Terminal completion timestamp.


* `facts` (`list[Fact]`): Validated evidence-backed observations.


* `hypotheses` (`list[Hypothesis]`): Active and historical hypotheses.


* `entities` (`list[InvestigationEntity]`): Discovered system and business domain entities.


* `timeline` (`list[TimelineEvent]`): Chronological sequence of normalized operational events.


* `evidence_references` (`list[EvidenceReference]`): References to retrieved evidence payloads.


* `findings` (`list[Finding]`): Confirmed conclusions and failure root causes.


* `conclusion` (`InvestigationConclusion | None`): Final investigation report and status.


* `metadata` (`dict[str, Any]`): Arbitrary operational metadata.


* `version` (`int`): Monotonically increasing sequence number for optimistic concurrency control.





##### `InvestigationRequest`

* **Purpose**: Encapsulates user input and trigger criteria.


* **Fields**: `problem_description` (`str`), `application_id` (`str`), `session_id` (`str`), `requested_by` (`str`), `requested_at` (`datetime`), `priority` (`str`), `parameters` (`dict[str, Any]`).



##### `InvestigationContext`

* **Purpose**: Operational runtime parameters and dynamic scope discovered during execution.


* **Fields**: `environment` (`str`), `region` (`str`), `deployment_version` (`str`), `service` (`str`), `host` (`str`), `time_window` (`tuple[datetime, datetime]`), `known_identifiers` (`dict[str, str]`), `user_context` (`dict[str, Any]`), `application_metadata` (`dict[str, Any]`).



##### `InvestigationStatus` (Enum)

* `CREATED`: Initialized, pending context expansion.


* `CONTEXTUALIZING`: Resolving application profiles and initial identifiers.


* `INVESTIGATING`: Querying runtime evidence and executing tools.


* `CORRELATING`: Deterministically cross-linking trace IDs, database records, and logs.


* `HYPOTHESIZING`: Formulating candidate explanations.


* `VERIFYING`: Testing hypotheses against refuting/supporting evidence.


* `CONCLUDING`: Generating root cause findings.


* `COMPLETED`: Terminal state with established or insufficient root cause.


* `FAILED`: Terminal state due to budget exhaustion or system error.


* `CANCELLED`: Terminal state initiated by user action.



##### `InvestigationTransition`

* **Purpose**: Audit record of an explicit lifecycle state change.


* **Fields**: `from_status` (`InvestigationStatus`), `to_status` (`InvestigationStatus`), `timestamp` (`datetime`), `actor` (`str`: `SYSTEM`, `ORCHESTRATOR`, `AGENT`, `USER`, `ADMIN`), `reason` (`str`).



##### `InvestigationState`

* **Purpose**: Serializable in-memory snapshot for execution checkpointing and resumption.


* **Fields**: Contains active aggregates (`investigation`, `active_hypotheses`, `known_facts`, `known_entities`, `timeline`, `evidence`, `relationships`, `pending_questions`, `investigation_plan`, `tool_history`, `findings`).



#### 3.2 Facts, Entities & Timeline

##### `Fact` & `FactType`

* **Purpose**: Verified observation established directly by retrieved evidence.


* **`FactType` Enum**: `OBSERVATION`, `STATE`, `EVENT`, `ERROR`, `CODE_BEHAVIOR`, `CONFIGURATION`, `RELATIONSHIP`.


* **Fields**: `id` (`UUID4`), `statement` (`str`), `fact_type` (`FactType`), `source_evidence_ids` (`list[UUID4]`), `confidence` (`float`), `observed_at` (`datetime`), `created_at` (`datetime`), `attributes` (`dict[str, Any]`).



##### `InvestigationEntity` & `EntityType`

* **Purpose**: Represents a domain noun discovered during investigation.


* **`EntityType` Enum**: `SESSION`, `REQUEST`, `USER`, `ORDER`, `TRANSACTION`, `JOB`, `SERVICE`, `HOST`, `DATABASE_RECORD`, `CLASS`, `METHOD`, `COMMIT`, `EXCEPTION`, `TRACE`.


* **Fields**: `id` (`UUID4`), `entity_type` (`EntityType`), `external_identifier` (`str`), `name` (`str`), `attributes` (`dict[str, Any]`), `source_evidence_ids` (`list[UUID4]`).



##### `TimelineEvent` & `TimelineEventType`

* **Purpose**: Chronologically sorted operational event.


* **`TimelineEventType` Enum**: `REQUEST`, `STATE_CHANGE`, `LOG_EVENT`, `DATABASE_EVENT`, `EXCEPTION`, `DEPLOYMENT`, `CODE_CHANGE`, `TRACE_EVENT`, `UNKNOWN`.


* **Fields**: `id` (`UUID4`), `timestamp` (`datetime`), `event_type` (`TimelineEventType`), `description` (`str`), `entity_ids` (`list[UUID4]`), `evidence_ids` (`list[UUID4]`), `attributes` (`dict[str, Any]`).



#### 3.3 Evidence Domain Specifications

##### `Evidence` & `EvidenceId`

* **Purpose**: Platform representation of data gathered from external systems.


* **Type Alias**: `EvidenceId = NewType("EvidenceId", UUID)`.


* **Fields**: `id` (`EvidenceId`), `evidence_type` (`EvidenceType`), `provider` (`str`), `source` (`str`), `title` (`str`), `summary` (`str`), `content_uri` (`str`), `fingerprint` (`str`), `security_classification` (`ClassificationLevel`), `redaction_manifest` (`list[RedactionEntry]`), `observed_at` (`datetime`), `retrieved_at` (`datetime`), `relevance` (`float`), `confidence` (`float`), `attributes` (`dict[str, Any]`), `entity_references` (`list[UUID4]`).



##### `EvidenceReference` & `EvidenceContent`

* **`EvidenceReference`**: Lightweight handle containing `evidence_id`, `provider`, `location`, and `snippet`.


* **`EvidenceContent`**: Payload container supporting `text`, `structured_data`, `source_code`, or `json`.



##### `EvidenceRelationship` & `EvidenceRelationshipType`

* **Purpose**: Directed typed edge between evidence instances forming the Evidence Graph.


* **`EvidenceRelationshipType` Enum**: `DERIVED_FROM`, `CORROBORATES`, `CONTRADICTS`, `RELATES_TO`, `CAUSED_BY`, `OCCURRED_IN`, `BELONGS_TO`, `REFERENCES`, `IMPLEMENTS`, `CHANGED_BY`, `PRECEDES`, `FOLLOWS`.


* **Fields**: `id` (`UUID4`), `source_evidence_id` (`UUID4`), `target_evidence_id` (`UUID4`), `relationship_type` (`EvidenceRelationshipType`), `confidence` (`float`), `created_by` (`str`), `created_at` (`datetime`).



#### 3.4 Hypothesis & Assessment Domain

##### `Hypothesis` & `HypothesisStatus`

* **Purpose**: Candidate explanation for an incident under evaluation.


* **`HypothesisStatus` Enum**: `PROPOSED`, `UNDER_INVESTIGATION`, `SUPPORTED`, `CONTRADICTED`, `VERIFIED`, `REJECTED`.


* **Fields**: `id` (`UUID4`), `statement` (`str`), `status` (`HypothesisStatus`), `confidence_score` (`float`), `supporting_evidence_ids` (`list[UUID4]`), `refuting_evidence_ids` (`list[UUID4]`), `assessments` (`list[HypothesisEvidenceAssessment]`), `required_verification` (`list[str]`), `parent_hypothesis_id` (`UUID4 | None`), `created_at` (`datetime`), `updated_at` (`datetime`).



##### `HypothesisEvidenceAssessment`

* **Purpose**: Explicit reasoning record linking evidence to hypothesis evaluation.


* **Fields**: `hypothesis_id` (`UUID4`), `evidence_id` (`UUID4`), `assessment` (`AssessmentType`: `SUPPORTS`, `CONTRADICTS`, `NEUTRAL`), `strength` (`AssessmentStrength`: `WEAK`, `MODERATE`, `STRONG`, `DECISIVE`), `reason` (`str`).



#### 3.5 Findings & Conclusion Domain

##### `Finding` & `FindingType`

* **Purpose**: Finalized or intermediate verified finding backed by causal evidence.


* **Fields**: `id` (`UUID4`), `finding_type` (`FindingType`), `title` (`str`), `statement` (`str`), `evidence_ids` (`list[UUID4]`), `related_hypothesis_ids` (`list[UUID4]`), `causal_chain` (`list[UUID4]`), `severity` (`SeverityLevel`), `remediation_steps` (`list[str]`), `confidence` (`float`), `created_at` (`datetime`).



##### `InvestigationConclusion` & `ConclusionStatus`

* **Purpose**: Terminal result container for an investigation.


* **Fields**: `status` (`ConclusionStatus`: `ROOT_CAUSE_ESTABLISHED`, `ROOT_CAUSE_LIKELY`, `INSUFFICIENT_EVIDENCE`, `NO_ROOT_CAUSE_FOUND`), `root_cause` (`str | None`), `supporting_evidence_ids` (`list[UUID4]`), `supporting_hypothesis_ids` (`list[UUID4]`), `contradicting_evidence_ids` (`list[UUID4]`), `confidence` (`float`), `limitations` (`list[str]`), `recommended_next_steps` (`list[str]`), `generated_at` (`datetime`).



#### 3.6 Application Profiles Domain

```text
                    Investigation Engine
                           |
             +-------------+-------------+
             |             |             |
        Application A Application B Application C
             |             |             |
          Profile       Profile       Profile
             |             |             |
        ELK/Oracle/Git ELK/PG/Git   OS/Oracle/Git

```

##### `ApplicationProfile`

* **Purpose**: Configuration specification declaring where and how evidence is acquired for a specific application.


* **Fields**: `id` (`str`), `name` (`str`), `description` (`str`), `environment` (`str`), `observability_configuration` (`ObservabilityProfile`), `state_configuration` (`StateProfile`), `code_configuration` (`CodeProfile`), `correlation_configuration` (`CorrelationProfile`), `investigation_configuration` (`InvestigationProfile`).



##### Component Profiles

* **`ObservabilityProfile`**: Fields: `provider`, `indices`, `timestamp_field`, `service_field`, `environment_field`, `session_field`, `request_field`, `trace_field`, `log_level_field`.


* **`StateProfile`**: Fields: `provider`, `database`, `schema`, `tables`, `primary_identifiers`, `state_fields`, `timestamp_fields`, `query_templates` (`dict[str, str]`: Static, parameterized SQL query templates. Dynamic SQL string generation by the LLM is explicitly prohibited).


* **`CodeProfile`**: Fields: `provider`, `repository`, `default_branch`, `language`, `source_roots`, `build_system`, `module_structure`.


* **`CorrelationProfile`**: Fields: `fields` (`list[str]` prioritized order, e.g., `["sessionId", "requestId", "traceId", "transactionId"]`).


* **`InvestigationProfile`**: Fields: `default_time_window`, `max_evidence_per_query`, `max_investigation_duration`, `enabled_evidence_types`, `correlation_depth`, `max_hypotheses`.



#### 3.7 Action & Execution Models

##### `InvestigationAction`

* **Purpose**: Typed intent model proposed by the LLM reasoning layer.


* **Fields**: `action_id` (`UUID4`), `action_type` (`ActionType`), `parameters` (`dict[str, Any]`), `execution_hash` (`str`).



##### `InvestigationResult` & `InvestigationActionRecord`

* **`InvestigationResult`**: Execution output returned by platform adapters containing `action_id`, `status`, `payload_content_uri`, `summary`, `execution_duration_ms`, `retrieved_evidence`.


* **`InvestigationActionRecord`**: Audit record tracking agent requests vs platform execution.



##### `InvestigationLimits`

* **Purpose**: Platform-enforced upper bounds for execution control.


* **Fields**: `max_duration_seconds` (`int`), `max_tool_calls` (`int`), `max_evidence_items` (`int`: Capped default 25–50 items), `max_hypotheses` (`int`), `max_correlation_depth` (`int`), `max_source_files` (`int`), `max_database_rows` (`int`), `max_log_query_window_minutes` (`int`).

* **Budget ownership (verified):** the single operator knob is infra `BudgetConfig` (`IAP_MAX_TOOL_CALLS` default 50, `IAP_MAX_REASONING_CALLS` default 20, `IAP_MAX_DURATION_SECONDS` default 1800). Runtime tracking lives in domain `InvestigationBudget` (`domain/investigation/budget.py`, usage counters + `is_exceeded()`); policy defaults in `application/investigation/guardrails.py`; hard per-query bounds above. The Temporal workflow's `WORKFLOW_LOCAL_ITERATION_GUARD` (10) is a determinism-safe backstop only — never the budget. Change operator budgets via environment, not code constants.

---

### 4. Hexagonal Port Interfaces (`typing.Protocol`)

All external interactions pass through explicit structural subtyping protocols (`typing.Protocol`). Domain and application logic depend solely on these protocols.

```text
                  +---------------------------------------------------+
                  |             INBOUND / DRIVING PORTS               |
                  +-------------------------+-------------------------+
                                            |
                                            v
                  +---------------------------------------------------+
                  |      Investigation Core Engine (State / FSM)      |
                  +-------------------------+-------------------------+
                                            |
                  +-------------------------+-------------------------+
                  |            OUTBOUND / DRIVEN PORTS                |
                  +---------------+---------+---------+---------------+
                                  |                   |
                                  v                   v
                      +-------------------+   +---------------+
                      | ToolExecutionPort |   | LLMReasoning  |
                      +-------------------+   +---------------+
                                  |                   |
                                  v                   v
                      +-------------------+   +---------------+
                      | Persistence Ports |   | EvidenceStore |
                      +-------------------+   +---------------+

```

#### 4.1 Inbound Driving Ports

##### `InvestigationTriggerPort`

```python
class InvestigationTriggerPort(Protocol):
    async def trigger(self, tenant_id: str, request: dict[str, Any]) -> UUID: ...
```

As built (`ports/protocols.py`): the tenant-isolation retrofit narrowed the inbound port to a single `trigger` operation — `tenant_id` is an explicit first argument on every call, and lifecycle reads/mutations (get/resume/cancel) live on the application services in `application/investigation/services.py` (`Create/Get/Resume/CancelInvestigationService`) rather than on the port. The four-operation shape above is superseded; do not reintroduce it without a tenant-scoping review.

#### 4.2 Outbound Driven Ports (Adapters & Infrastructure)

##### Evidence Provider Ports

```python
class RuntimeEvidenceProviderProtocol(Protocol):
    async def search_runtime_evidence(self, tenant_id: str, investigation_id: UUID, request: RuntimeEvidenceRequest, profile: ObservabilityProfile) -> EvidenceQueryResult: ...
    async def get_runtime_evidence(self, tenant_id: str, evidence_id: UUID) -> Evidence: ...

class StateEvidenceProviderProtocol(Protocol):
    async def get_application_state(self, tenant_id: str, investigation_id: UUID, request: ApplicationStateRequest, profile: StateProfile) -> EvidenceQueryResult: ...
    async def search_application_state(self, tenant_id: str, investigation_id: UUID, request: ApplicationStateRequest, profile: StateProfile) -> EvidenceQueryResult: ...

class CodeEvidenceProviderProtocol(Protocol):
    async def get_source(self, tenant_id: str, investigation_id: UUID, file_path: str, profile: CodeProfile) -> Evidence: ...
    async def get_code_history(self, tenant_id: str, investigation_id: UUID, path: str, profile: CodeProfile) -> list[Evidence]: ...
    async def compare_versions(self, tenant_id: str, source_ref: str, target_ref: str, profile: CodeProfile) -> CodeDiffResult: ...

class CodeIntelligenceProviderProtocol(Protocol):
    async def search_code(self, tenant_id: str, query: str, profile: CodeProfile) -> list[Evidence]: ...
    async def find_symbol(self, tenant_id: str, symbol_name: str, profile: CodeProfile) -> list[CodeSymbol]: ...
    async def find_callers(self, tenant_id: str, symbol_name: str, profile: CodeProfile) -> list[CallGraphNode]: ...
    async def find_callees(self, tenant_id: str, symbol_name: str, profile: CodeProfile) -> list[CallGraphNode]: ...
    async def find_exception_handlers(self, tenant_id: str, exception_class: str, profile: CodeProfile) -> list[CodeLocation]: ...
    async def find_database_operations(self, tenant_id: str, entity_or_table: str, profile: CodeProfile) -> list[CodeLocation]: ...

```

As built: every provider operation takes `tenant_id` (and usually `investigation_id`) as explicit leading arguments; raw `dict[str, Any]` requests became typed `RuntimeEvidenceRequest` / `ApplicationStateRequest`; list results became the `EvidenceQueryResult` envelope. The code provider role split in two — `CodeEvidenceProviderProtocol` (raw source/history/diff) vs `CodeIntelligenceProviderProtocol` (AST/symbol/call-graph operations) — so symbol queries return `CodeSymbol` / `CallGraphNode` / `CodeLocation`, not `Evidence`.

##### Deterministic Correlation Port

```python
class CorrelationEngine(Protocol):
    async def correlate(self, tenant_id: str, seed_identifiers: dict[str, str], profile: CorrelationProfile) -> CorrelationResult: ...

class CorrelationExpander(Protocol):
    async def expand_correlation(self, tenant_id: str, application_id: str, root_evidence_ids: list[UUID], max_depth: int) -> CorrelationResult: ...

```

As built (`ports/correlation/engine.py`): correlation takes `tenant_id` first and returns a typed `CorrelationResult`. The gateway additionally consumes `CorrelationExpander` for explicit root-evidence graph traversal, which this section predates.

##### Persistence Repository Ports

```python
class InvestigationRepository(Protocol):
    async def create(self, investigation: Investigation) -> None: ...
    async def get_by_id(self, investigation_id: UUID) -> Investigation | None: ...
    async def save(self, investigation: Investigation, expected_version: int) -> None: ...
    async def delete(self, investigation_id: UUID) -> None: ...
    async def exists(self, investigation_id: UUID) -> bool: ...

class EvidenceRepository(Protocol):
    async def save(self, evidence: Evidence) -> None: ...
    async def save_batch(self, evidence_list: list[Evidence]) -> None: ...
    async def get_by_id(self, evidence_id: UUID) -> Evidence | None: ...
    async def get_by_ids(self, evidence_ids: list[UUID]) -> list[Evidence]: ...
    async def find_by_investigation_id(self, investigation_id: UUID) -> list[Evidence]: ...

class HypothesisRepository(Protocol):
    async def save(self, hypothesis: Hypothesis) -> None: ...
    async def get_by_id(self, hypothesis_id: UUID) -> Hypothesis | None: ...
    async def find_by_investigation_id(self, investigation_id: UUID) -> list[Hypothesis]: ...

class TimelineRepository(Protocol):
    async def append(self, event: TimelineEvent) -> None: ...
    async def append_batch(self, events: list[TimelineEvent]) -> None: ...
    async def find_by_investigation_id(self, investigation_id: UUID) -> list[TimelineEvent]: ...
    async def find_by_time_range(self, investigation_id: UUID, start: datetime, end: datetime) -> list[TimelineEvent]: ...

class ApplicationProfileRepository(Protocol):
    async def get_by_application_id(self, application_id: str) -> ApplicationProfile: ...
    async def list(self) -> list[ApplicationProfile]: ...
    async def save(self, profile: ApplicationProfile) -> None: ...

```

##### Reasoning & Storage Ports

```python
class InvestigationReasoner(Protocol):
    async def reason(self, tenant_id: str, state: InvestigationState) -> InvestigationDecision: ...
    # As built (ports/reasoning/reasoner.py): tenant-scoped, returns InvestigationDecision.

class EvidenceStorePort(Protocol):
    async def store_payload(self, investigation_id: UUID, evidence_id: UUID, raw_bytes: bytes) -> str: ...
    async def fetch_payload(self, content_uri: str) -> bytes: ...

class ObservabilityPort(Protocol):
    def record_step_span(self, investigation_id: UUID, step_name: str) -> ContextManager[Any]: ...
    def record_metric(self, metric_name: str, value: float, tags: dict[str, str]) -> None: ...

```

---

### 5. Application Layer Services, Context & Security Boundaries

#### 5.1 Core Application Services & Runtime Context

Located in `src/iap/application/investigation/`:

1. **`CreateInvestigationService`**: Validates `InvestigationRequest`, resolves `ApplicationProfile`, instantiates `Investigation` aggregate root, persists initial aggregate, and emits `STARTED` event.


2. **`GetInvestigationService`**: Fetches the current aggregate state, timeline, facts, hypotheses, and conclusions for querying clients.


3. **`ResumeInvestigationService`**: Loads persisted investigation, profile, evidence, and timeline to reconstruct the active `InvestigationExecutionContext`. As built, resume restores investigation + profile + evidence + timeline only — facts, hypotheses, and relationships are re-derived by subsequent workflow activities rather than restored verbatim.

4. **`CancelInvestigationService`**: Sets status to `CANCELLED` and persists the transition. As built, actual execution halt is Temporal-signal-based (`RunInvestigationWorkflow` cancel signal + `events.py` pause/resume/approve-action); there is no in-service async task-group cancellation.



##### `InvestigationExecutionContext`

In-memory working context object used during investigation execution:

```python
@dataclass
class InvestigationExecutionContext:
    investigation: Investigation
    profile: ApplicationProfile
    facts: list[Fact]
    hypotheses: list[Hypothesis]
    entities: list[InvestigationEntity]
    evidence: list[Evidence]
    timeline: list[TimelineEvent]
    relationships: list[EvidenceRelationship]
    execution_limits: InvestigationLimits

```

As built (`application/investigation/services.py`): the context carries investigation, profile, facts, hypotheses, entities, evidence, timeline, relationships, and execution limits — `open_questions` and `pending_actions` were dropped (open questions live on the workflow checkpoint; pending actions on the action-execution records).

#### 5.2 Action Validation & Mandatory Security Rules

All actions proposed by the reasoning layer pass through `InvestigationActionValidator` prior to execution.

```text
+---------------------+      +-------------------------------+      +----------------------+
| LLM Reasoner        | ---> | InvestigationActionValidator  | ---> | Tool Adapter / Port  |
| (Proposes Action)   |      | (Enforces Rules 1-8)          |      | (Executes Oper.)     |
+---------------------+      +-------------------------------+      +----------------------+

```

##### Mandatory Platform Security Rules

* **Rule 1**: The LLM cannot directly execute or dynamically generate arbitrary SQL strings. All database queries must execute via static, parameterized SQL templates explicitly declared in the target `StateProfile`.
* **Rule 2**: The LLM cannot directly call Elasticsearch / OpenSearch APIs.


* **Rule 3**: The LLM cannot execute shell commands.


* **Rule 4**: The LLM cannot arbitrarily access local or remote file repositories.


* **Rule 5**: Every external operation must pass through a platform-validated capability and typed action parameters.


* **Rule 6**: Application profiles must never contain credentials or plaintext secrets.


* **Rule 7**: Evidence payloads returned to the reasoning context are capped by `max_evidence_items` (default 25–50 items) and snippet length limits.
* **Rule 8**: All evidence payloads are scrubbed by `EvidenceSanitizer` at ingress before entering Tier-2 storage or the LLM reasoning context.

---

### 6. State Machine Engine, Adaptive Loop & Determinism Controls

State machine orchestration is anchored on **Temporal Python SDK Workflows** as the single orchestrator and authoritative state execution engine. All tool executions and database state updates occur strictly inside idempotent Temporal Activities.

```text
                                 +-----------------------+
                                 |     1. CREATED        |
                                 +-----------+-----------+
                                             |
                                             v
                                 +-----------------------+
                                 |  2. CONTEXTUALIZING   |
                                 +-----------+-----------+
                                             |
                                             v
                     +---------->|   3. INVESTIGATING    |
                     |           +-----------+-----------+
                     |                       |
                     |                       v
                     |           +-----------------------+
                     |           |     4. CORRELATING    |<------------------------+
                     |           +-----------+-----------+                         |
                     |                       |                                     |
                     |                       v                                     |
                     |           +-----------------------+                         | (Re-plan /
                     |           |    5. HYPOTHESIZING   |                         |  Next Step)
                     |           +-----------+-----------+                         |
                     |                       |                                     |
                     |                       v                                     |
                     |           +-----------------------+                         |
                     |           |     6. VERIFYING      |                         |
                     |           +-----------+-----------+                         |
                     |                       |                                     |
                     |                       +-------------------------------------+
                     |                       |
                     |                       v
                     |           +-----------------------+
                     |           |    7. CONCLUDING      |
                     |           +-----------+-----------+
                     |                       |
                     |                       v
                     +-------------------+-----------------------+
                                         |     8. COMPLETED      |
                                         +-----------------------+

```

#### 6.1 State Machine Transitions & Adaptive Loop

1. **`CREATED` $\rightarrow$ `CONTEXTUALIZING**`: Loads application profiles, expands initial correlation vectors.


2. **`CONTEXTUALIZING` $\rightarrow$ `INVESTIGATING**`: Executes initial runtime and log queries via providers.


3. **`INVESTIGATING` $\rightarrow$ `CORRELATING**`: Extracts identifiers (`sessionId`, `requestId`, `traceId`) and executes deterministic graph expansion.


4. **`CORRELATING` $\rightarrow$ `HYPOTHESIZING**`: Evaluates facts to propose candidate hypotheses.


5. **`HYPOTHESIZING` $\rightarrow$ `VERIFYING**`: Dispatches specific verification actions against target systems.


* If new facts contradict current assumptions, transitions back to `INVESTIGATING` or `CORRELATING`.


* If hypotheses are verified or execution budget is reached, transitions to `CONCLUDING`.




6. **`CONCLUDING` $\rightarrow$ `COMPLETED**`: Compiles final `Finding` aggregates and `InvestigationConclusion`.



#### 6.2 Determinism Controls & Budget Enforcement

##### 1. Action Fingerprinting & Deduplication

Before dispatching any action, the platform evaluates a cryptographic fingerprint:

$$F_{\text{action}} = \text{SHA256}\Big(\text{action\_type} \;\parallel\; \text{canonical\_json}(\text{parameters})\Big)$$

If $F_{\text{action}}$ exists in the `tool_history` of the current investigation session, execution is bypassed, and cached results are returned.

##### 2. Hard Execution Budget Enforcement (`InvestigationLimits`)

Every step evaluates execution metrics against limits:

$$\text{EvaluateLimits}() = \begin{cases} \text{TERMINATE\_FAIL}, & \text{if } \text{count}_{\text{tool\_calls}} \ge L_{\text{max\_tool\_calls}} \\ \text{TERMINATE\_FAIL}, & \text{if } \text{count}_{\text{evidence}} \ge L_{\text{max\_evidence\_items}} \\ \text{TERMINATE\_FAIL}, & \text{if } T_{\text{elapsed}} \ge L_{\text{max\_duration}} \\ \text{CONTINUE}, & \text{otherwise} \end{cases}$$

---

### 7. Two-Tier Memory & Heavy Payload Offloading Architecture

To avoid memory bloat during persistent snapshotting, the platform employs a Two-Tier Storage Strategy with mandatory ingress sanitization.

```text
+-----------------------------------------------------------------------+
|                      INGRESS SANITIZATION PIPELINE                    |
|                                                                       |
|  Raw Payload --> [ EvidenceSanitizer: Presidio PII & Secret Redactor ]|
+-----------------------------------------------------------------------+
                                  |
               +------------------+------------------+
               |                                     |
               v                                     v
+-----------------------------------+ +---------------------------------+
|   TIER 1: IN-MEMORY / POSTGRES    | | TIER 2: OBJECT STORAGE (S3/MinIO)|
| - Metadata & Summaries            | | - Scrubbed Un-truncated Payload |
| - Fingerprint & Fact Links        | | - Stack Traces & DB Row Exports |
+-----------------------------------+ +---------------------------------+

```

* **Ingress Sanitization Guard**: All incoming raw evidence payloads are processed by `EvidenceSanitizer` at ingress *before* any tier storage occurs, ensuring secrets or PII are never persisted in object storage or database logs.
* **Tier 1 (In-Memory / Postgres)**: Holds normalized metadata, summaries (capped at 500 tokens), fact links, and status flags. Keeps context snapshot footprint $< 250\text{ KB}$.


* **Tier 2 (S3 / MinIO Storage)**: Streams scrubbed, un-truncated payloads (e.g., sanitized stack traces, filtered DB row exports) directly from adapters via `fsspec` / `aioboto3`.

---

### 8. Safety, Sanitization & Prompt Injection Boundary Pipeline

Data retrieved from external adapters is untrusted. The platform applies a multi-stage scrubbing pipeline prior to Tier-2 offload and LLM context inclusion.

```text
+------------------+     +-------------------+     +-----------------------+     +-------------------+
|  Raw Tool Data   | --> |  1. Presidio NLP  | --> | 2. XML Isolation      | --> |  3. Outbound LLM  |
|  (Adapter Level) |     |  Secret Redactor  |     |  Prompt Wrapper       |     |  Context Window   |
+------------------+     +-------------------+     +-----------------------+     +-------------------+

```

#### 8.1 Step 1: PII and Secret Sanitization

`EvidenceSanitizer` uses Microsoft Presidio and Regex patterns to scrub API Keys, JWT Tokens, Passwords (`postgres://user:pass@host`), and PII at ingress. Redacted values are replaced with tokens (`[REDACTED:API_KEY:hash_prefix]`) and logged in `redaction_manifest`.

#### 8.2 Step 2: Indirect Prompt Injection Isolation

Evidence snippets placed into LLM context are wrapped in structural XML blocks:

```xml
<untrusted_evidence_payload 
  evidence_id="ev-8841-a" 
  source="urn:elasticsearch:logs" 
  classification="CONFIDENTIAL">
  [Sanitized Evidence Snippet Content]
</untrusted_evidence_payload>
```[cite: 5]

---

### 9. Database Schema & Persistence Strategy

State updates follow append-only event logging backed by relational table snapshotting using SQLAlchemy 2.0 and Alembic executed inside Temporal Activities.

#### 9.1 Relational Persistence Tables (SQLAlchemy 2.0)

The underlying database contains 15 explicit relational tables[cite: 5]:

```text
1. INVESTIGATION                 (Aggregate root snapshots & metadata)
2. INVESTIGATION_TRANSITION      (Lifecycle transition audit log)
3. INVESTIGATION_FACT            (Verified facts and evidence references)
4. INVESTIGATION_ENTITY          (Discovered system/business entities)
5. INVESTIGATION_RELATIONSHIP    (Authoritative persistent store for correlation graph edges across workers)
6. EVIDENCE                      (Normalized evidence metadata & summaries)
7. EVIDENCE_REFERENCE            (Lightweight evidence pointers)
8. TIMELINE_EVENT                (Chronological normalized event stream)
9. HYPOTHESIS                    (Candidate explanations and confidence)
10. HYPOTHESIS_EVIDENCE          (Assessment links between evidence & hypotheses)
11. FINDING                      (Validated findings & root cause reports)
12. INVESTIGATION_CONCLUSION     (Terminal investigation reports)
13. INVESTIGATION_ACTION         (Requested actions proposed by LLM)
14. INVESTIGATION_TOOL_EXECUTION (Platform action execution records)
15. APPLICATION_PROFILE          (Onboarded application configurations)

```

Note: `INVESTIGATION_RELATIONSHIP` in PostgreSQL is designated as the primary, authoritative persistent store for all graph correlation edges, avoiding state loss across worker node restarts in multi-worker runtimes.

#### 9.2 Optimistic Concurrency Control (OCC)

State updates verify sequence numbers during persistence:

$$\text{ValidateWrite}(V_{\text{incoming}}, V_{\text{db}}) = \begin{cases} \text{COMMIT}, & \text{if } V_{\text{incoming}} = V_{\text{db}} + 1 \\ \text{REJECT\_CONCURRENCY\_EXCEPTION}, & \text{otherwise} \end{cases}$$

---

### 10. Platform Configuration & Profile Specifications

#### 10.1 Global Platform Configuration (`config/application.yaml`)

```yaml
platform:
  name: investigation-platform
  environment: production

investigation:
  default_timeout_seconds: 1800
  max_tool_calls: 100
  max_hypotheses: 10
  max_evidence_items: 25

persistence:
  provider: postgresql
  connection_pool_size: 20

reasoning:
  provider: anthropic
  model: claude-3-5-sonnet-20241022

observability:
  tracing_enabled: true
  otlp_endpoint: "http://otel-collector:4317"

```

#### 10.2 Logging Configuration (`config/logging.yaml`)

```yaml
version: 1
formatters:
  json:
    class: structlog.stdlib.ProcessorFormatter
    processor: structlog.dev.ConsoleRenderer
handlers:
  console:
    class: logging.StreamHandler
    formatter: json
root:
  level: INFO
  handlers: [console]
```[cite: 5]

#### 10.3 Generic Reference Profile (`config/profiles/example.yaml`)

```yaml
application:
  id: example
  name: Example Generic Application Configuration

observability:
  provider: elastic
  indices:
    - example-application-logs-*
  timestampField: "@timestamp"
  serviceField: service.name
  environmentField: service.environment
  sessionField: labels.sessionId
  requestField: labels.requestId
  traceField: trace.id
  logLevelField: log.level

state:
  provider: postgresql
  database: EXAMPLE_DB
  schema: public
  tables:
    - APP_SESSION
    - APP_TRANSACTION
  primaryIdentifiers:
    - SESSION_ID
    - TRANSACTION_ID
  stateFields:
    - STATUS
  timestampFields:
    - CREATED_AT
  queryTemplates:
    get_session_by_id: "SELECT * FROM APP_SESSION WHERE SESSION_ID = :session_id"
    get_tx_by_session: "SELECT * FROM APP_TRANSACTION WHERE SESSION_ID = :session_id"

code:
  provider: git
  repository: example-service
  defaultBranch: main
  language: python
  sourceRoots:
    - src
  buildSystem: uv
  moduleStructure: single-module

correlation:
  fields:
    - sessionId
    - requestId
    - traceId
    - correlationId

investigation:
  defaultTimeWindow: 1800
  maximumEvidencePerQuery: 25
  maximumInvestigationDuration: 900
  enabledEvidenceTypes:
    - LOG
    - TRACE
    - DATABASE_STATE
  correlationDepth: 2
  maximumHypotheses: 5

```

#### 10.4 Payments Application Reference Profile (`config/profiles/payments.yaml`)

```yaml
application:
  id: payments
  name: Payment Processing Infrastructure

observability:
  provider: elastic
  indices:
    - payments-application-logs-*
  timestampField: "@timestamp"
  serviceField: service.name
  environmentField: service.environment
  sessionField: labels.sessionId
  requestField: labels.requestId
  traceField: trace.id
  logLevelField: log.level

state:
  provider: oracle
  database: PAYMENTS_DB
  schema: APP_PAYMENTS
  tables:
    - PAYMENT_TRANSACTION
    - PAYMENT_SESSION
  primaryIdentifiers:
    - PAYMENT_ID
    - SESSION_ID
  stateFields:
    - STATUS
    - ERROR_CODE
  timestampFields:
    - CREATED_AT
    - UPDATED_AT
  queryTemplates:
    get_payment_tx: "SELECT * FROM APP_PAYMENTS.PAYMENT_TRANSACTION WHERE PAYMENT_ID = :payment_id"
    get_payment_session: "SELECT * FROM APP_PAYMENTS.PAYMENT_SESSION WHERE SESSION_ID = :session_id"

code:
  provider: git
  repository: payments-service
  defaultBranch: main
  language: java
  sourceRoots:
    - src/main/java
  buildSystem: maven
  moduleStructure: single-module

correlation:
  fields:
    - sessionId
    - requestId
    - transactionId
    - correlationId
    - orderId
    - jobId

investigation:
  defaultTimeWindow: 3600
  maximumEvidencePerQuery: 30
  maximumInvestigationDuration: 1800
  enabledEvidenceTypes:
    - LOG
    - TRACE
    - DATABASE_STATE
    - SOURCE_CODE
  correlationDepth: 3
  maximumHypotheses: 5

```

---

### 11. Domain Utilities, Exceptions & Package Dependency Rules

#### 11.1 Infrastructure Dependency Direction Rule

The package hierarchy strictly enforces unidirectionality:

$$\text{domain} \impliedby \text{application} \impliedby \text{ports} \impliedby \text{infrastructure}$$

The `domain` package must contain **zero** external infrastructure, DB, or ML third-party dependencies (no SQLAlchemy, no Elasticsearch SDK, no HTTP clients, no LLM SDKs). `Pydantic v2` is granted as the sole explicit domain layer exception for data contract definitions, validation, and serialization.

#### 11.2 Common Domain Utilities & Exceptions Hierarchy

Located in `src/iap/domain/common/`:

* **`Clock`**: Abstract clock protocol (`utcnow()`) for deterministic testing.


* **`IdentifierGenerator`**: Cryptographically secure UUIDv4 and domain entity ID generator.


* **`DomainSerializer`**: Deterministic JSON encoder for domain aggregate state serialization.


* **`DomainException` Hierarchy**:
* `DomainException` (Base domain exception with machine-readable error codes)


* `InvalidInvestigationStateException`

* `InvalidLifecycleTransitionException`

* `InvalidHypothesisException`

* `EvidenceNotFoundException`

* `ApplicationProfileNotFoundException`

* `InvestigationLimitExceededException`




---

### 12. Testing Strategy, Architectural Decision Records & Acceptance Criteria

#### 12.1 Unit Testing Strategy

Tests in `tests/unit/` mirror domain aggregate boundaries:

* **`test_investigation_lifecycle.py`**: Validates valid state mutations (`CREATED` $\rightarrow$ `CONTEXTUALIZING` $\rightarrow$ `INVESTIGATING` $\rightarrow$ `CORRELATING` $\rightarrow$ `HYPOTHESIZING` $\rightarrow$ `VERIFYING` $\rightarrow$ `CONCLUDING` $\rightarrow$ `COMPLETED`) and rejects invalid state mutations.


* **`test_hypothesis.py`**: Tests hypothesis formulation, evidence assessments (`SUPPORTS`, `CONTRADICTS`), confidence rating updates, and verification rules.


* **`test_evidence.py`**: Tests normalization, fingerprinting, content reference offloading, and relationship creation.


* **`test_investigation_limits.py`**: Verifies strict enforcement of tool execution, duration, and evidence size limits.



#### 12.2 Architectural Decision Records (ADRs)

##### `ADR-001`: Reasoning and Evidence Separation

* **Decision**: Investigation reasoning and evidence acquisition are strictly separated.


* **Rationale**: Reasoning models remain unchanged when replacing infrastructure providers.



##### `ADR-002`: Evidence as First-Class Domain Object

* **Decision**: All logs, database records, and code snippets are normalized into uniform `Evidence` domain objects.


* **Rationale**: LLM reasoners consume a stable semantic model rather than vendor-specific response formats.



##### `ADR-003`: Deterministic Entity Correlation

* **Decision**: Identifiers (`sessionId`, `traceId`, `requestId`) are correlated deterministically via graph rules rather than LLM inference.


* **Rationale**: Guarantees $100\%$ precision for structural identity links without relying on non-deterministic LLM output.



##### `ADR-004`: MCP as Adapter Layer

* **Decision**: Model Context Protocol (MCP) is positioned as an infrastructure adapter beneath the `Evidence Gateway` ports, not as core architecture.


* **Rationale**: Prevents framework lock-in and allows standard REST/gRPC/SDK adapters alongside MCP.



#### 12.3 Part 1 Acceptance Criteria

Part 1 is complete when:

1. **Domain Isolation**: Core domain models have zero third-party framework/SDK imports (with `Pydantic v2` as the single allowed schema exception).
2. **Hexagonal Contracts**: Protocols exist for all inbound triggers, persistence operations, evidence providers, correlation engines, and reasoning engines.


3. **Resumable State**: `InvestigationState` snapshots are fully serializable and support point-in-time resumption.


4. **Safety & Security**: Action validators strictly prevent dynamic SQL, shell, or arbitrary vendor execution.

#### 12.4 Transition to Part 2

Part 2 builds upon these stable contracts to implement the reasoning agent and orchestration loops (`InvestigationOrchestrator`, `InvestigationPlanner`, `HypothesisManager`, `ActionPlanner`, and structured tool dispatchers).

---
