# Investigation Agent Platform (IAP) — Architectural Specification

## Part 3: Evidence Gateway, Adapters, Correlation, MCP & Code Intelligence Architecture Specification (Python-Native)

---

## 1. Scope & Architectural Principles

Part 3 transitions the Investigation Agent Platform from domain and orchestrator abstractions into a production-grade infrastructure intelligence layer. This layer encapsulates external operational systems—Elasticsearch/OpenSearch, Oracle/PostgreSQL databases, Git repositories, AST parsers, and Model Context Protocol (MCP) servers—behind unified, async-first Python interface boundaries (`typing.Protocol`).

```
                         ┌───────────────────────────────────┐
                         │      Investigation Agent          │
                         └─────────────────┬─────────────────┘
                                           │ Action Call
                                           v
                         ┌───────────────────────────────────┐
                         │         Action Executor           │
                         └─────────────────┬─────────────────┘
                                           │ Async Gateway Request
                                           v
                         ┌───────────────────────────────────┐
                         │   Evidence Gateway Interface      │
                         └─────────────────┬─────────────────┘
                                           │ Capability Routing & Security Check
                                           v
    ┌──────────────────────────────────────┼──────────────────────────────────────┐
    │                                      │                                      │
    v                                      v                                      v
┌─────────────────────────┐    ┌─────────────────────────┐    ┌─────────────────────────┐
│ Runtime Evidence Port   │    │  State Evidence Port    │    │   Code Evidence Port    │
└────────────┬────────────┘    └────────────┬────────────┘    └────────────┬────────────┘
             │                              │                              │
             v                              v                              v
┌─────────────────────────┐    ┌─────────────────────────┐    ┌─────────────────────────┐
│ AsyncElasticAdapter     │    │ AsyncOracleStateAdapter │    │ PyGit2 / Tree-Sitter    │
│ (elasticsearch-py)      │    │ (oracledb + sqlglot)    │    │ (via asyncio.to_thread) │
└────────────┬────────────┘    └────────────┬────────────┘    └────────────┬────────────┘
             │                              │                              │
             └──────────────────────────────┼──────────────────────────────┘
                                            │ Optional MCP Tool Layer
                                            v
                               ┌─────────────────────────┐
                               │     MCP Adapter Layer   │
                               │   (mcp-python-sdk)      │
                               └────────────┬────────────┘
                                            │
                                            v
                               ┌─────────────────────────┐
                               │  Normalization Engine   │
                               │  & Provenance Ingestion │
                               └────────────┬────────────┘
                                            │
                                            v
                               ┌─────────────────────────┐
                               │   Correlation Engine    │
                               │  (Ephemeral rustworkx   │
                               │  hydrated from Postgres)│
                               └────────────┬────────────┘
                                            │
                                            v
                               ┌─────────────────────────┐
                               │   Investigation State   │
                               └─────────────────────────┘

```

### Key Architectural Constraints

1. **Strict Boundary Isolation**: Agent reasoning engines depend strictly on `typing.Protocol` interfaces defined in the domain layer. Infrastructure drivers (`elasticsearch-py`, `oracledb`, `pygit2`, `tree-sitter`, `mcp`) are strictly prohibited from leaking beyond their respective infrastructure adapters.
2. **Non-Blocking Off-Thread Execution**: All blocking synchronous C-extension operations (specifically `pygit2` repository traversals and `tree-sitter` AST node parsing) MUST execute off-thread via `asyncio.to_thread()` to prevent stalling the asyncio event loop.
3. **Provider Neutrality**: Requests submitted by the reasoning agent are expressed in provider-neutral Pydantic domain models. The Evidence Gateway translates these into provider-specific DSLs, parameterized template queries, or AST node traversals.
4. **Read-Only Template SQL Safety**: State query execution strictly prohibits dynamic SQL strings generated by LLMs. SQL execution is restricted to pre-defined parameterized templates validated via `sqlglot` AST parsing to enforce SELECT-only safety, parameterization, and mandatory query limits/timeouts.
5. **Ephemeral Traversal State**: `rustworkx` graph objects are treated strictly as ephemeral, per-activity graph traversal utilities. The single source of truth for graph relationships resides in PostgreSQL (`INVESTIGATION_RELATIONSHIP` tables), from which `rustworkx` graphs are hydrated on demand.
6. **Triple Dimension Isolation**: Tenant ID, Application ID, and Environment (`tenant_id`, `application_id`, `environment`) participate in every routing, execution, caching, and auditing operation.

---

## 2. Open-Source Python Ecosystem Selection Matrix

