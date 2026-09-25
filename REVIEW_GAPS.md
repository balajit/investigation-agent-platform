# REVIEW_GAPS.md

Code review of `src/` against the `docs/` specification set
(`HLD.md`, `IAP-implementation-part1..part4`).

- **Review scope**: full `src/investigation_agent_platform` tree vs. the 4-part IAP architectural specification and HLD.
- **Method**: diff is unavailable (no commits — `git log` empty, all files untracked), so every source module was read in full and compared against the documented contracts. Runtime assertions were executed with `uv run python` to verify suspected bugs; exact repro output is quoted under each critical finding.
- **Tooling results captured**:
  - `uv run ruff check src/` → **303 errors** (255 auto-fixable; includes `F811` redefined-while-unused ×5, `PIE794` duplicate-class-field-definition, `I001` unsorted imports ×31, `F401` unused imports ×21).
  - `uv run mypy src/investigation_agent_platform` → **errors prevented further checking**: `Source file found twice under different module names` (caused by mixed `src.`-prefixed and bare import styles).
  - `uv run pytest` → **no tests ran** (test directories are empty).

---

## Severity legend

| Level | Meaning |
|---|---|
| **BLOCKER** | Module fails to import or crashes deterministically on a documented code path; product cannot run. |
| **HIGH** | Silent correctness/security defect; works by accident or mis-scopes data. |
| **MEDIUM** | Deviation from the documented contract that will produce wrong behavior once exercised. |
| **LOW** | Convention/structure/quality gaps; no immediate runtime impact. |

---

## Summary

| Severity | Count | Bucket |
|---|---|---|
| BLOCKER | 0 open (7 resolved) | See "Resolution log" below |
| HIGH | 0 open (6 resolved) | See "Resolution log" below |
| MEDIUM | 0 open (5 resolved) | See "Resolution log" below |
| LOW | 8 | Import-style inconsistency, empty top-level dirs, no tests, no migrations, missing deps, obsolete ADR dir, lint debt |

> Last updated after Blockers + HIGH + MEDIUM remediation (see Resolution log).

---

## BLOCKER findings

### B1. `services.py` cannot be imported — nonexistent `ports.observability` module
`application/investigation/services.py:36`
```python
from src.investigation_agent_platform.ports.observability.telemetry import ObservabilityPort
```
There is no `ports/observability` package (ports/ contains `correlation, evidence, persistence, profile, protocols.py, reasoning, security`). `ObservabilityPort` is actually declared in `infrastructure/observability/telemetry.py`, whose own header claims `# src/investigation_agent_platform/ports/observability/telemetry.py` — i.e. the file is in the wrong layer and referenced from a path that does not exist.

Verified:
```
FAIL investigation_agent_platform.application.investigation.services: ModuleNotFoundError: No module named 'src.investigation_agent_platform.ports.observability'
```
Doc ref: Part 1 §4.2 (`ObservabilityPort` outbound port), Part 1 §11.1 (port placement).

### B2. `EvidencePage` is imported but never defined in the domain layer
`domain/evidence/models.py` has no `EvidencePage` (it exists only in the Part 3 doc §4.1). Three modules import it and fail at import time:
- `ports/evidence/gateway.py:3`
- `application/evidence/gateway.py:6`
- `infrastructure/evidence/runtime/elastic.py:8`

Verified:
```
ImportError: cannot import name 'EvidencePage' from 'investigation_agent_platform.domain.evidence.models'
```
Doc ref: Part 3 §4.1 (`EvidencePage` schema). This also breaks the documented `EvidenceGatewayProtocol` return type.

### B3. Redactor crashes — `Evidence` has no `content` field
`infrastructure/evidence/security.py:32` reads `dict(evidence.content)` and `:55` sets `"content": ...`. The Part 3 `Evidence` schema (with `content`, `provenance`, `freshness`, `is_redacted`) was never adopted in `domain/evidence/models.py`, which uses the Part 1 schema (`id`, `content_uri`, `attributes`, `redaction_manifest`).

Verified:
```
redact FAILED: AttributeError 'Evidence' object has no attribute 'content'
```
Impact: the mandatory ingress sanitization path (Part 1 §5.2 Rule 8, Part 4 §8.1 / invariant 2) cannot run at all.

