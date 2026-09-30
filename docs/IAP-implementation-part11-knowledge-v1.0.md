# IAP Implementation Part 11: Knowledge Document v1.0 — Gap Analysis vs. `prism-agent`

Review scope: `~/SourceCode/comcast/comcast-xcp-shared-services/prism-agent/src` (33 files, LangGraph-based single-purpose account-analysis agent) compared against the current Investigation Agent Platform (IAP) tree. This document identifies capabilities present in `prism-agent` that IAP does not yet have, and specifies **generalized, componentized** designs for each — decoupled from `prism-agent`'s domain (Xfinity account/policy-gap analysis) so they serve any IAP-onboarded application. No code from `prism-agent` is copied; every design below is derived from IAP's existing conventions (Temporal workflows, `ApplicationProfile`, ports/adapters, `AppContext`) and reimagines the *pattern* prism-agent proves out, not its implementation.

This is a design document only — no code changes are included here. Implementation phases live in `docs/IAP-implementation-part11-plan-todo.md`.

---

## 0. Method

`prism-agent` is a single-tenant, single-purpose LangGraph agent: one `StateGraph` (`check_transcripts → fetch_elk → kb_search → llm_analysis → write_report`) plus a bulk fan-out wrapper, a FastAPI shell, a ChromaDB doc-search sidecar, an LLM-driven clustering pass, and a static HTML reporter. It solves a narrow problem (customer account policy-gap analysis) end-to-end, including several capabilities IAP has **not yet built** because Parts 1–10 focused on the evidence/investigation core. Nine gaps were identified by reading every file in the tree and cross-checking against IAP's `src/` (searched for `cluster|bulk|chat|aggregate|chromadb|job.*queue|websocket|oauth|SAT` — all absent in IAP except as incidental substrings).

Each gap below states: **what prism-agent does**, **why it doesn't transfer as-is**, **the generalized IAP capability**, and **where it plugs into the existing architecture** (ports, `AppContext` attributes, `ApplicationProfile` extension points, Temporal workflows).

---

## 1. Gap Inventory (summary table)

| # | Gap | prism-agent evidence | IAP absence confirmed |
|---|---|---|---|
| 1 | Structured input-required interrupt | `check_transcripts.py` uses LangGraph `interrupt()` | IAP signals are `pause`/`resume`/`cancel`/`action_approval_response` — none model "workflow needs caller-supplied data before it can proceed" |
| 2 | Bulk/batch multi-entity intake + fan-out | `bulk_graph.py` (`Send()` fan-out over CSV rows) | No multi-record intake or parallel-dispatch orchestration anywhere in `application/` |
| 3 | Cross-investigation root-cause clustering | `clustering/clusterer.py` (LLM batch clustering + Chroma semantic candidate retrieval) | No clustering of `Finding`/`InvestigationConclusion` across investigations |
| 4 | External reference-doc semantic search | `kb/vector_store.py` (ChromaDB over cloned markdown repos) | Mem0/Graphiti store investigation-*derived* knowledge; nothing indexes static external reference docs |
| 5 | Cross-investigation aggregate + static report | `report/account_aggregate.py`, `report/account_report.py` | No aggregation-across-investigations or renderable dashboard artifact |
| 6 | Conversational investigation chat | `api/routes/chat.py` (SSE, session store, trigger keyword) | MCP tools are agent-to-agent, not human-conversational; no chat session concept |
| 7 | Generic async job/progress tracking | `api/jobs.py` + `/status/ws/{job_id}` | Investigation status exists but nothing generic for non-Investigation background work (KB indexing, clustering runs, report exports) |
| 8 | Downstream credential provider (OAuth2/SAT) | `tool_registry.py::_get_sat_token` | No pluggable outbound-credential abstraction anywhere in `infrastructure/evidence/` |
| 9 | Resilient LLM JSON extraction | `llm_analysis.py::_parse_llm_json` (brace-repair for truncated JSON) | IAP relies solely on provider `response_format=json_schema`; no fallback repair path for providers/responses that don't honor it |