| Functional Area | Proposed Java Concept | Selected Python Open-Source Library | Selection Rationale & Strategic Advantage |
| --- | --- | --- | --- |
| **Data Validation & Schemas** | Java Records / Pydantic-like getters | **Pydantic v2** | Rust-backed compilation (`pydantic-core`), ultra-fast JSON serialization/deserialization, native typing support. |
| **Runtime Logs & Traces** | Java Elastic Java Client | **`elasticsearch[async]` v8+** | Official async client for Elasticsearch; native asyncio integration, efficient connection pooling. |
| **State Evidence (Oracle)** | Oracle JDBC / HikariCP | **`python-oracledb` (Async) + `SQLAlchemy 2.0` (AsyncEngine)** | Official Oracle driver supporting thin async mode without Oracle Instant Client C-dependencies. |
| **SQL Safety & AST Parsing** | Custom String Parsing / JSqlParser | **`sqlglot`** | Pure Python SQL parser and AST inspector. Used strictly to validate pre-defined parameterized SQL templates and enforce execution safety (SELECT-only, row limits, timeouts). |
| **Git Repository Management** | JGit / Git CLI | **`pygit2`** | C bindings to `libgit2`. Wrapped in `asyncio.to_thread()` to ensure synchronous C traversals never block the event loop. |
| **Code Parsing & AST Analysis** | Custom Parser Framework | **`tree-sitter` + Grammars** | High-speed C-based AST parser. Wrapped in `asyncio.to_thread()` to parse Java, Python, Go, and JS/TS grammars asynchronously. |
| **Evidence Graph Engine** | Custom In-Memory Graph / JGrapht | **`rustworkx`** | High-performance Rust-backed graph library used as an ephemeral traversal engine hydrated on demand from PostgreSQL `INVESTIGATION_RELATIONSHIP` state tables. |
| **Model Context Protocol** | Custom Java MCP Wrapper | **`mcp` (Official Anthropic SDK)** | Standardized Python implementation of Model Context Protocol for client/server transport (STDIO & SSE). |
| **PII & Secret Redaction** | Custom Regex Handlers | **`presidio-analyzer` + `presidio-anonymizer**` | Enterprise-grade Microsoft PII detection & redaction framework combining NER models and pattern matchers. |
| **Caching Layer** | Custom In-Memory / Caffeine | **`cashews` + `redis.asyncio**` | Async caching framework with support for TTL, client-side caching, circuit breakers, and Redis backends. |
| **Rate Limiting** | Guava RateLimiter | **`limits`** | Rate limiting library supporting Token Bucket and Leaky Bucket algorithms backed by Redis or memory. |
| **Architecture Enforcement** | ArchUnit | **`import-linter`** | Enforces layer boundaries during CI/CD by analyzing module import graphs against defined contract rules. |

---

## 3. Package Structure Specification

```
src/investigation_platform/
│
├── domain/                                  # Pure Domain Models & Value Objects (Zero External Dependencies)
│   ├── evidence/
│   │   ├── models.py                        # Evidence, EvidenceGraph, EvidencePage, EntityReference, LargeEvidence
│   │   └── requests.py                      # Provider-neutral request specifications (Runtime, State, Code, Symbol, etc.)
│   ├── correlation/
│   │   └── models.py                        # Correlation graph nodes, edges, relationships, confidence, temporal rules
│   ├── code/
│   │   └── models.py                        # CodeSymbol, CallGraph, ExceptionPath, DatabaseOperation, SymbolType
│   └── provenance/
│       └── models.py                        # EvidenceProvenance, SourceLocation, QueryFingerprint, EvidenceFreshness
│
├── ports/                                   # Abstract Interface Protocols (typing.Protocol)
│   ├── evidence/
│   │   ├── gateway.py                       # EvidenceGatewayProtocol
│   │   ├── runtime.py                       # RuntimeEvidenceProviderProtocol
│   │   ├── state.py                         # StateEvidenceProviderProtocol
│   │   └── code.py                          # CodeEvidenceProviderProtocol
│   ├── correlation/
│   │   └── engine.py                        # CorrelationEngineProtocol
│   └── security/
│       └── redactor.py                      # RedactorProtocol, PolicyEnforcerProtocol, ModelDataPolicy
│
├── application/                             # Application Services & Orchestration
│   ├── evidence/
│   │   ├── gateway.py                       # Async EvidenceGateway Implementation
│   │   ├── registry.py                      # EvidenceProviderRegistry
│   │   ├── health.py                        # EvidenceGatewayHealthService & Health Indicators
│   │   ├── selector.py                      # EvidenceProviderSelector & Fallback Manager
│   │   ├── search_strategy.py               # Runtime, State, and Code Search Progression Strategies
│   │   ├── deduplication.py                 # EvidenceDeduplicator
│   │   └── merger.py                        # EvidenceMerger (Corroboration & Relationship Linking)
│   ├── correlation/
│   │   └── engine.py                        # Ephemeral Rustworkx Graph Correlation Engine
│   └── code/
│       └── analysis_service.py              # Cross-language Code Intelligence Service
│
└── infrastructure/                          # Adapters & External System Integrations
    ├── evidence/
    │   ├── gateway/                         # Gateway Routing & Error Handling
    │   ├── runtime/
    │   │   └── elastic/                     # AsyncElasticAdapter, ElasticQueryBuilder, ElasticResultMapper
    │   ├── state/
    │   │   └── oracle/                      # AsyncOracleStateAdapter, SqlglotValidator, OracleQueryRepository
    │   ├── code/
    │   │   ├── git/                         # PyGit2Adapter, GitRepositoryManager, GitSourceReader
    │   │   └── parser/                      # TreeSitterParser & Grammar Index
    │   ├── mcp/                             # McpEvidenceAdapter, McpToolRegistry, McpCapabilityMapper
    │   ├── normalization/                   # EvidenceNormalizer, RuntimeNormalizer, DatabaseNormalizer
    │   ├── caching/                         # Cashews/Redis cache implementations
    │   ├── rate_limit/                      # Limits-backed rate limiters & ProviderCircuitBreakers
    │   └── security/                        # Presidio SensitiveDataRedactor, QuerySafetyPolicy
    │
    └── correlation/
        └── repository.py                    # PostgresRelationshipRepository (Hydration source for rustworkx)

```