### B4. Adapters reference non-existent `EvidenceType` members → AttributeError
The `EvidenceType` enum in `domain/evidence/models.py:20` has only `LOG, TRACE, DATABASE_STATE, SOURCE_CODE, METRIC, DOCUMENTATION, SYSTEM_EVENT`. The Part 3 adapters use members that don't exist:
- `infrastructure/evidence/runtime/elastic.py:111` → `EvidenceType.RUNTIME_LOG`
- `infrastructure/evidence/mcp.py:80` → `EvidenceType.RUNTIME_LOG`
- `infrastructure/evidence/code/git.py:128` → `EvidenceType.COMMIT_HISTORY`

Verified (both raise):
```
AttributeError: type object 'EvidenceType' has no attribute 'RUNTIME_LOG'
AttributeError: type object 'EvidenceType' has no attribute 'COMMIT_HISTORY'
```
Doc ref: Part 3 §4.1 (the docs' `EvidenceType` includes `RUNTIME_LOG`/`RUNTIME_TRACE`/`COMMIT_HISTORY`/`CALL_GRAPH`/`EXCEPTION_PATH`); the repo merged Part 1 and Part 3 schemas into one file that matches neither.

### B5. Evidence construction silently discards content and provenance
All four adapters build domain `Evidence` with Part 3 field names that the Part 1 model ignores by default (`extra` is unset → ignored):
- `elastic.py:108-124`, `oracle.py:106-122`, `git.py:76-92`, `mcp.py:78-94` pass `evidence_id=`, `content=`, `provenance=`, `freshness=`, `is_redacted=`/`content=` etc.

Verified:
```
Evidence constructed OK. id= c391ddb7-... has content attr: False has provenance: False
```
The model generates a fresh UUID `id`, and the actual content/provenance are dropped. This means
- `Evidence.id` never equals the ID recorded in relationships/persistence (P3 engine hydrates by those IDs),
- raw evidence content is lost before it reaches the store/LLM context, and
- `redaction_manifest`/`fingerprint` fields (used for dedup, Part 2 §9) carry adapter-derived values keyed on dropped IDs.

### B6. Duplicate class definitions produce a fractured type system
`domain/investigation/models.py` defines **`InvestigationAction` twice**:
- `:101` — `capability_name`/`force_refresh` version
- `:184` — `action_type`/`execution_hash` version (wins the namespace)

and `domain/common/exceptions.py` defines **`DomainException` twice** (`:12` and `:141`, different signatures).

Verified:
- `guardrails.py:87` `compute_fingerprint` calls `action.capability_name`:
```
compute_fingerprint FAILED: 'InvestigationAction' object has no attribute 'capability_name'
```
- The winning `InvestigationAction` requires `execution_hash` with no default, but `validator.py`/planner code constructs/treats actions as having neither `execution_hash` nor `capability_name` — split-brain.
- `InvalidLifecycleTransitionException` subclasses the *first* (shadowed) `DomainException`, so:
```
InvalidLifecycleTransitionException is instance of module DomainException: False
```
Meaning `except DomainException:` in application code (e.g. `services.py`, `CancelInvestigationService`) will **not** catch transition violations, and `InvalidLifecycleTransitionException` is neither a module-level `DomainException` nor an `IAPError`. Two independent exception trees exist.

`Fact` also redefines `statement` and adds a required `source_evidence_id` (in addition to `source_evidence_ids`) — see F7. `ruff` flags `F811` ×5 and `PIE794 duplicate-class-field-definition` for exactly these.

### B7. Patch-traversal guard in `validator.py` bypassable via earlier check order + root prefix
`application/investigation/validator.py:61-75` catches `..` in `file_path`, but the check happens *after* `params` are evaluated and the GET_CODE guard only inspects `file_path`. The `..` containment test is a substring match on the raw parameter and is applied before the root check, so a value produced by the second `InvestigationAction` (e.g. parameters arriving through a different key like `repository`/`revision` that later become path fragments in `PyGit2Adapter.get_source` at `git.py:40-43`) is not validated. More concretely, `git.py` composes `repo_path = f"{repo_base}/{request.repository}"` and `commit.tree[request.file_path]`; nothing in the adapter re-validates either field against `source_roots`/allowed repositories. Security posture relies entirely on the validator that operates on a different `InvestigationAction` shape (B6). Flagged as relying on runtime behaviour that currently cannot execute (the validator consumes `action.action_type`, which the winning `InvestigationAction` provides, but callers such as `services.py` are currently unimportable), so the net effect cannot be observed end-to-end — treat as latent.

---

## HIGH findings

### H1. `PostgresRelationshipRepository` queries a table/columns that do not exist
`infrastructure/correlation/repository.py:35` queries table `investigation_relationship` (never defined) with `source_evidence_id`/`target_evidence_id` columns and an `application_id` filter, while the ORM `EvidenceRelationshipORM` (`infrastructure/persistence/models.py:122-145`) defines table `evidence_relationships` with `tenant_id, investigation_id, source_node_id, target_node_id, relationship_type, confidence, reason, provenance_evidence_id` — no `application_id`, no `source_evidence_id`.
Additionally the error handler (`:79-81`) does `return []` on any exception, **swallowing** the schema failure and silently returning an empty graph. The correlation feature (HLD §4, Part 1 §3.2) therefore never returns edges in practice.
Also: pass `root_ids` as `List[str]` against UUID columns.

### H2. `scripts/setup.py` `rollback()` is not a method
`scripts/setup.py:65` defines `def rollback():` (no `self`) but `initialize()` calls `self.rollback()` at `:245` inside the failure handler, and the body references `self`/`self.log_event`. Any init failure raises `TypeError` (unbound method called with an argument) inside the `except`, escaping instead of performing rollback — worse than silent: it masks the original error.

### H3. Tenant isolation is not applied to the correlation path
`ports/correlation/engine.py` `correlate(seed_identifiers, profile)` and `EphemeralCorrelationEngine` receive tenant only for logging; `infrastructure/correlation/repository.py` filters relationships by `tenant_id` but the ORM table (`evidence_relationships`) provides `tenant_id` only indirectly; evidence fetch in `application/correlation/engine.py:65` calls `self._evidence_repo.get_by_ids(discovered_ids)` with **no tenant binding**. Multi-tenant triple isolation (Part 3 §1 constraint 6) is not enforced at the storage layer — a tenant-1 agent traversing tenant-2 evidence IDs would hydrate tenant-2 payloads. RLS (Part 4 §8.3) is claimed by the docs but absent in code/README/config.

### H4. Optimistic concurrency control contract unimplemented
`InvestigationRepository.save(..., expected_version)` (Part 1 §4.2, §9.2) has no recorded implementation — the only save path is `services.py:190`. No repository implementation files exist anywhere (see M2). `Investigation.transition_to` bumps `version` locally but nothing validates `V_db`; the OCC formula is documented but dead.

### H5. `InvestigationContext.time_window` constructed as zero-width window
`services.py:93-96` sets `time_window = (now, now)` at creation. The HLD/Part 1 `InvestigationContext` requires a real window from the profile (`investigation.default_time_window`); runtime evidence queries built from this context (`elastic.py:56-63`) will effectively query a zero-second range and return nothing. `CreateInvestigationService` also never persists the request's `requested_time`/parameters into the window.

### H6. `SensitiveDataRedactor` writes manifest entries but redacts content that is discarded
Even after B3 is fixed, `redact_evidence` redacts only string values in `evidence.content`; adapter content is a `dict` of JSON, nested fields (`log.level`, `labels`, embedded JSON arrays) are never recursed, and because content is dropped (B5) the manifest references content that was never stored. Doc expects Presidio-level PII/secret scrubbing at ingress (Part 3 §8), while the implementation is a 3-pattern regex that misses bearer tokens/JWTs/URIs and `postgres://user:pass@host` connection strings enumerated in Part 1 §8.1.

---

## MEDIUM findings

### M1. Persistence schema is only a fraction of the documented 15 tables
`infrastructure/persistence/models.py` implements `investigations`, `investigation_checkpoints`, `evidence`, `evidence_relationships` (8 ORM classes). Missing vs Part 1 §9.1 / Part 4 §5.1:
`INVESTIGATION_TRANSITION`, `INVESTIGATION_FACT`, `INVESTIGATION_ENTITY`, `INVESTIGATION_ACTION`, `INVESTIGATION_TOOL_EXECUTION`, `HYPOTHESIS`, `HYPOTHESIS_EVIDENCE`, `FINDING`, `INVESTIGATION_CONCLUSION`, `TIMELINE_EVENT`, `EVIDENCE_REFERENCE`, `EVIDENCE_ATTRIBUTE`, `APPLICATION_PROFILE`, `APPLICATION_PROFILE_VERSION`, `AGENT_ACTIONS`.
Correspondingly there are **no repository implementations** for `InvestigationRepository`, `EvidenceRepository`, `HypothesisRepository`, `TimelineRepository`, `ApplicationProfileRepository` (only `ports/persistence/repositories.py` protocols). Every application service is therefore unimplementable against real storage.

### M2. `EvidenceIngressPipeline` depends on undeclared package `tenacity`
`infrastructure/sanitization/ingress.py:11` imports `tenacity` (retry). `pyproject.toml` dependencies do not include it. In a fresh environment `uv sync` installs nothing that provides it:
```
FAIL investigation_agent_platform.infrastructure.sanitization.ingress: ModuleNotFoundError: No module named 'tenacity'
```

### M3. Config/profile drift from documented specs
- `config/application.yaml` (12 lines) is missing `persistence.connection_pool_size`, `reasoning.provider/model`, `observability.tracing_enabled/otlp_endpoint` from Part 1 §10.1.
- `config/logging.yaml` (§10.2) does not exist.
- `config/profiles/example.yaml` and `payments.yaml` (§10.3/10.4) do not exist — `config/profiles/` is empty.
- `config/application.yaml` wouldn't even be read by anything: no YAML loader exists; `infrastructure/configuration/config.py` reads env vars only and requires `IAP_DATABASE_URI` + `IAP_LLM_API_KEY` at boot, contradicting the declared config-file based approach.

### M4. No messaging implementation
`infrastructure/messaging/__init__.py` is an empty `"""Package initialization."""`. Part 4 §6.1/§6.4 requires a Kafka FastStream event backbone with `investigation.events` subscriber and `InvestigationCancelled` publishing; nothing emits or consumes events. Domain events exist (`domain/events/base.py`) but no publisher exists for them.

### M5. Elasticsearch adapter pagination cursor is not a paging token
`elastic.py:80` sets `next_cursor = hits[-1]["_id"]` and `has_more = len(hits) == limit`, but the request/query never uses a cursor for subsequent pages (the `search` call has no `search_after`/`from` parameter). The `EvidenceGateway` returns `next_cursor`/`has_more` that cannot be honoured — callers would loop with the same page. Also `total_count` from `response["hits"]["total"]["value"]` is optional in newer ES responses.

---

## LOW findings

### L1. Import-style inconsistency breaks mypy and is fragile at runtime
Two styles coexist:
- `src.`-prefixed: `domain/investigation/models.py:10`, `application/investigation/services.py`, `domain/common/exceptions.py:1-9`, `domain/evidence/providers.py`, `ports/persistence/repositories.py`, `ports/correlation/engine.py`, `ports/reasoning/reasoner.py`, `infrastructure/persistence/models.py`
- bare: `application/investigation/guardrails.py`, `application/evidence/gateway.py`, `infrastructure/evidence/*`, `ports/evidence/gateway.py`, `infrastructure/configuration/config.py`, etc.

Verified mypy:
```
Source file found twice under different module names: "investigation_agent_platform.application.investigation.validator" and "src.investigation_agent_platform.application.investigation.validator"
```
Runtime only works by accident: `src` resolves as a namespace package when cwd == repo root. As an installed wheel (`hatchling` ships `src/investigation_agent_platform` only), every `src.`-prefixed import breaks.

### L2. `domain/evidence/providers.py` is misplaced and mislabeled
The file's header says `# src/investigation_agent_platform/ports/evidence/providers.py` but it lives in `domain/evidence/`. It declares the provider **ports** (`RuntimeEvidenceProvider`, `StateEvidenceProvider`, `CodeEvidenceProvider`, `EvidenceStorePort`) inside the domain layer. Part 1 §3/§11.1 and Part 3 require these ports in `ports/evidence/`; the domain layer must contain zero outbound port dependencies. The dedicated `ports/evidence/` package contains only `gateway.py` (no `runtime.py`/`state.py`/`code.py` per Part 3 §3).

### L3. `ports/protocols.py` is empty; several documented ports absent
- `ports/protocols.py` (0 lines), `ports/profile/__init__.py` (1 line).
- Missing per Part 1 §4.1: `InvestigationTriggerPort`.
- Missing per Part 3 §3/§7: `RuntimeEvidenceProviderProtocol`/`StateEvidenceProviderProtocol`/`CodeEvidenceProviderProtocol`, `EvidenceProviderRegistry`, `EvidenceGatewayHealthService`, `EvidenceDeduplicator`, `EvidenceMerger`, `EvidenceNormalizer` (+runtime/db normalizers), cache (`cashews`/redis), rate limiter (`limits`), provider circuit breakers, `PostgresRelationshipRepository` uses raw SQL not the documented ORM path.

### L4. No tests
`tests/` contains only empty `fixtures/`, `integration/`, `unit/application`, `unit/domain` directories. The docs mandate: `test_investigation_lifecycle.py`, `test_hypothesis.py`, `test_evidence.py`, `test_investigation_limits.py` (Part 1 §12.1), the Part 2 §13.4 simulation suite (`simple-timeout`, `missing-log-error`, `contradictory-evidence`, `provider-failure`, `budget-exhaustion`, `resume-execution`, `agent-safety-boundary`), and Part 4 §11 integration (testcontainers) / replay harness.

### L5. Deployment, Docker, migrations, CI scaffolds are empty
- `deployment/k8s/`, `docker/`, `migrations/` contain **zero files**.
- No `Dockerfile.api/worker/frontend`, no `docker-compose.yml`, no `alembic.ini`/`env.py`, no migration versions (Part 4 §3, §5.3).
- No `.importlinter` config (Part 3 §7.1 architecture enforcement). No ruff/mypy CI wiring.

### L6. Missing third-party dependencies required by the docs
`pyproject.toml` lacks: `presidio-analyzer`/`presidio-anonymizer` (Part 1 §1), `temporalio` (Part 1 §1, Part 2, Part 4), `faststream`/`kafka` (Part 4 §6.1), `pingtree-sitter`/`tree_sitter_languages` (imported lazily in `parser.py`), `mcp` (lazy import), `tenacity` (H/M2), `fsspec`/`aioboto3`/`minio` (Tier-2 store, Part 1 §1), `instructor`/`pydantic-ai` (structured outputs, Part 2 §1), `sentence-transformers`/`fastembed` (dedup, Part 2 §7), `structlog` is present but `openinference`/`arize-phoenix`/`langsmith` absent (Part 2 §13). The only implemented message transport is none.

### L7. `domain/investigation/budget.py` vs `InvestigationLimits`/`BudgetPolicy` inconsistency
Three budget/limit models exist with different defaults and no shared enforcement: `InvestigationLimits` (domain, max_evidence_items=25), `BudgetPolicy` (guardrails.py, max_tool_calls=50/max_evidence_items removed), `InvestigationBudget` (budget.py, token/cost fields). Part 4 §6.3 hard budget (K=15 / 30,000 tokens / $10) is defined only in `InvestigationBudget`, but the enforcement loop (LoopGuard `BudgetPolicy` + `InvestigationActionValidator` `InvestigationLimits`) reads different ones, so documented ceilings are not consistently applied. `InvestigationBudget.is_exceeded` never compares `current_tokens_used >= max_prompt_tokens_per_turn` per-turn (only cumulative) — the per-turn cap is unimplemented.

### L8. Minor domain-model issues
- `Fact` (`domain/investigation/models.py:108-125`) duplicates `statement` field and adds required `source_evidence_id` unrelated to the documented `source_evidence_ids` list plus a leftover `confidence`/`established_at` set that is never used consistently. Constructing a documented Fact fails:
  ```
  ValidationError: source_evidence_id Field required
  ```
- `InvestigationMemory.trim_evidence_context` (`memory.py:32-34`) returns the *last* `max_items` (`[-max_items:]`) rather than top-K by relevance, contradicting Part 2 §6.1/§11.1.
- `main.py` is still the scaffold stub (`print("Hello from investigation-agent-platform!")`); no FastAPI app, no health endpoints, no Temporal worker entrypoint (Part 4 §3/§4/§9.4).
- `docs/architecture/adr/` is an empty directory — all 4 ADRs (Part 1 §12.2) are specified but not recorded.

---

## Doc-vs-code feature completion matrix

| Documented capability | Doc ref | Status in code |
|---|---|---|
| Hexagonal protocols (evidence/persistence/reasoning/correlation) | P1 §4 | Partial — `InvestigationTriggerPort`, `ObservabilityPort` misplaced/missing; `ports/protocols.py` empty |
| Domain model parity (Pydantic v2, Part-1 schemas) | P1 §3 | Mostly present; broken by duplicate classes (B6) and mixed schemas (B4/B5) |
| Evidence graph / correlation engine | P1 §3.3, P3 §6 | Present-but-inert: DB repo queries missing table (H1), evidence IDs dropped (B5) |
| Investigation lifecycle state machine | P1 §6, P4 §2 | Domain transitions implemented; no orchestrator/Temporal engine to drive them |
| Temporal Workflows/Activities | P1 §6, P2 §3, P4 §2 | **Absent** |
| Adaptive planner / step graph | P2 §5 | Models only in `planner.py`; no planner logic |
| Agent reasoning loop (LLM) | P2 §4/§6 | Models only (`reasoning.py`); no LLM invocation, no port impl |
| Hypothesis mgmt / dedup / contradiction | P2 §7 | Model stubs (`verification.py`); no manager/evaluator/deduplicator logic |
| Verification & conclusion policy | P2 §10, P4 §14 | Model stubs; scoring formula present in `InvestigationConfidence` |
| Loop guard / fingerprinting | P1 §6.2, P2 §9 | `guardrails.py` exists but fingerprint crashes on `capability_name` (B6) |
| Evidence Gateway (`AsyncEvidenceGateway`) | P3 §5.1 | Implementation present but **unimportable** (B2) |
| Elastic / Oracle / Git / MCP adapters | P3 §5.2-5.6 | Present but crash on missing `EvidenceType`/`EvidencePage` and drop content (B2/B4/B5/B3) |
| MCP-as-adapter architecture | P1 §4, ADR-004 | `mcp.py` adapter present; not wired into gateway/provider registry |
| Two-tier memory / S3 offload | P1 §7, P4 §5 | `EvidenceIngressPipeline` present (broken dep, M2); no S3 client or `EvidenceStore` impl |
| Ingress sanitization (Presidio) | P1 §8, P4 §8.1 | Regex redactor present but crashes (B3); Presidio not a dependency |
| App profiles (example/payments) | P1 §10.3/10.4 | Model present; **no profile YAML files** |
| Persistence (15 tables, OCC, RLS) | P1 §9, P4 §5 | 8 ORM tables; no repos; no RLS; OCC unimplemented (M1, H4) |
| API/FastAPI + health | P4 §4/§9.4 | **Absent** (no `api/` package) |
| Eventing (FastStream/Kafka) | P4 §6.1 | **Absent** |
| Frontend | P4 §10 | **Absent** |
| Deployment (docker/k8s/helm/alembic) | P4 §3/§13 | Empty dirs |

---

## Resolution log (Blockers + HIGH + MEDIUM remediation)

Applied fixes for all 7 BLOCKER findings, then for all 6 HIGH findings, then for all 5 MEDIUM findings. Verified via runtime repro + full-package import scan.

### Blockers

| ID | Fix |
|---|---|
| B1 | `ObservabilityPort` relocated to `ports/observability/telemetry.py`; `infrastructure/observability/telemetry.py` re-exports it. `services.py` switched to bare `investigation_agent_platform.*` imports (also removes the duplicate broken import block). `services.py` import now succeeds. |
| B2 | `EvidencePage` defined in `domain/evidence/models.py` per Part 3 §4.1 (`items`, `next_cursor`, `has_more`, `total_count`). `gateway.py`/`application/evidence/gateway.py`/`elastic.py` import it successfully. |
| B3 | `Evidence` schema unified (Part 1 + Part 3): now carries `content: dict`, `provenance`, `freshness`, `is_redacted`. `SensitiveDataRedactor.redact_evidence` no longer raises `AttributeError`; redaction manifest + `is_redacted` preserved. Verified: `redact` returns redacted content with manifest entry. |
| B4 | `EvidenceType` extended with documented members `RUNTIME_LOG`, `RUNTIME_TRACE`, `COMMIT_HISTORY`, `CALL_GRAPH`, `EXCEPTION_PATH`. Adapter references resolve; verified via construction. |
| B5 | `Evidence.evidence_id` is now a retained `str` field (adapter-supplied, e.g. `elastic:idx:docid`); `content`/`provenance`/`freshness` are real required fields, not silently dropped. Evidence identity/relationships now match adapter-reported IDs. Provider/repository protocols updated to `str` ids. |
| B6 | Duplicate `InvestigationAction` removed (single Part-1 schema: `action_type`/`parameters`/`execution_hash`); `guardrails.compute_fingerprint` uses `action_type.value`. Duplicate `Fact` fields removed (`statement`×2, stray `source_evidence_id`) — documented `Fact` (with `source_evidence_ids`) now constructs. Single `DomainException` (with `error_code` + `.code` alias); `InvalidHypothesisException`/`SecurityPolicyViolationException`/`InvalidLifecycleTransitionException`/etc. all `isinstance` of it — `except DomainException` works. |
| B7 | Validator now rejects traversal in both `file_path` and `repository`, incl. percent-encoded (`%2e%2e`) and backslash encodings + null bytes; root containment enforced at component boundaries (`src` no longer matches `src-secret`), replacing the old substring `..` + `startswith` checks. Verified accepted/rejected cases. |

### HIGH findings

| ID | Fix |
|---|---|
| H1 | `PostgresRelationshipRepository` SQL rewritten against the actual `evidence_relationships` schema (`source_node_id`/`target_node_id`/`tenant_id`; `application_id` filter removed — column does not exist). `EvidenceRelationshipORM` node-id/provenance columns changed from `UUID` to `String(256)` to hold string evidence ids. DB failures no longer swallowed as an empty graph — re-raised as `ExecutionError`. Empty root list still short-circuits to `[]`. |
| H2 | `scripts/setup.py: rollback()` corrected to `rollback(self)`; failure handler now runs a real rollback instead of raising `TypeError`. |
| H3 | Tenant isolation enforced on hydration: `EvidenceRepository.get_by_ids(evidence_ids, tenant_id)` (protocol + `EphemeralCorrelationEngine` call site), `CorrelationEngine.correlate(tenant_id, seed_identifiers, profile)` now carries tenant at the port boundary. |
| H4 | OCC implemented: new `SqlAlchemyInvestigationRepository` (`infrastructure/persistence/investigation_repository.py`) with `save(investigation, expected_version)` guarded by `WHERE version = expected_version`, raising `ConcurrencyError` when `rowcount == 0`; `create`/`get_by_id`/`delete`/`exists` (incl. request/context JSON snapshots) included. `InvestigationORM` gained `session_id`, `request_json`, `context_json`, `version`. |
| H5 | `CreateInvestigationService` builds a real window: `(now - default_time_window, now)` from `profile.investigation_configuration.default_time_window` instead of the zero-width `(now, now)`. |
| H6 | `SensitiveDataRedactor` now redacts recursively over nested dict/list content and adds patterns for bearer tokens, JWTs, AWS access keys, GitHub PATs, private keys, and credential-bearing connection strings (`postgres://user:pass@host`). Manifest entries record per-type original lengths; `is_redacted` set correctly. |

Verification summary for Blockers + HIGH + MEDIUM: full-module import scan 85/85 modules pass; runtime repros for every B, H, and M finding; no new ruff F/I errors introduced (remaining lint issues are pre-existing baseline debt).

Remaining next-level buckets: LOW (L1–L8).

### MEDIUM findings

| ID | Fix |
|---|---|
| M1 | Persistence now covers the documented 15-table schema. Added ORM tables: `investigation_transitions`, `investigation_facts`, `investigation_entities`, `evidence_references`, `timeline_events`, `hypotheses`, `hypothesis_evidence`, `findings`, `investigation_conclusions`, `investigation_actions`, `investigation_tool_executions`, `evidence_attributes`, `agent_actions`, `application_profiles`. Implemented SQLAlchemy repositories: `SqlAlchemyEvidenceRepository`, `SqlAlchemyHypothesisRepository`, `SqlAlchemyTimelineRepository`, `SqlAlchemyApplicationProfileRepository` (join the existing OCC `SqlAlchemyInvestigationRepository`). Port protocols updated so `save`/`append` accept an optional `investigation_id` (domain aggregates carry no investigation link) and `get_by_application_id` returns `ApplicationProfile | None`; `ResumeInvestigationService` now guards a missing profile. Evidence stores a full JSON payload + typed columns with a unique `(tenant_id, evidence_key)` constraint. |
| M2 | Missing deps added via `uv add`: `tenacity` (unblocks `ingress.py`; full-package import scan now 85/85), `mcp`, `tree-sitter` + `tree-sitter-language-pack` (provides `tree_sitter_languages.get_parser`; `tree-sitter-languages` itself has no py3.13 wheel), `temporalio`, `minio`, `fsspec`, `faststream[kafka]`. |
| M3 | Config matched to docs: `config/application.yaml` now carries `persistence.connection_pool_size`, `reasoning.provider/model`, `observability.tracing_enabled/otlp_endpoint` (Part 1 §10.1); added `config/logging.yaml` (§10.2) and both `config/profiles/{example,payments}.yaml` (§10.3/10.4). New YAML loaders in `infrastructure/configuration/config.py`: `load_platform_settings`, `load_application_profile_from_file`, `load_application_config_from_yaml` (binds file + env secrets). |
| M4 | Elasticsearch pagination is a real paging token: query sorts on `@timestamp`/`_id` and uses `search_after` from an opaque base64/JSON cursor (`AsyncElasticAdapter`); `next_cursor` only set when `has_more`; malformed cursors logged + ignored; `total_count` handles dict/int/absent ES total. |
| M5 | Messaging backbone: `ports/messaging/publisher.py` (`EventPublisher`), `infrastructure/messaging/faststream.py` (Kafka `KafkaEventPublisher`, `investigation.events` subscriber + audit group, `build_faststream_app`, `make_envelope`), `domain/events/base.py` gained `InvestigationCancelled`. `CancelInvestigationService` publishes `InvestigationCancelled` after a successful OCC save (§6.4 step 5). |

## Recommended remediation order

1. **Unify the domain schema** — resolve `Evidence` to a single model (merge Part 1 + Part 3 fields), define `EvidencePage`, alias/extend `EvidenceType` with the documented members (`RUNTIME_LOG`, `COMMIT_HISTORY`, `CALL_GRAPH`, `EXCEPTION_PATH`), and delete the duplicated `InvestigationAction`/`Fact`/`DomainException` definitions. This fixes B2/B3/B4/B5/B6/H6 in one pass.
2. **Fix the import convention** — standardize on bare `investigation_agent_platform.*` imports and add `pythonpath = ["src"]` (already in pytest config); this restores mypy and installability (L1).
3. **Relocate/implement ports** — move provider ports to `ports/evidence/`, add `ObservabilityPort` at `ports/observability/`, complete `InvestigationTriggerPort` (B1, L2, L3).
4. **Add deps** — `tenacity`, `tree-sitter`(+languages), `mcp`, `temporalio`, `faststream[kafka]`, `fsspec`/`minio`, `presidio-analyzer`/`anonymizer`, dev `testcontainers` (M2, L6).
5. **Implement persistence** — remaining ORM tables + repositories + OCC + Alembic migrations; align `PostgresRelationshipRepository` to `evidence_relationships` schema with tenant-bound evidence fetch (H1/H3/H4, M1).
6. **Write the mandated test suites** (L4) before connecting any adapter, using the Part 2 simulation fixtures.