Also noted but **out of scope for Part 11** (call out explicitly so they aren't silently dropped): prism-agent's bundled static single-page UI (`ui/index.html`) and its APScheduler weekly cron (Temporal already does scheduled workflows better — no gap). These are flagged in §9 as explicitly deferred, not missing.

---

## 2. Gap 1 — Structured Input-Required Interrupt

### 2.1 What prism-agent does

`check_transcripts` node checks `state.get("transcripts")`; if absent, it calls LangGraph's `interrupt({...required_fields...})`, which suspends the graph and returns a structured prompt describing exactly what data is needed. The caller (CLI, API background task, or MCP tool) resumes the graph with the missing data injected into state via the checkpointer.

### 2.2 Why it doesn't transfer as-is

LangGraph's `interrupt()`/checkpointer model is a general-purpose graph-suspension primitive. IAP's orchestration is Temporal, which already has an equivalent-but-different mechanism (signals + `workflow.wait_condition`), used today only for **operator control** (pause/resume/cancel/approve-action) — never for **the workflow itself declaring it is blocked on missing input**.

### 2.3 Generalized design: `InputRequirement` signal + `AWAITING_INPUT` status

**Domain** (`domain/investigation/models.py`, additive):

```python
class InputRequirement(BaseModel):
    """A structured description of data the workflow cannot proceed without."""
    model_config = ConfigDict(frozen=True)

    requirement_id: str = Field(..., max_length=64)          # stable key, e.g. "customer_transcripts"
    reason: str = Field(..., max_length=512)                  # human-readable why
    fields: dict[str, str] = Field(default_factory=dict, max_length=50)  # field_name -> description
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class InvestigationStatus(StrEnum):
    ...
    AWAITING_INPUT = "AWAITING_INPUT"   # new terminal-until-resumed state
```

**Workflow** (`application/worker/workflows.py`, additive method on `RunInvestigationWorkflow`):

```python
@workflow.signal
async def provide_input(self, requirement_id: str, data: dict[str, Any]) -> None:
    self._provided_input[requirement_id] = data
    self._input_provided_event.set()

async def _require_input(self, requirement: InputRequirement) -> dict[str, Any]:
    """Persist AWAITING_INPUT, signal via outbox, wait_condition on provide_input."""
    self._pending_requirement = requirement
    # persisted via existing transition/outbox machinery (F-012/F-013 precedent)
    await workflow.wait_condition(lambda: requirement.requirement_id in self._provided_input)
    return self._provided_input.pop(requirement.requirement_id)
```

Activities that discover missing prerequisite data (e.g. an evidence-gathering activity that needs caller-supplied context the profile can't source automatically) raise a typed `InputRequiredError(requirement: InputRequirement)`; the workflow's activity-dispatch loop catches this specific exception (not generic `Exception`), transitions to `AWAITING_INPUT`, and awaits `_require_input`.

**API** (`api/v1/routers/investigations.py`, additive):

```text
GET  /investigations/{id}                          — response includes `pending_requirement` when AWAITING_INPUT
POST /investigations/{id}/provide-input             — body: {requirement_id, data}; signals provide_input; 409 if not AWAITING_INPUT or requirement_id mismatch
```

**Fail-closed rule:** `provide_input` validates `data` keys against `requirement.fields` before signaling — unknown/missing required keys → `422 INVALID_REQUEST`, never partial-apply. This differs from PRISM's untyped `interrupt()` payload, which accepts whatever the caller sends.

**Reuse, not reinvention:** this rides the exact same transition/outbox/idempotency machinery pause/resume/cancel already use — `AWAITING_INPUT` is a new `InvestigationStatus` value, not a new subsystem.

---

## 3. Gap 2 — Bulk/Batch Multi-Entity Intake & Fan-Out

### 3.1 What prism-agent does

`bulk_graph.py`: parses a CSV, extracts unique account numbers, `Send()`s one sub-graph invocation per account (max parallelism via a semaphore inside the shared ELK/LLM clients), collects completed/failed lists, then runs aggregation.

### 3.2 Why it doesn't transfer as-is

CSV-with-magic-columns (`account_number`, `xm_flg`, `context_narrative`, ...) is prism-agent's domain-specific intake shape. IAP's `POST /investigations` already accepts one investigation per call with a generic `parameters: dict[str, Any]` bag — the missing piece is purely the **fan-out orchestration**, not the parsing.

### 3.3 Generalized design: `BulkIntakeWorkflow` + `BatchIntakeRecord`

**Domain** (new `domain/investigation/batch.py`):

```python
class BatchIntakeRecord(BaseModel):
    """One row of a structured bulk-intake payload — application-agnostic."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    external_key: str = Field(..., max_length=256)   # caller's identifier (account #, ticket #, ...)
    problem_description: str = Field(..., max_length=8000)
    parameters: dict[str, Any] = Field(default_factory=dict, max_length=50)


class BatchIntakeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    application_id: str = Field(..., max_length=128)
    records: list[BatchIntakeRecord] = Field(..., min_length=1, max_length=500)
    priority: str = Field(default="NORMAL", max_length=16)
```

**API** (new router `api/v1/routers/batch.py`):

```text
POST /batch-intake                    — body: BatchIntakeRequest; 202; dispatches BulkIntakeWorkflow
GET  /batch-intake/{batch_id}         — {batch_id, total, completed, failed, investigation_ids: {external_key: uuid}}
```

`records` arrives as parsed JSON, **not raw CSV** — CSV/whatever-format parsing is a client-side or thin adapter-side concern (mirrors PRISM's own `load_csv` being a separate, swappable function from the graph). A companion utility module `application/investigation/batch_parsing.py` provides an optional `parse_csv_records(content: str, mapping: dict[str, str]) -> list[BatchIntakeRecord]` helper (field-name remapping table instead of hardcoded column names) for callers who do want CSV ergonomics, keeping the workflow itself format-agnostic.

**Orchestration** (new Temporal workflow `application/worker/workflows.py::BulkIntakeWorkflow`):

```python
@workflow.defn
class BulkIntakeWorkflow:
    @workflow.run
    async def run(self, request: BatchIntakeRequest, tenant_id: str) -> BatchIntakeResult:
        # Fan out via child workflows (Temporal's Send()-equivalent), bounded by
        # a configurable max-parallel (IAP_BATCH_MAX_PARALLEL, mirrors PRISM's
        # max_parallel_accounts) using asyncio.Semaphore around child-workflow starts.
        handles = []
        sem = asyncio.Semaphore(self._max_parallel)
        async def _start_one(record: BatchIntakeRecord):
            async with sem:
                return await workflow.start_child_workflow(
                    RunInvestigationWorkflow.run,
                    args=[_to_investigation_request(record), tenant_id],
                    id=f"wf-investigation-batch-{self._batch_id}-{record.external_key}",
                )
        handles = await asyncio.gather(*[_start_one(r) for r in request.records])
        results = await asyncio.gather(*[h.result() for h in handles], return_exceptions=True)
        ...
```

Temporal child workflows are the direct analog of LangGraph's `Send()` fan-out — each gets its own history, retry policy, and can be individually queried/cancelled, which is strictly better isolation than PRISM's shared-process asyncio tasks.

**Reuse:** every child is a normal `RunInvestigationWorkflow` — no new investigation execution logic. `BulkIntakeWorkflow` is purely a dispatcher + collector, matching `bulk_graph.py`'s own thin-orchestration role (it embeds `single_graph` as a node rather than reimplementing analysis).

---

## 4. Gap 3 — Cross-Investigation Root-Cause Clustering

### 4.1 What prism-agent does

After a bulk run, `clusterer.py` loads every `issues[]` entry across all account reports, batches them (25 at a time) into an LLM prompt alongside the *existing* cluster taxonomy (retrieved via Chroma semantic search over cluster label+description embeddings, capped at ~25 total clusters), and asks the LLM to assign each issue to an existing cluster or propose a new one. Results are cached (`cluster-taxonomy.json`) so subsequent runs only classify new issues, not the whole history.

### 4.2 Why it doesn't transfer as-is

The clustering *algorithm* (semantic-candidate-retrieval + incremental LLM batch assignment + cache) is entirely domain-agnostic — it operates on `{title, description, category}` triples, which map directly onto IAP's `Finding.title`/`Finding.statement`/`Finding.finding_type`. What's domain-specific is the fixed 6-category taxonomy (`POLICY_GAP`, `PROCESS_GAP`, ...) and the file-based cache — IAP has Postgres and multi-tenancy, which a flat JSON cache violates.

### 4.3 Generalized design: `FindingClusterService` + `finding_clusters` table

**Domain** (new `domain/finding/clustering.py`):

```python
class FindingCluster(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(..., max_length=16)          # "C001", tenant-scoped sequence
    tenant_id: str = Field(..., max_length=128)
    label: str = Field(..., max_length=128)
    description: str = Field(..., max_length=512)
    member_finding_ids: list[UUID] = Field(default_factory=list, max_length=1000)
    created_at: datetime
    updated_at: datetime
```

**Port** (new `ports/persistence/repositories.py` addition):

```python
class FindingClusterRepository(Protocol):
    async def load_taxonomy(self, tenant_id: str) -> list[FindingCluster]: ...
    async def upsert_cluster(self, tenant_id: str, cluster: FindingCluster) -> None: ...
    async def assign_finding(self, tenant_id: str, finding_id: UUID, cluster_id: str) -> None: ...
    async def unassigned_findings(self, tenant_id: str, limit: int = 500) -> list[Finding]: ...
```

SQLAlchemy impl backed by two tables (`finding_clusters`, `finding_cluster_assignments`) — replaces prism-agent's flat `cluster-taxonomy.json` with tenant-scoped, RLS-protected rows (same pattern as every other Part 6+ repository).

**Application service** (new `application/finding/clustering_service.py`):

```python
class FindingClusterService:
    """Generalizes clusterer.py's batch-assign algorithm; category-agnostic —
    operates on Finding.title/statement/finding_type, not a fixed enum."""

    def __init__(self, cluster_repo: FindingClusterRepository, llm_gateway: LLMGateway,
                 semantic_index: ClusterSemanticIndexPort, batch_size: int = 25): ...

    async def run_incremental(self, tenant_id: str) -> ClusteringRunResult:
        unassigned = await self._cluster_repo.unassigned_findings(tenant_id)
        taxonomy = await self._cluster_repo.load_taxonomy(tenant_id)
        for batch in _chunks(unassigned, self._batch_size):
            candidates = await self._semantic_index.top_candidates(taxonomy, batch, k=12)
            assignments = await self._assign_batch(batch, candidates, taxonomy)  # LLM call, JSON schema strict
            for finding, cluster in assignments:
                await self._cluster_repo.assign_finding(tenant_id, finding.id, cluster.id)
```

`ClusterSemanticIndexPort` is a **new, narrow port** (`ports/knowledge/ports.py` addition) — an embedding-similarity lookup over `{cluster_id, label, description}` triples. Default adapter reuses the platform's existing embedding model config (`IAP_KNOWLEDGE_MEM0_EMBEDDER`) rather than introducing ChromaDB/sentence-transformers as a new dependency; pgvector (already present via migration 006) backs it — one more table, no new infra.

**Trigger:** a Temporal workflow `FindingClusteringWorkflow` (cron-scheduled via Temporal's native schedule support — no APScheduler needed) runs `run_incremental` per tenant on a configurable interval (`IAP_CLUSTERING_INTERVAL_HOURS`), plus an on-demand `POST /investigations/clusters/run` API trigger for parity with prism-agent's `/cluster` endpoint.

**Read API** (new `api/v1/routers/clusters.py`):

```text
GET /clusters                — {items: FindingCluster[], total} tenant-scoped
GET /clusters/{cluster_id}   — cluster detail + member findings (paginated)
```

---

## 5. Gap 4 — External Reference-Documentation Semantic Search

### 5.1 What prism-agent does

`kb/vector_store.py` clones two GitHub repos (`xcp-intelligence-hub`, `xto-architecture-briefs`) shallow, indexes every `service-rules.md` and `architecture-brief-*.md` file into ChromaDB (via `sentence-transformers`), and exposes `semantic_kb(issue_text)` for the `kb_search` node to pull the top-N most relevant docs into the LLM prompt. Falls back to filename/keyword scoring when the index isn't built.

### 5.2 Why it doesn't transfer as-is

This is genuinely a **different knowledge store** from what IAP already has: Mem0 and Graphiti (Part 6) index knowledge **derived from investigations** (facts distilled from concluded cases). `kb/vector_store.py` indexes **static reference material that never changes based on investigation outcomes** — architecture docs, runbooks, service-ownership rules. Conflating the two would let stale investigation artifacts pollute reference-doc search and vice versa.

### 5.3 Generalized design: `ReferenceDocumentIndex` (new knowledge sub-layer, sibling to Mem0/Graphiti)

**Port** (new `ports/knowledge/reference_docs.py`):

```python
class ReferenceDocumentPort(Protocol):
    async def search(self, tenant_id: str, query: str, kinds: list[str] | None = None,
                      limit: int = 6) -> list[ReferenceDocumentHit]: ...
    async def index_source(self, source: ReferenceDocumentSource) -> IndexRunSummary: ...
    async def index_status(self, source_id: str) -> IndexStatus: ...
```

```python
class ReferenceDocumentSource(BaseModel):
    """Config-driven, not hardcoded repo names (§ generalizes PRISM's two fixed repos)."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(..., max_length=64)
    kind: str = Field(..., max_length=32)              # free-form label, e.g. "service-rules", "runbook"
    fetch: ReferenceDocumentFetchSpec                   # git | local_path | s3 (pluggable, §5.4)
    include_globs: list[str] = Field(default_factory=list, max_length=20)   # e.g. ["**/service-rules.md"]
```

**Adapter** (`infrastructure/knowledge/reference_docs_pgvector.py`): reuses the same pgvector substrate as Mem0 (migration 006), a distinct table (`reference_document_chunks`) so it can never be conflated with investigation-derived Mem0 rows. Chunking + embedding pipeline structured as three composable stages:

1. **Fetch** (`ReferenceDocumentFetchSpec`, pluggable strategy: `git` clone-shallow reusing `pygit2` — already a platform dependency — instead of prism-agent's raw `subprocess.run(["git", "clone", ...])`; `local_path` for on-disk docs; `s3` for object-storage-hosted docs).
2. **Chunk + embed** (markdown-aware splitter, same embedder config as Mem0).
3. **Upsert** (content-hash keyed, so re-indexing an unchanged file is a no-op — generalizes prism-agent's `fingerprint.json` freshness check into a per-file hash instead of an aggregate count).

**Config** (`ApplicationProfile` extension, additive field on `ObservabilityProfile`'s sibling — new `ReferenceDocsProfile`):

```yaml
reference_docs:
  sources:
    - source_id: service-rules
      kind: service-rules
      fetch: {type: git, repo: "org/internal-docs", ref: main, token_env: IAP_REFDOCS_TOKEN}
      include_globs: ["**/service-rules.md"]
```

**Wired into evidence gateway** as one more evidence-source-like capability: `application/evidence/gateway.py` gains an optional `reference_docs: ReferenceDocumentPort | None` so the reasoning loop can call it exactly like any other evidence provider — same tool-call shape MCP tools already use, so no new agent-facing protocol.

**MCP tool** (new `mcp/tools/reference_docs.py`): `search_reference_docs(query, kinds?)` — mirrors the existing `runtime_evidence`/`trace_investigation` tool registration pattern in `mcp/registry.py`.

**Index build trigger:** async job (Gap 7's generic job tracker), not a blocking CLI step — `POST /reference-docs/{source_id}/reindex`.

---

## 6. Gap 5 — Cross-Investigation Aggregate Reporting

### 6.1 What prism-agent does

`account_aggregate.py` groups all account reports by `aggregation_key` (an LLM-assigned canonical root-cause slug), computing per-group average/max impact score and dominant fix type; `account_report.py` renders a static 3-tab HTML dashboard from that aggregate. Reports are versioned as `{account}.json` (latest) + `history/{account}_{job8}.json` (per-run archive), with delete-all/delete-by-job/delete-by-account endpoints.

### 6.2 Why it doesn't transfer as-is

The aggregation key (`aggregation_key`/`aggregation_label`) is an LLM-invented free-text slug specific to PRISM's prompt. IAP's `Finding` clustering (Gap 3) already produces a **structured, queryable** grouping — aggregate reporting should consume `FindingCluster`, not reinvent grouping logic.

### 6.3 Generalized design: `InvestigationAggregateReport` service + artifact store

**Application service** (new `application/investigation/aggregate_report.py`):

```python
class InvestigationAggregateReportService:
    """Consumes FindingClusterRepository (Gap 3) — no separate grouping logic."""

    async def build(self, tenant_id: str, application_id: str | None = None) -> AggregateReport:
        clusters = await self._cluster_repo.load_taxonomy(tenant_id)
        rows = [
            AggregateReportRow(
                cluster_id=c.id, label=c.label,
                investigation_count=len(c.member_finding_ids),  # via finding->investigation join
                avg_severity=..., dominant_finding_type=...,
            )
            for c in clusters
        ]
        return AggregateReport(tenant_id=tenant_id, generated_at=now, rows=rows)
```

**Rendering:** a pluggable `ReportRendererPort` (`ports/reasoning/report_renderer.py`) with one default `HtmlAggregateReportRenderer` adapter — Jinja2 template (already implicitly patterned by prism-agent's f-string HTML; Jinja2 makes it maintainable and is a light, well-known dependency) producing the same "grouped table" dashboard shape, generalized to arbitrary cluster labels instead of PRISM's fixed columns.

**Artifact storage:** reuses the platform's existing `ObjectStorageConfig`/S3-compatible storage (already wired in `bootstrap/__init__.py` for evidence payloads) instead of prism-agent's local-filesystem-first design — `latest/{tenant_id}/aggregate-report.html` + `history/{tenant_id}/{run_id}/aggregate-report.html`, mirroring prism-agent's latest+history convention but on durable, multi-replica-safe storage instead of local disk.

**API** (new `api/v1/routers/aggregate_reports.py`):

```text
POST /aggregate-reports/generate      — 202, triggers build+render as an async job (Gap 7)
GET  /aggregate-reports/latest         — redirect/proxy to latest rendered HTML
GET  /aggregate-reports/history        — list of past runs
DELETE /aggregate-reports              — tenant-scoped reset (mirrors PRISM's delete-all, tenant-bounded here — never a global wipe)
```

---

## 7. Gap 6 — Conversational Investigation Chat

### 7.1 What prism-agent does

`api/routes/chat.py`: a session store keyed by UUID, SSE-streamed LLM responses, a context-builder that flattens transcript fields into the prompt, and a magic-keyword trigger (`"generate report"`) that switches from conversation to running the full analysis graph.

### 7.2 Why it doesn't transfer as-is

The context-builder is transcript-field-specific. The magic-keyword trigger is a fragile pattern (string-matching user text) that doesn't belong in a platform meant to serve many application domains with different trigger vocabularies.

### 7.3 Generalized design: `InvestigationChatSession` + explicit action affordance (not keyword-sniffing)

**Domain** (new `domain/investigation/chat.py`):

```python
class ChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True)
    role: str = Field(..., max_length=16)        # "user" | "assistant"
    content: str = Field(..., max_length=8000)
    created_at: datetime


class InvestigationChatSession(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID = Field(default_factory=uuid4)
    tenant_id: str
    investigation_id: UUID | None          # None = pre-investigation exploratory chat
    messages: list[ChatMessage] = Field(default_factory=list, max_length=200)
```

**Context builder is pluggable, not hardcoded:** `ChatContextBuilderPort` resolves `investigation_id` (when present) into a context string via the *existing* `KnowledgeRetrievalPort.retrieve_for_reasoning` (Part 6) — so chat automatically sees the same investigation knowledge context the reasoning loop does, instead of a bespoke transcript-flattener.

**Explicit action, not keyword sniffing:** rather than string-matching `"generate report"`, the chat response schema includes a structured `suggested_actions: list[ChatAction]` field (LLM structured output, same `response_schema` mechanism IAP's reasoning loop already uses for tool-call decisions — F-027). The UI/caller renders these as buttons; invoking one calls the real endpoint (`POST /investigations` or `POST /investigations/{id}/start`) directly. This removes an entire class of "LLM said something that happened to contain report-trigger words mid-sentence" false positives.

**API** (new `api/v1/routers/chat.py`):

```text
POST /chat/sessions                          — {investigation_id?} -> session_id + opening message
POST /chat/sessions/{id}/messages            — SSE stream (reuses LLMGateway.complete streaming support — extend
                                                 LLMGatewayResponse/port with an optional stream() method, additive)
GET  /chat/sessions/{id}                      — history
```

**Persistence:** `chat_sessions`/`chat_messages` tables, tenant-scoped RLS — same repository pattern as everything else, not prism-agent's in-process dict (which loses history on restart).

---

## 8. Gap 7 — Generic Async Job/Progress Tracking

### 8.1 What prism-agent does

`api/jobs.py`: an in-process `JobQueue` (dict of `Job` dataclasses) with polling (`GET /status/{job_id}`) and WebSocket push (`/status/ws/{job_id}`), used for bulk analysis progress, KB index builds, and clustering runs — anything that isn't itself a first-class domain aggregate.

### 8.2 Why it doesn't transfer as-is

In-process-only state is lost on restart and doesn't work across API replicas. IAP already solved this exact problem for Investigations via Temporal + the outbox pattern — Gap 7 is about extending that solved pattern to **non-Investigation** background work (KB reindex, clustering runs, aggregate report generation) that doesn't deserve full `Investigation` aggregate status but still needs "is it done yet, and here's the live progress" semantics.

### 8.3 Generalized design: `BackgroundJob` — thin, generic, Temporal-backed

**Domain** (new `domain/common/background_job.py`):

```python
class BackgroundJobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class BackgroundJob(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: UUID = Field(default_factory=uuid4)
    tenant_id: str
    kind: str = Field(..., max_length=64)     # "reference_docs_reindex" | "finding_clustering" | "aggregate_report"
    status: BackgroundJobStatus = BackgroundJobStatus.PENDING
    progress: int = 0
    total: int = 0
    result_ref: str | None = None             # opaque pointer (object storage key, cluster run id, ...)
    error: str | None = None
    stages: list[dict] = Field(default_factory=list, max_length=50)
```

**Backing:** every `BackgroundJob` **is** a Temporal workflow execution — `workflow_id = f"job-{kind}-{job_id}"`. No separate in-process queue; `GET /jobs/{id}` reads the `background_jobs` Postgres row (updated via the same activity-heartbeat/outbox convention `RunInvestigationWorkflow` already uses), and streaming progress reuses the platform's existing Kafka event-publisher (`ctx.event_publisher`) — a WebSocket gateway subscribes to the tenant's job-progress topic instead of maintaining its own subscriber-queue-per-job dict (which doesn't survive a restart or scale past one replica).

**API** (new `api/v1/routers/jobs.py`):

```text
GET /jobs/{id}                — poll
GET /jobs?kind=&status=        — list, tenant-scoped
WS  /jobs/{id}/stream          — live progress (subscribes to Kafka topic `iap-jobs.{tenant_id}`)
```

Gaps 4 (reindex), 3 (clustering run), and 5 (aggregate report generation) all become `BackgroundJob` producers — one generic subsystem, three call sites, zero bespoke tracking code per feature (directly fixing prism-agent's pattern of `jobs.py` being reinvented ad hoc alongside each async feature).

---

## 9. Gap 8 — Downstream Service Credential Provider

### 9.1 What prism-agent does

`tool_registry.py::_get_sat_token`: OAuth2 client-credentials grant against Comcast's internal SAT (Service Auth Token) endpoint, with an internal-library fast path (`tpx_sat_gen`) and a direct-HTTP fallback. Tokens are fetched per-connection and cached at the shared-MCP-client level (`fetch_elk.py`'s `_ELK_TOOLS_CACHE`), with reconnect-on-drop handling.

### 9.2 Why it doesn't transfer as-is

`tpx_sat_gen` and the CodeBig2 SAT endpoint are Comcast-internal — not portable. What generalizes is the **shape**: "some evidence/tool providers need a platform-managed, auto-refreshing outbound bearer token, distinct from the inbound JWT that authenticates the calling agent."

### 9.3 Generalized design: `OutboundCredentialProvider` port

**Port** (new `ports/security/outbound_credentials.py`):

```python
class OutboundCredentialProvider(Protocol):
    async def get_token(self, provider_id: str) -> str:
        """Return a valid bearer token, refreshing if expired. Never blocks
        on a full re-auth if a cached token still has >60s validity."""
        ...
```

**Adapters:**
- `OAuth2ClientCredentialsProvider` (generic RFC 6749 client-credentials grant — config-driven token endpoint/client-id/secret/scope, covering PRISM's `_sat_via_oauth` fallback path generically, no Comcast-specific library dependency).
- `StaticTokenProvider` (env-var token, dev/test).
- Extension point documented for organizations with an internal SAT-style library, following the same "optional import, graceful fallback" pattern PRISM itself uses (`try: from tpx_sat_gen...; except ImportError: fallback`).

**Caching/refresh:** single `asyncio.Lock`-guarded cache per `provider_id`, expiry tracked from the token response's `expires_in`, proactive refresh at 80% of TTL — directly generalizing PRISM's double-checked-locking `_get_shared_elk_tools` pattern (which conflated token caching with MCP-client caching; this design separates them: the *token* cache lives here, the *MCP client connection* cache is Gap unrelated-but-adjacent, see §9.4).

**Wiring:** `EvidenceConfig` gains `outbound_credentials: dict[str, OutboundCredentialSpec]` (provider_id → grant config); `bootstrap/__init__.py`'s evidence-gateway wiring resolves a provider's declared `credential_provider_id` (new optional field on the MCP evidence adapter config) through this port before opening the outbound connection — same "config declares the dependency, bootstrap wires the adapter" convention as everything else in `_build_production_context`.

### 9.4 Related, smaller finding: shared outbound MCP-connection caching

PRISM's `fetch_elk.py` double-checked-locking connection cache (one SSE connection reused across all accounts, with drop-detection + single-retry reconnect) is a reusable *connection pooling* pattern independent of credentials. Since IAP's `McpEvidenceAdapter` (Part 9) already exists, this is noted as a **follow-up hardening item** for that adapter (reuse connections across requests within a process) rather than a new component — tracked in the plan doc as an optional Part 11 phase, not a new port.

---

## 10. Gap 9 — Resilient LLM JSON Extraction

### 10.1 What prism-agent does

`llm_analysis.py::_parse_llm_json`: strips markdown code fences, tries direct `json.loads`, falls back to finding the outermost `{...}` block (trailing prose after JSON), and as a last resort counts unclosed braces/brackets/open-strings and synthesizes the closing characters to recover a **truncated** response (e.g. hit `max_tokens` mid-object).

### 10.2 Why it doesn't transfer as-is

Nothing domain-specific here — this is pure defense-in-depth against imperfect LLM output. IAP's current posture (F-027: strict `response_format: json_schema`) is the **better primary mechanism** where the provider supports it, but not every provider/model does (the factory's `MODEL_REGISTRY` already spans OpenAI/Anthropic/Azure — Anthropic's Claude models via the `AnthropicGateway` don't have OpenAI-style `json_schema` strict mode), and even strict-schema providers can still truncate on `max_tokens`.

### 10.3 Generalized design: `resilient_json_extract` utility in the reasoning layer

**New module** (`infrastructure/reasoning/json_extraction.py`, pure functions, no I/O):

```python
def resilient_json_extract(content: str) -> dict[str, Any]:
    """Best-effort JSON recovery: fence-strip -> direct parse -> outermost-
    object scan -> brace/bracket-balance repair for truncation. Raises
    JSONExtractionError (never a bare ValueError) with the original content
    truncated to 200 chars for logging — mirrors PRISM's failure-diagnostics
    posture, generalized as a typed platform exception."""
```

**Wiring:** `AnthropicGateway`/`OpenAIGateway`'s non-structured-output code path (i.e., when `request.response_schema` is `None`, or as a fallback when a strict-schema call still returns unparseable content — logged distinctly from the primary strict-mode success path so the two failure modes stay distinguishable in telemetry) calls this instead of a bare `json.loads`. This is a hardening addition to existing adapters, not a new adapter — smallest possible surface area for a defense-in-depth utility.

**Explicitly not a replacement for F-027:** the design doc is explicit that `response_format: json_schema` remains the primary, preferred mechanism; this utility is the fallback for providers/situations where that mechanism isn't available or still degrades, mirroring exactly how prism-agent itself treats it (a recovery path, not the primary contract).

---

## 11. Explicitly Deferred (reviewed, not missing — call these out so they aren't silently dropped)

| Item | prism-agent evidence | Why deferred |
|---|---|---|
| Bundled static web UI | `ui/index.html` (single-file SPA) | IAP is API-first by design (F-065/F-066 posture); a bundled operator UI is a legitimate future ask but is a frontend project, not a backend platform gap — tracked as a candidate Part 12+ if requested, not designed here |
| APScheduler weekly cron | `runner.py::schedule_weekly` | Temporal's native `Schedule` API (already a platform dependency) strictly subsumes this — no gap, just "use what's already there" when Gap 2/3 workflows need periodic triggers |
| Rate-limit-aware LLM retry backoff | `llm_analysis.py::_call_llm` retry loop | IAP's `openai_adapter.py`/`anthropic_adapter.py` already implement tenacity-based exponential backoff on 429/503 (confirmed in Part 9 review) — functionally equivalent, no gap |

---

## 12. Cross-Cutting Design Principles Applied

Every generalized design above follows conventions already established in Parts 1–10, not new house style:

- **Tenant-scoped, RLS-protected persistence** for every new table (clusters, chat sessions, background jobs, reference-doc chunks) — never prism-agent's flat JSON files or in-process dicts.
- **Ports before adapters** — every new capability gets a `Protocol` in `ports/` before any concrete implementation, matching the evidence-gateway/knowledge-layer precedent.
- **Fail-closed on ambiguity** — `InputRequirement` validates against its own declared schema; unknown cluster-assignment LLM output falls to an explicit `UNASSIGNED` bucket (not silently dropped); credential provider never returns an expired token.
- **Reuse existing infra over new dependencies** — pgvector (not ChromaDB) for both reference-doc and cluster-semantic search, `pygit2` (not raw `subprocess git clone`) for doc-source fetching, Temporal Schedules (not APScheduler) for periodic triggers, Kafka (not a bespoke pub/sub) for job-progress streaming, S3-compatible storage (not local disk) for report artifacts.
- **Additive-only where domain models already exist** — `InvestigationStatus.AWAITING_INPUT`, `ObservabilityProfile`-sibling `ReferenceDocsProfile`, are new enum members/sibling models, never breaking changes to `Investigation`/`Finding`/`ApplicationProfile`.