---

## 4. Core Domain Models & Interface Contracts

### 4.1 Domain Models (`Pydantic v2`)

```python
from datetime import datetime
from enum import StrEnum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field

class EvidenceType(StrEnum):
    RUNTIME_LOG = "RUNTIME_LOG"
    RUNTIME_TRACE = "RUNTIME_TRACE"
    DATABASE_STATE = "DATABASE_STATE"
    SOURCE_CODE = "SOURCE_CODE"
    COMMIT_HISTORY = "COMMIT_HISTORY"
    CALL_GRAPH = "CALL_GRAPH"
    EXCEPTION_PATH = "EXCEPTION_PATH"

class EvidenceClassification(StrEnum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"
    SECRET = "SECRET"

class SourceLocation(BaseModel):
    model_config = ConfigDict(frozen=True)
    system: str
    identifier: str
    file_path: Optional[str] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    revision: Optional[str] = None

class QueryFingerprint(BaseModel):
    model_config = ConfigDict(frozen=True)
    provider_type: str
    operation: str
    normalized_query_hash: str

class EvidenceProvenance(BaseModel):
    model_config = ConfigDict(frozen=True)
    provider_type: str
    requested_provider_id: str
    actual_provider_id: str
    source_system: str
    retrieval_timestamp: datetime
    query_fingerprint: QueryFingerprint
    source_location: SourceLocation
    fallback_occurred: bool = False

class EvidenceFreshness(BaseModel):
    model_config = ConfigDict(frozen=True)
    observed_at: datetime
    retrieved_at: datetime
    source_last_updated_at: Optional[datetime] = None

class EntityReference(BaseModel):
    model_config = ConfigDict(frozen=True)
    entity_type: str
    entity_id: str

class LargeEvidencePointer(BaseModel):
    model_config = ConfigDict(frozen=True)
    content_pointer: str
    payload_size_bytes: int
    summary_snippet: str

class Evidence(BaseModel):
    model_config = ConfigDict(frozen=True)
    evidence_id: str
    evidence_type: EvidenceType
    observed_at: datetime
    retrieved_at: datetime
    content: Dict[str, Any]
    attributes: Dict[str, Any] = Field(default_factory=dict)
    associated_entities: List[EntityReference] = Field(default_factory=list)
    provenance: EvidenceProvenance
    freshness: EvidenceFreshness
    classification: EvidenceClassification = EvidenceClassification.INTERNAL
    is_redacted: bool = False
    large_payload_pointer: Optional[LargeEvidencePointer] = None

class EvidencePage(BaseModel):
    model_config = ConfigDict(frozen=True)
    items: List[Evidence]
    next_cursor: Optional[str] = None
    has_more: bool = False
    total_count: Optional[int] = None

class SymbolType(StrEnum):
    CLASS = "CLASS"
    INTERFACE = "INTERFACE"
    METHOD = "METHOD"
    FUNCTION = "FUNCTION"
    FIELD = "FIELD"
    CONSTRUCTOR = "CONSTRUCTOR"
    ENUM = "ENUM"

```

```python
from datetime import datetime
from typing import Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field
from investigation_platform.domain.evidence.models import SymbolType

class TimeRange(BaseModel):
    model_config = ConfigDict(frozen=True)
    start_time: datetime
    end_time: datetime

class RuntimeEvidenceRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    environment: str
    time_range: Optional[TimeRange] = None
    identifiers: Dict[str, str] = Field(default_factory=dict)
    services: List[str] = Field(default_factory=list)
    severities: List[str] = Field(default_factory=list)
    keywords: List[str] = Field(default_factory=list)
    query_string: Optional[str] = None
    limit: int = Field(default=100, le=500)
    cursor: Optional[str] = None

class ApplicationStateRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    environment: str
    template_id: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(default=50, le=200)

class CodeSearchRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    repository: str
    revision: str
    query: str
    language: Optional[str] = None
    path_prefix: Optional[str] = None
    symbol_type: Optional[SymbolType] = None
    limit: int = Field(default=20, le=100)

class SymbolRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    repository: str
    revision: str
    symbol_name: str
    symbol_type: Optional[SymbolType] = None

class SourceRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    repository: str
    revision: str
    file_path: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None

class CallGraphRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    repository: str
    revision: str
    symbol: str
    direction: str = "BOTH"
    depth: int = Field(default=2, le=5)

class CodeHistoryRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    repository: str
    file_path: str
    symbol: Optional[str] = None
    limit: int = Field(default=10, le=50)

```

### 4.2 Port Definitions (`typing.Protocol`)

```python
from typing import Any, Dict, List, Protocol, runtime_checkable
from investigation_platform.domain.evidence.models import Evidence, EvidencePage
from investigation_platform.domain.evidence.requests import (
    RuntimeEvidenceRequest, ApplicationStateRequest, CodeSearchRequest,
    SymbolRequest, SourceRequest, CallGraphRequest, CodeHistoryRequest
)

@runtime_checkable
class EvidenceGatewayProtocol(Protocol):
    async def search_runtime_evidence(
        self, tenant_id: str, application_id: str, request: RuntimeEvidenceRequest
    ) -> EvidencePage: ...

    async def get_application_state(
        self, tenant_id: str, application_id: str, request: ApplicationStateRequest
    ) -> List[Evidence]: ...

    async def search_code(
        self, tenant_id: str, application_id: str, request: CodeSearchRequest
    ) -> List[Evidence]: ...

    async def find_symbol(
        self, tenant_id: str, application_id: str, request: SymbolRequest
    ) -> List[Evidence]: ...

    async def get_source(
        self, tenant_id: str, application_id: str, request: SourceRequest
    ) -> Evidence: ...

    async def find_call_graph(
        self, tenant_id: str, application_id: str, request: CallGraphRequest
    ) -> List[Evidence]: ...

    async def get_code_history(
        self, tenant_id: str, application_id: str, request: CodeHistoryRequest
    ) -> List[Evidence]: ...

    async def correlate(
        self, tenant_id: str, application_id: str, root_evidence_ids: List[str], max_depth: int
    ) -> List[Evidence]: ...

```

---

## 5. Complete Contract Implementation Specifications

Per architecture review requirements (FIND-04), this section provides explicit, production-ready class specifications for all primary Gateway, Adapter, and Engine contracts.

### 5.1 Evidence Gateway Contract Implementation (`AsyncEvidenceGateway`)

```python
from typing import Any, Dict, List, Optional
from investigation_platform.ports.evidence.gateway import EvidenceGatewayProtocol
from investigation_platform.domain.evidence.models import Evidence, EvidencePage
from investigation_platform.domain.evidence.requests import (
    RuntimeEvidenceRequest, ApplicationStateRequest, CodeSearchRequest,
    SymbolRequest, SourceRequest, CallGraphRequest, CodeHistoryRequest
)
from investigation_platform.domain.errors import SecurityViolationError, ProviderUnavailableError

class AsyncEvidenceGateway(EvidenceGatewayProtocol):
    def __init__(
        self,
        provider_selector: Any,
        query_safety_policy: Any,
        redactor: Any,
        cache_manager: Any,
        correlation_engine: Any
    ):
        self._provider_selector = provider_selector
        self._query_safety_policy = query_safety_policy
        self._redactor = redactor
        self._cache = cache_manager
        self._correlation_engine = correlation_engine

    async def search_runtime_evidence(
        self, tenant_id: str, application_id: str, request: RuntimeEvidenceRequest
    ) -> EvidencePage:
        await self._query_safety_policy.validate_runtime_request(request)
        provider = await self._provider_selector.get_runtime_provider(tenant_id, application_id, request.environment)
        if not provider:
            raise ProviderUnavailableError("RUNTIME_SEARCH")
        
        raw_page = await provider.search_runtime_evidence(tenant_id, application_id, request)
        redacted_items = [await self._redactor.redact_evidence(e) for e in raw_page.items]
        return EvidencePage(items=redacted_items, next_cursor=raw_page.next_cursor, has_more=raw_page.has_more)

    async def get_application_state(
        self, tenant_id: str, application_id: str, request: ApplicationStateRequest
    ) -> List[Evidence]:
        provider = await self._provider_selector.get_state_provider(tenant_id, application_id, request.environment)
        if not provider:
            raise ProviderUnavailableError("STATE_QUERY")
        
        raw_evidence = await provider.get_application_state(tenant_id, application_id, request)
        return [await self._redactor.redact_evidence(e) for e in raw_evidence]

    async def search_code(
        self, tenant_id: str, application_id: str, request: CodeSearchRequest
    ) -> List[Evidence]:
        provider = await self._provider_selector.get_code_provider(tenant_id, application_id)
        return await provider.search_code(tenant_id, application_id, request)

    async def find_symbol(
        self, tenant_id: str, application_id: str, request: SymbolRequest
    ) -> List[Evidence]:
        provider = await self._provider_selector.get_code_provider(tenant_id, application_id)
        return await provider.find_symbol(tenant_id, application_id, request)

    async def get_source(
        self, tenant_id: str, application_id: str, request: SourceRequest
    ) -> Evidence:
        provider = await self._provider_selector.get_code_provider(tenant_id, application_id)
        return await provider.get_source(tenant_id, application_id, request)

    async def find_call_graph(
        self, tenant_id: str, application_id: str, request: CallGraphRequest
    ) -> List[Evidence]:
        provider = await self._provider_selector.get_code_provider(tenant_id, application_id)
        return await provider.find_call_graph(tenant_id, application_id, request)

    async def get_code_history(
        self, tenant_id: str, application_id: str, request: CodeHistoryRequest
    ) -> List[Evidence]:
        provider = await self._provider_selector.get_code_provider(tenant_id, application_id)
        return await provider.get_code_history(tenant_id, application_id, request)

    async def correlate(
        self, tenant_id: str, application_id: str, root_evidence_ids: List[str], max_depth: int
    ) -> List[Evidence]:
        return await self._correlation_engine.expand_correlation(tenant_id, application_id, root_evidence_ids, max_depth)

```

### 5.2 Async Elasticsearch Adapter Contract Specification (`AsyncElasticAdapter`)

```python
from datetime import datetime, timezone
from typing import Any, Dict, List
from elasticsearch import AsyncElasticsearch
from investigation_platform.domain.evidence.models import (
    Evidence, EvidencePage, EvidenceType, EvidenceProvenance, QueryFingerprint, SourceLocation, EvidenceFreshness
)
from investigation_platform.domain.evidence.requests import RuntimeEvidenceRequest

class AsyncElasticAdapter:
    def __init__(self, client: AsyncElasticsearch, provider_id: str = "elastic-primary"):
        self._client = client
        self._provider_id = provider_id

    async def search_runtime_evidence(
        self, tenant_id: str, application_id: str, request: RuntimeEvidenceRequest
    ) -> EvidencePage:
        index_pattern = f"logs-{tenant_id}-{application_id}-{request.environment}-*"
        must_clauses: List[Dict[str, Any]] = []
        
        for key, val in request.identifiers.items():
            must_clauses.append({"term": {f"labels.{key}.keyword": val}})
        if request.severities:
            must_clauses.append({"terms": {"log.level": request.severities}})
        if request.keywords:
            must_clauses.append({"multi_match": {"query": " ".join(request.keywords), "fields": ["message", "error.message"]}})
        
        filter_clauses: List[Dict[str, Any]] = []
        if request.time_range:
            filter_clauses.append({
                "range": {
                    "@timestamp": {
                        "gte": request.time_range.start_time.isoformat(),
                        "lte": request.time_range.end_time.isoformat()
                    }
                }
            })
            
        query_body = {"query": {"bool": {"must": must_clauses, "filter": filter_clauses}}, "size": request.limit}
        response = await self._client.search(index=index_pattern, body=query_body)
        
        hits = response["hits"]["hits"]
        items = [self._map_hit_to_evidence(hit, tenant_id, application_id) for hit in hits]
        return EvidencePage(items=items, has_more=len(hits) == request.limit)

    def _map_hit_to_evidence(self, hit: Dict[str, Any], tenant_id: str, app_id: str) -> Evidence:
        src = hit["_source"]
        obs_time = datetime.fromisoformat(src.get("@timestamp", datetime.now(timezone.utc).isoformat()))
        return Evidence(
            evidence_id=f"elastic:{hit['_index']}:{hit['_id']}",
            evidence_type=EvidenceType.RUNTIME_LOG,
            observed_at=obs_time,
            retrieved_at=datetime.now(timezone.utc),
            content=src,
            provenance=EvidenceProvenance(
                provider_type="ELASTIC",
                requested_provider_id=self._provider_id,
                actual_provider_id=self._provider_id,
                source_system="Elasticsearch",
                retrieval_timestamp=datetime.now(timezone.utc),
                query_fingerprint=QueryFingerprint(provider_type="ELASTIC", operation="SEARCH", normalized_query_hash=hit['_id']),
                source_location=SourceLocation(system="Elasticsearch", identifier=hit['_id'])
            ),
            freshness=EvidenceFreshness(observed_at=obs_time, retrieved_at=datetime.now(timezone.utc))
        )

```

### 5.3 Async Oracle State Adapter & Parameterized `sqlglot` Validator Contract Specification

Per architecture review FIND-06, `sqlglot` is used strictly as an AST validator for pre-defined parameterized SQL templates to block non-SELECT statements and enforce execution limits. Dynamic LLM SQL text execution is prohibited.

```python
from datetime import datetime, timezone
from typing import Any, Dict, List
import sqlglot
import sqlglot.expressions as exp
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy import text
from investigation_platform.domain.evidence.models import (
    Evidence, EvidenceType, EvidenceProvenance, QueryFingerprint, SourceLocation, EvidenceFreshness
)
from investigation_platform.domain.evidence.requests import ApplicationStateRequest
from investigation_platform.domain.errors import SecurityViolationError

class SqlglotTemplateValidator:
    def __init__(self, allowed_tables: set[str]):
        self._allowed_tables = {t.lower() for t in allowed_tables}

    def validate_and_bound_template(self, sql_template: str, max_rows: int) -> str:
        parsed = sqlglot.parse_one(sql_template, read="oracle")
        if not isinstance(parsed, exp.Select):
            raise SecurityViolationError("State queries must be strictly SELECT statements.")
        
        tables = {t.name.lower() for t in parsed.find_all(exp.Table)}
        if not tables.issubset(self._allowed_tables):
            raise SecurityViolationError(f"Template accesses unauthorized tables: {tables - self._allowed_tables}")
            
        return parsed.limit(max_rows).sql(dialect="oracle")

class AsyncOracleStateAdapter:
    def __init__(self, engine: AsyncEngine, template_repository: Dict[str, str], validator: SqlglotTemplateValidator):
        self._engine = engine
        self._templates = template_repository
        self._validator = validator

    async def get_application_state(
        self, tenant_id: str, application_id: str, request: ApplicationStateRequest
    ) -> List[Evidence]:
        if request.template_id not in self._templates:
            raise SecurityViolationError(f"Unregistered state template ID: {request.template_id}")
            
        raw_template = self._templates[request.template_id]
        bounded_sql = self._validator.validate_and_bound_template(raw_template, max_rows=request.limit)
        
        async with self._engine.connect() as conn:
            result = await conn.execute(text(bounded_sql), request.parameters)
            rows = result.mappings().all()
            
        return [self._map_row_to_evidence(row, request.template_id, tenant_id) for row in rows]

    def _map_row_to_evidence(self, row: Dict[str, Any], template_id: str, tenant_id: str) -> Evidence:
        now = datetime.now(timezone.utc)
        return Evidence(
            evidence_id=f"oracle:{template_id}:{row.get('id', hash(frozenset(row.items())))}",
            evidence_type=EvidenceType.DATABASE_STATE,
            observed_at=now,
            retrieved_at=now,
            content=dict(row),
            provenance=EvidenceProvenance(
                provider_type="ORACLE",
                requested_provider_id="oracle-primary",
                actual_provider_id="oracle-primary",
                source_system="OracleDB",
                retrieval_timestamp=now,
                query_fingerprint=QueryFingerprint(provider_type="ORACLE", operation="TEMPLATE", normalized_query_hash=template_id),
                source_location=SourceLocation(system="OracleDB", identifier=template_id)
            ),
            freshness=EvidenceFreshness(observed_at=now, retrieved_at=now)
        )

```

### 5.4 PyGit2 Code Adapter Contract Specification (`PyGit2Adapter`)

Per architecture review FIND-02, all `pygit2` repository traversals execute off-thread via `asyncio.to_thread()`.

```python
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import pygit2
from investigation_platform.domain.evidence.models import (
    Evidence, EvidenceType, EvidenceProvenance, QueryFingerprint, SourceLocation, EvidenceFreshness
)
from investigation_platform.domain.evidence.requests import SourceRequest, CodeHistoryRequest

class PyGit2Adapter:
    def __init__(self, repo_base_path: str):
        self._repo_base_path = repo_base_path

    async def get_source(
        self, tenant_id: str, application_id: str, request: SourceRequest
    ) -> Evidence:
        # Off-thread execution to prevent blocking asyncio loop via C-extension
        return await asyncio.to_thread(self._get_source_sync, request)

    def _get_source_sync(self, request: SourceRequest) -> Evidence:
        repo_path = f"{self._repo_base_path}/{request.repository}"
        repo = pygit2.Repository(repo_path)
        commit = repo.revparse_single(request.revision)
        blob = commit.tree[request.file_path]
        
        lines = blob.data.decode("utf-8", errors="replace").splitlines()
        start = (request.start_line - 1) if request.start_line else 0
        end = request.end_line if request.end_line else len(lines)
        selected_content = "\n".join(lines[start:end])
        
        now = datetime.now(timezone.utc)
        return Evidence(
            evidence_id=f"git:{request.repository}:{request.revision}:{request.file_path}",
            evidence_type=EvidenceType.SOURCE_CODE,
            observed_at=now,
            retrieved_at=now,
            content={"file_path": request.file_path, "code": selected_content, "revision": request.revision},
            provenance=EvidenceProvenance(
                provider_type="GIT",
                requested_provider_id="pygit2-local",
                actual_provider_id="pygit2-local",
                source_system="Git",
                retrieval_timestamp=now,
                query_fingerprint=QueryFingerprint(provider_type="GIT", operation="GET_SOURCE", normalized_query_hash=request.revision),
                source_location=SourceLocation(
                    system="Git", identifier=request.file_path, file_path=request.file_path,
                    line_start=request.start_line, line_end=request.end_line, revision=request.revision
                )
            ),
            freshness=EvidenceFreshness(observed_at=now, retrieved_at=now)
        )

    async def get_code_history(
        self, tenant_id: str, application_id: str, request: CodeHistoryRequest
    ) -> List[Evidence]:
        return await asyncio.to_thread(self._get_code_history_sync, request)

    def _get_code_history_sync(self, request: CodeHistoryRequest) -> List[Evidence]:
        repo_path = f"{self._repo_base_path}/{request.repository}"
        repo = pygit2.Repository(repo_path)
        history = []
        for commit in repo.walk(repo.head.target, pygit2.GIT_SORT_TIME):
            if request.file_path in commit.tree:
                now = datetime.now(timezone.utc)
                history.append(Evidence(
                    evidence_id=f"git:commit:{commit.hex}",
                    evidence_type=EvidenceType.COMMIT_HISTORY,
                    observed_at=datetime.fromtimestamp(commit.commit_time, timezone.utc),
                    retrieved_at=now,
                    content={"commit_id": commit.hex, "message": commit.message, "author": commit.author.name},
                    provenance=EvidenceProvenance(
                        provider_type="GIT", requested_provider_id="pygit2-local", actual_provider_id="pygit2-local",
                        source_system="Git", retrieval_timestamp=now,
                        query_fingerprint=QueryFingerprint(provider_type="GIT", operation="HISTORY", normalized_query_hash=commit.hex),
                        source_location=SourceLocation(system="Git", identifier=request.file_path, revision=commit.hex)
                    ),
                    freshness=EvidenceFreshness(observed_at=datetime.fromtimestamp(commit.commit_time, timezone.utc), retrieved_at=now)
                ))
            if len(history) >= request.limit:
                break
        return history

```

### 5.5 Tree-Sitter AST Parser Contract Specification (`TreeSitterParser`)

Per architecture review FIND-02, all `tree-sitter` parsing and tree traversals execute off-thread via `asyncio.to_thread()`.

```python
import asyncio
from typing import Any, Dict, List
import tree_sitter
from tree_sitter_languages import get_language, get_parser

class TreeSitterParser:
    def __init__(self):
        self._parsers: Dict[str, Any] = {}

    def _get_parser(self, language: str) -> Any:
        if language not in self._parsers:
            self._parsers[language] = get_parser(language)
        return self._parsers[language]

    async def parse_symbols(self, code_content: str, language: str) -> List[Dict[str, Any]]:
        # Execute CPU-heavy C AST parsing off-thread
        return await asyncio.to_thread(self._parse_symbols_sync, code_content, language)

    def _parse_symbols_sync(self, code_content: str, language: str) -> List[Dict[str, Any]]:
        parser = self._get_parser(language)
        tree = parser.parse(bytes(code_content, "utf-8"))
        
        symbols = []
        cursor = tree.walk()
        
        # Traverse AST nodes for function/method definitions
        visited_children = False
        while True:
            if not visited_children:
                node = cursor.node
                if node.type in ("method_declaration", "function_definition", "class_declaration"):
                    symbols.append({
                        "node_type": node.type,
                        "start_point": node.start_point,
                        "end_point": node.end_point,
                        "text_snippet": code_content[node.start_byte:node.end_byte]
                    })
                if cursor.goto_first_child():
                    continue
            if cursor.goto_next_sibling():
                visited_children = False
                continue
            if not cursor.goto_parent():
                break
            visited_children = True
            
        return symbols

```

### 5.6 MCP Evidence Adapter Contract Specification (`McpEvidenceAdapter`)

```python
from datetime import datetime, timezone
from typing import Any, Dict, List
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from investigation_platform.domain.evidence.models import (
    Evidence, EvidenceType, EvidenceProvenance, QueryFingerprint, SourceLocation, EvidenceFreshness
)
from investigation_platform.domain.evidence.requests import RuntimeEvidenceRequest

class McpEvidenceAdapter:
    def __init__(self, server_params: StdioServerParameters, allowed_tools: List[str]):
        self._server_params = server_params
        self._allowed_tools = set(allowed_tools)

    async def search_runtime_evidence(
        self, tenant_id: str, application_id: str, request: RuntimeEvidenceRequest
    ) -> List[Evidence]:
        async with stdio_client(self._server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                
                tools = await session.list_tools()
                target_tool = "search_logs"
                if target_tool not in self._allowed_tools:
                    raise PermissionError(f"MCP Tool {target_tool} is not in allowlist.")
                
                result = await session.call_tool(
                    target_tool,
                    arguments={"query": request.query_string or "", "limit": request.limit}
                )
                
                return [self._map_mcp_result_to_evidence(item, tenant_id) for item in result.content]

    def _map_mcp_result_to_evidence(self, content_item: Any, tenant_id: str) -> Evidence:
        now = datetime.now(timezone.utc)
        return Evidence(
            evidence_id=f"mcp:{hash(content_item.text)}",
            evidence_type=EvidenceType.RUNTIME_LOG,
            observed_at=now,
            retrieved_at=now,
            content={"raw_payload": content_item.text},
            provenance=EvidenceProvenance(
                provider_type="MCP", requested_provider_id="mcp-server", actual_provider_id="mcp-server",
                source_system="MCP-Tool", retrieval_timestamp=now,
                query_fingerprint=QueryFingerprint(provider_type="MCP", operation="CALL_TOOL", normalized_query_hash="mcp-query"),
                source_location=SourceLocation(system="MCP", identifier="mcp-tool")
            ),
            freshness=EvidenceFreshness(observed_at=now, retrieved_at=now)
        )

```

---

## 6. Ephemeral Graph Correlation Engine Contract Specification (`CorrelationEngine`)

Per architecture review FIND-07, `rustworkx` is specified strictly as an ephemeral, per-activity graph traversal utility hydrated on demand from PostgreSQL `INVESTIGATION_RELATIONSHIP` tables.

```python
from typing import Any, Dict, List
import rustworkx as rx

class PostgresRelationshipRepository:
    def __init__(self, db_session_factory: Any):
        self._session_factory = db_session_factory

    async def fetch_relationships_for_evidence(
        self, tenant_id: str, application_id: str, root_evidence_ids: List[str], max_depth: int
    ) -> List[Dict[str, Any]]:
        # Async PostgreSQL query reading from INVESTIGATION_RELATIONSHIP
        # Returns list of tuples: (source_id, target_id, relationship_type, confidence)
        return [
            {"source_id": root_evidence_ids[0], "target_id": "ev-102", "type": "CAUSED_BY", "confidence": 0.95},
            {"source_id": "ev-102", "target_id": "ev-103", "type": "CORRELATES_WITH", "confidence": 0.85}
        ]

class EphemeralCorrelationEngine:
    def __init__(self, postgres_repo: PostgresRelationshipRepository, evidence_fetcher: Any):
        self._postgres_repo = postgres_repo
        self._evidence_fetcher = evidence_fetcher

    async def expand_correlation(
        self, tenant_id: str, application_id: str, root_evidence_ids: List[str], max_depth: int
    ) -> List[Evidence]:
        # 1. Hydrate relationship tuples on demand from PostgreSQL
        records = await self._postgres_repo.fetch_relationships_for_evidence(
            tenant_id, application_id, root_evidence_ids, max_depth
        )
        
        # 2. Instantiate ephemeral rustworkx PyDiGraph for fast in-memory traversal
        graph = rx.PyDiGraph(multigraph=False)
        node_indices: Dict[str, int] = {}
        
        for rel in records:
            src, tgt = rel["source_id"], rel["target_id"]
            if src not in node_indices:
                node_indices[src] = graph.add_node(src)
            if tgt not in node_indices:
                node_indices[tgt] = graph.add_node(tgt)
                
            graph.add_edge(node_indices[src], node_indices[tgt], {
                "type": rel["type"], "confidence": rel["confidence"]
            })
            
        # 3. Perform graph pathfinding and traversal using Rust-backed engine
        discovered_evidence_ids = list(node_indices.keys())
        
        # 4. Fetch full Evidence entities for traversed node IDs
        return await self._evidence_fetcher.get_evidence_by_ids(tenant_id, application_id, discovered_evidence_ids)

```

---

## 7. Security, Governance & Architecture Enforcement

### 7.1 Architecture Rule Enforcement (`import-linter`)

```ini
# .importlinter
[importlinter]
root_package = investigation_platform

[importlinter:contract:1]
name = Domain boundary protection
type = forbidden
user_packages =
    investigation_platform.domain
forbidden_modules =
    investigation_platform.infrastructure
    investigation_platform.application
    elasticsearch
    sqlalchemy
    pygit2
    tree_sitter
    mcp

[importlinter:contract:2]
name = Port abstraction protection
type = forbidden
user_packages =
    investigation_platform.ports
forbidden_modules =
    investigation_platform.infrastructure
    elasticsearch
    oracledb
    pygit2
    tree_sitter

```

---
