# Design: LLM Reasoner — Code-Grounded Evidence & Sandboxed API Verification

Closes the gap exposed by self-investigation `a301ece4` (2026-10-16): the reasoner concluded
`ROOT_CAUSE_ESTABLISHED` on an API-behavior complaint ("profile versions are being dropped")
using only log and generic-Postgres evidence. It never read the route handler that actually
answers the complaint, and it had no action that could call the API and observe what came back.
A human architect disproved the conclusion in minutes with one `GET_CODE` read and four `curl`
calls. This design gives the reasoner those two capabilities — grounded code search/read and a
sandboxed HTTP probe — plus the context, prompt, and gating changes needed so it uses them
*before* concluding, not instead of a post-hoc human check.

## Context

### Current reasoning loop

`ReasoningCoordinator.reason()` (`src/investigation_agent_platform/application/investigation/reasoning.py`)
is the entire decision point. Each iteration it:

1. Builds a single JSON blob (`facts_block`) containing the problem description, known facts,
   up to 100 evidence *summaries* (truncated to 1000 chars each, no payload), the profile-derived
   `state_templates` catalog, and the last 10 action-history entries (iteration/action/readiness/
   hypotheses/observations only — no parameters, no outcome detail).
2. Sends `facts_block` plus a **hardcoded system-prompt string** (one ~60-line literal, not
   data-driven) to the LLM gateway and gets back one `InvestigationDecision` — one action, to be
   dispatched by `application/worker/activities.py`.
3. Validates the decision's `action_type` against `ActionType` (`domain/investigation/models.py`):
   `SEARCH_LOGS | QUERY_STATE | GET_CODE | CORRELATE | FORMULATE_HYPOTHESIS | VERIFY_HYPOTHESIS |
   CONCLUDE`. This is the complete action catalog — seven fixed verbs, enforced structurally (an
   unknown value raises before the decision is even returned).

Verification sits downstream in `application/investigation/verification.py`: `VerificationEngine`
scores a hypothesis via `coverage/reliability/causal/contradiction` and gates `VERIFIED` on
`cross_layer` evidence — distinct **providers** (`len(providers) >= 2`), not distinct capabilities
exercised. Ten Postgres rows plus one Elastic hit already satisfy `cross_layer` today; a profile
read-path investigation can hit `CONCLUDE` without ever calling `GET_CODE` or anything API-shaped,
because the gate counts *where evidence came from*, not *whether the complaint's actual surface
(a route, a query) was ever queried*.

### The three concrete gaps, traced to code

**Gap 1 — `GET_CODE` requires an exact path; there is no way to find one.**
`PyGit2Adapter.get_source()` (`infrastructure/evidence/code/git.py:115`) does
`commit.tree[file_path]` — a single blob lookup. If the path is wrong it raises
`CodePathNotFoundError`, which `activities.py:_handle_get_code` (line 1532) degrades to a silent
`return 0` (zero evidence, no error surfaced to the model beyond the next iteration's action
history). The system prompt tells the model to derive `file_path` "from the problem_description" —
but investigation problem statements describe *symptoms* ("profile versions are being dropped"),
not file paths. A real coding agent never guesses a path cold; it searches, greps, or lists a
directory first. The reasoner has no equivalent action, so it either never calls `GET_CODE` or
calls it once, misses, and gives up on code evidence for the rest of the run — which is exactly
what happened in `a301ece4` (zero `SOURCE_CODE` evidence rows in the completed run).

**Gap 2 — a repo-wide symbol/call graph already exists and is completely unwired.**
`CodebaseGraphPipeline` (`infrastructure/evidence/code/pipeline.py`) builds a tree-sitter-parsed,
`rustworkx`-backed symbol graph (`build_repository_graph`) with ORM/SQL-keyword detection
(`infrastructure/evidence/code/intelligence.py`) — the exact index a "find the route for X"
search needs. `rg -n "CodebaseGraphPipeline" src/` outside its own file and tests returns nothing:
it is built, tested, and never invoked from any reasoning-time code path. This is not a build task;
it is a wiring task, and the design below reuses it rather than building a second indexer.

**Gap 3 — there is no action that observes platform behavior directly.**
Every action resolves to *stored* evidence: logs already shipped, DB rows already written, files
already committed. None of them can ask "what does `GET /api/v1/profiles/{id}/revisions` return
right now?" — the one question that would have falsified the `a301ece4` hypothesis immediately.
`httpx` is already a direct dependency (`pyproject.toml`), unused for this purpose. Without a
sandboxed, profile-scoped HTTP-probe action, every API-behavior complaint is necessarily concluded
from *inference* (logs/code say what *should* happen) rather than *observation* (an actual
request/response pair) — this is the generic-verdict failure mode named in the incident.

**Compounding factors (context-assembly level, not action-catalog level):**

- `ReasoningContext.available_actions` (`reasoning.py:40`) is declared, typed, and **never
  populated or read** — the action catalog the model sees is a literal string baked into
  `system_prompt`, not data. Adding an action today means editing prose inside a Python f-string
  and hoping the model parses the new clause correctly; there is no structured, testable contract
  between "actions that exist" and "actions the model is told about."
- Action-history entries dropped to the model carry no `parameters` and no `evidence_count`/
  `miss_reason` per step (only `iteration/action/readiness/hypotheses/observations`) — the model
  cannot see *that* `GET_CODE` was tried with a bad path and missed, only that some `GET_CODE`
  happened. It cannot learn from its own failed attempts within the same run.
- Evidence summaries are truncated to 1000 characters with no indication of *why* (budget) or
  *what was cut* — for `SOURCE_CODE` evidence this routinely truncates mid-function, which is
  worse than not truncating, because the model reasons over a syntactically broken fragment
  without knowing it is broken.
- The diversity rule in the system prompt ("if the SAME action_type chosen 3+ times... choose a
  DIFFERENT capability") only fires on 3 consecutive repeats of one action with *zero* evidence
  overall — it does not fire when the model repeatedly chooses among log/state actions while
  never touching code or API capabilities at all. There is no requirement coupling the complaint's
  *shape* (API-behavior vs. data-correctness vs. performance) to which capability class must be
  exercised before `CONCLUDE`.

No `docs/dev/architecture.md` exists in this project; `docs/IAP-architecture-v1.0.md` is the
as-built architecture reference and is updated per the subsection at the end of this document.

## References

- `src/investigation_agent_platform/application/investigation/reasoning.py` — current prompt
  assembly, context shape, decision validation (read in full for this design).
- `src/investigation_agent_platform/application/worker/activities.py:1464-1670` — action dispatch
  table (`_handle_query_state`, `_handle_get_code`, `_handle_correlate`, etc.) and existing
  evidence-banking/truncation conventions (`_bank`).
- `src/investigation_agent_platform/infrastructure/evidence/code/git.py` — `PyGit2Adapter`:
  tenant allow-list enforcement, path-traversal guards (`_validate_tree_path`), size/line caps
  (`MAX_SOURCE_BYTE_SIZE`, `MAX_SOURCE_LINE_COUNT`) — the security pattern every new code action
  must inherit exactly.
- `src/investigation_agent_platform/infrastructure/evidence/code/pipeline.py` and
  `intelligence.py` — the unwired `CodebaseGraphPipeline`/`CodeSymbolGraphBuilder`, reused rather
  than reimplemented by Decision D2.
- `src/investigation_agent_platform/application/investigation/verification.py` — `VerificationEngine`
  cross-layer scoring and `ConclusionGate`; the gate this design tightens (Decision D5).
- `src/investigation_agent_platform/domain/profile/models.py` — `ApplicationProfile`,
  `Service` (`endpoints: list[str]` — reused as the probe allowlist seed), `ServiceCode`
  (`source_roots`, `paths` — reused for code-search scoping), `allowed_capabilities` (the existing
  per-profile capability gate every new `ActionType` must register against).
- `src/investigation_agent_platform/domain/common/exceptions.py` — `CodePathNotFoundError`,
  `ProviderNotConfiguredError`, `SecurityPolicyViolationException`, `ExecutionError` — reused,
  not duplicated, by the new adapters.
- `pyproject.toml` — confirms `httpx>=0.28.1`, `pygit2>=1.20.1`, `tree-sitter>=0.26.0`,
  `rustworkx>=0.18.1` are already direct dependencies. **No new runtime dependency is introduced
  by this design.**
- Incident evidence: self-investigation `a301ece4-e580-457d-bbb4-4cfa2cb9e28e` (tenant `n-a`,
  2026-10-16) — `ROOT_CAUSE_ESTABLISHED` at confidence 0.80 from 10 `ELASTIC` + 1 `POSTGRES`
  evidence rows, zero `SOURCE_CODE` rows, zero API-probe evidence (the action didn't exist);
  independently falsified by direct `GET_CODE`-equivalent reads and four live `curl` probes.

## Goals / Non-Goals

**Goals:**

- Give the reasoner a **code-search** action (`FIND_CODE`) that answers "where is the code for
  X?" without a pre-known path, built on the already-present tree-sitter/rustworkx pipeline.
- Give the reasoner a **sandboxed, profile-scoped HTTP-probe** action (`PROBE_API`) that can call
  the application's own API and bank the response as evidence — closing exactly the gap named in
  the incident.
- Make the action catalog **data-driven**: one typed `ActionCatalog` the prompt is rendered from,
  so `available_actions` stops being dead and new actions do not require hand-editing prose.
- Make the **context given to the LLM** richer where it is currently lossy: full action-history
  (parameters + outcome, not just the action name), evidence-truncation disclosure, and a
  per-complaint **capability-class requirement** (API-behavior complaints must exercise code
  and/or API-probe capabilities, not just logs/state, before `CONCLUDE` is even offered).
- Tighten `ConclusionGate`/`VerificationEngine` so cross-layer corroboration is defined by
  **capability class exercised**, not merely **provider count** — the scoring hole the incident
  evidence (10 Postgres + 1 Elastic) walked straight through.
- Keep every new capability inside the existing security model: profile-scoped allow-lists,
  tenant checks, path/traversal validation, size/time/rate bounds — the same posture `GET_CODE`
  already has, extended, not loosened.

**Non-Goals:**

- No general-purpose code execution, shell access, or arbitrary outbound network access for the
  agent. `PROBE_API` calls only the investigated application's own declared endpoints, read-only
  by default.
- No semantic/embedding-based code search (vector index over source) in this phase — the
  tree-sitter/rustworkx symbol graph already exists and is sufficient for "find the route/handler
  for X"; embeddings are a possible future enhancement, not required to close the named gap.
- No change to the Temporal workflow shape, budget enforcement mechanism, or evidence persistence
  layer beyond adding two new evidence-producing action handlers that follow existing conventions
  (`_bank`, `save`/`save_batch`).
- No UI changes. This is entirely within the worker/reasoning layer.
- No multi-step agentic tool loop *within* a single LLM call (e.g., ReAct-style sub-loops). The
  platform's existing one-action-per-iteration loop is retained; this design adds actions to that
  loop and improves what each iteration sees, not the loop's control structure.

## Decisions

### D1: Data-driven `ActionCatalog` replacing the hardcoded system-prompt string

**Decision:** Introduce `ActionSpec`/`ActionCatalog` as the single source of truth for "what
actions exist, what parameters they take, and what the model must know to call them correctly."
`ReasoningContext.available_actions` (currently declared and never populated — `reasoning.py:40`)
becomes the real wire: the catalog is rendered into both the system prompt and
`facts_block["available_actions"]`, so the model receives the same structured description that
drives validation. This turns "edit a prose string and hope" into "add an `ActionSpec` and the
prompt, the context, and the validator all update together."

```python
# src/investigation_agent_platform/application/investigation/action_catalog.py
from pydantic import BaseModel, Field

from investigation_agent_platform.domain.investigation.models import ActionType


class ActionSpec(BaseModel):
    """One entry in the reasoner's action catalog — rendered into both the
    system prompt and the structured context, and used to validate that a
    returned decision's parameters are shaped the way the model was told."""

    action_type: ActionType
    summary: str = Field(..., max_length=200)
    when_to_use: str = Field(..., max_length=400)
    required_parameters: list[str] = Field(default_factory=list)
    optional_parameters: list[str] = Field(default_factory=list)
    parameter_hints: dict[str, str] = Field(default_factory=dict)
    # Capability class this action evidences, for the gate in D5.
    capability_class: str  # "log" | "state" | "code" | "api" | "graph" | "meta"
    # Profile allowed_capabilities key gating this action (None = always on).
    requires_profile_capability: str | None = None


# One module-level, versioned catalog. Extending the action set means adding
# an entry here — reasoning.py and activities.py both read from it.
ACTION_CATALOG: list[ActionSpec] = [
    ActionSpec(
        action_type=ActionType.SEARCH_LOGS,
        summary="Keyword/time-ranged search over shipped application logs.",
        when_to_use="First action when evidence_records is empty and the complaint mentions symptoms, errors, or behavior over time.",
        required_parameters=["keywords"],
        optional_parameters=["services", "limit"],
        parameter_hints={"keywords": "terms from problem_description", "services": "leave EMPTY unless exact service identifiers are named"},
        capability_class="log",
    ),
    ActionSpec(
        action_type=ActionType.QUERY_STATE,
        summary="Run a pre-declared, parameterized query template against a profile datasource.",
        when_to_use="When the complaint concerns stored state (rows, counts, config) and a matching template_id exists in application.state_templates.",
        required_parameters=["template_id"],
        optional_parameters=[],
        parameter_hints={"template_id": "must be one of application.state_templates[].template_id verbatim"},
        capability_class="state",
    ),
    ActionSpec(
        action_type=ActionType.FIND_CODE,
        summary="Search the repository symbol/call graph for definitions, routes, or references matching a query — returns candidate locations, not file contents.",
        when_to_use="Whenever a complaint describes behavior (an endpoint, a function name, a class, 'X is not returning Y') and no exact file_path is yet known. ALWAYS call this before GET_CODE unless a path is already confirmed from a previous FIND_CODE or GET_CODE result this run.",
        required_parameters=["query"],
        optional_parameters=["kind", "limit"],
        parameter_hints={"query": "symbol name, route path, or short phrase from the complaint", "kind": "one of route|function|class|any"},
        capability_class="code",
        requires_profile_capability="code.search",
    ),
    ActionSpec(
        action_type=ActionType.GET_CODE,
        summary="Read a bounded snippet of one exact source file at one exact path.",
        when_to_use="Only after FIND_CODE (or a prior GET_CODE this run) has confirmed the exact file_path — never as a first guess.",
        required_parameters=["file_path"],
        optional_parameters=["start_line", "end_line"],
        parameter_hints={"file_path": "repo-relative path returned by a prior FIND_CODE result"},
        capability_class="code",
        requires_profile_capability="code.read",
    ),
    ActionSpec(
        action_type=ActionType.PROBE_API,
        summary="Issue a read-only HTTP request to one of the investigated application's own declared endpoints and bank the live response as evidence.",
        when_to_use="Whenever the complaint is about observable API/service behavior (missing data, wrong status code, wrong shape) — this is the ONLY action that observes current behavior directly rather than inferring it from logs or code; prefer it over concluding from code/log inference alone whenever an endpoint for the complaint exists in application.probeable_endpoints.",
        required_parameters=["endpoint_id"],
        optional_parameters=["path_params", "query_params"],
        parameter_hints={"endpoint_id": "must be one of application.probeable_endpoints[].endpoint_id verbatim"},
        capability_class="api",
        requires_profile_capability="api.probe",
    ),
    ActionSpec(
        action_type=ActionType.CORRELATE,
        summary="Traverse the evidence-relationship graph from root evidence to find connected items.",
        when_to_use="After 2+ evidence items exist and a causal/structural link between them is suspected.",
        required_parameters=["root_evidence_ids"],
        optional_parameters=["max_depth"],
        parameter_hints={"root_evidence_ids": "evidence_id strings from evidence_records"},
        capability_class="graph",
    ),
    ActionSpec(
        action_type=ActionType.FORMULATE_HYPOTHESIS,
        summary="Record a candidate root-cause statement for later verification.",
        when_to_use="As soon as evidence_records is non-empty and a plausible explanation exists.",
        required_parameters=["statement"],
        optional_parameters=[],
        parameter_hints={},
        capability_class="meta",
    ),
    ActionSpec(
        action_type=ActionType.VERIFY_HYPOTHESIS,
        summary="Score a hypothesis against the VerificationEngine policy.",
        when_to_use="When at least one hypothesis exists and enough cross-capability evidence has been gathered to test it.",
        required_parameters=["hypothesis_id"],
        optional_parameters=[],
        parameter_hints={},
        capability_class="meta",
    ),
    ActionSpec(
        action_type=ActionType.CONCLUDE,
        summary="Finalize the investigation with a root-cause conclusion.",
        when_to_use="Only when ConclusionGate preconditions are met — see constraints.conclusion_requirements in the context.",
        required_parameters=[],
        optional_parameters=[],
        parameter_hints={},
        capability_class="meta",
    ),
]


def catalog_for_prompt() -> list[dict[str, object]]:
    """Render the catalog into the structured context block (facts_block)."""
    return [spec.model_dump(mode="json") for spec in ACTION_CATALOG]
```

**Alternative considered:** Keep the system prompt as hand-maintained prose and only add the two
new actions' clauses to it. Rejected — this is exactly the pattern that let `available_actions`
go dead for the entire life of the project to date; every action added this way increases the
chance the prompt text and the validator (`ActionType` enum) drift, and there is no way to unit
test "does the model's instruction match what the dispatcher actually accepts."

**Rationale:** A typed catalog is testable (one test can assert every `ActionType` has exactly one
`ActionSpec`, and every `requires_profile_capability` has a matching `allowed_capabilities` key
documented somewhere), and it is the datum both the prompt-renderer (D3) and the gate (D5) read
from, instead of three independent sources of truth.

**Trade-offs:** The system prompt becomes generated text instead of a literal string — slightly
harder to eyeball in a diff, but `catalog_for_prompt()` output is logged once per investigation at
debug level so the exact rendered catalog is inspectable without re-deriving it mentally.

---

### D2: `FIND_CODE` — wire the existing symbol/call graph into a search action

**Decision:** Add `ActionType.FIND_CODE` backed by a new `CodeSearchPort`/`CodeSearchAdapter` that
wraps the **already-built** `CodebaseGraphPipeline`/`CodeSymbolGraphBuilder`
(`infrastructure/evidence/code/pipeline.py`) instead of writing a second indexer. The adapter
builds (and caches, per repository+commit) the symbol graph on first use, then answers queries by
walking it: symbol-name substring/fuzzy match, route-decorator detection (the pipeline already
tags ORM/SQL-keyword and, per `intelligence.py`, is extensible to route decorators — extend
`EXT_TO_LANGUAGE`-adjacent detection with a `ROUTE_PATTERNS` table mirroring the existing
`ORM_PATTERNS`/`SQL_KEYWORDS` convention), and returns **candidate locations**, never file
contents — content retrieval stays `GET_CODE`'s job, keeping the security/size boundary
(`MAX_SOURCE_BYTE_SIZE`, `MAX_SOURCE_LINE_COUNT`) in exactly one place.

```python
# src/investigation_agent_platform/ports/evidence/code_search.py
from pydantic import BaseModel, Field


class CodeSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=200)
    kind: str = Field(default="any")  # "route" | "function" | "class" | "any"
    limit: int = Field(default=10, ge=1, le=25)


class CodeSearchHit(BaseModel):
    file_path: str
    symbol_name: str | None = None
    symbol_kind: str  # "route" | "function" | "class" | "module"
    line_start: int
    line_end: int
    # Short, non-content preview (signature/decorator line only) — enough to
    # choose a GET_CODE target without duplicating GET_CODE's content budget.
    preview: str = Field(max_length=300)


class CodeSearchResult(BaseModel):
    hits: list[CodeSearchHit]
    query_echo: str
    truncated: bool


class CodeSearchPort:
    """Read-only symbol/route search over one tenant-scoped repository
    checkout. Implementations MUST apply the same tenant allow-list and
    path-containment checks PyGit2Adapter already applies for GET_CODE —
    this port extends the read surface, not the trust boundary."""

    async def search(
        self, tenant_id: str, application_id: str, request: CodeSearchRequest
    ) -> CodeSearchResult: ...
```

```python
# src/investigation_agent_platform/infrastructure/evidence/code/search_adapter.py
# (signature-level design; full body is implementer-owned)
class CodebaseSymbolSearchAdapter(CodeSearchPort):
    def __init__(
        self,
        repo_base_path: Path,
        pipeline: CodebaseGraphPipeline,   # reused, not reimplemented
        tenant_allowlist: dict[str, set[str]] | None,
        graph_cache: GraphCache,           # keyed by (repository, commit_sha); TTL or LRU
    ) -> None: ...

    async def search(self, tenant_id, application_id, request) -> CodeSearchResult:
        # 1. resolve profile -> repository/branch (same _primary_locator as GET_CODE)
        # 2. enforce tenant allow-list (same _require_tenant_scope as GET_CODE)
        # 3. graph = await graph_cache.get_or_build(repository, commit_sha, pipeline)
        # 4. match request.query against graph node names/decorators (case-insensitive
        #    substring first; fall back to token-overlap scoring if zero hits — never
        #    shell out to `rg`/grep: stay inside the already-sandboxed pygit2 tree)
        # 5. filter by request.kind, cap at request.limit, return CodeSearchHit list
        #    with `preview` = the single matched line (decorator/def/class line),
        #    never a multi-line body (that is GET_CODE's job, size-bounded there)
        ...
```

Dispatch mirrors `_handle_get_code`'s existing degrade-to-zero convention on miss/provider-absent
(never fails the investigation), and banks a lightweight `EvidenceType.SOURCE_CODE` manifest-only
evidence row per hit (no content — the hits themselves, so `CORRELATE` and later `GET_CODE` calls
can reference `evidence_id`s for the found locations) plus returns the hit list inline in the
action's observation for the *same* iteration's reasoning:

```python
# src/investigation_agent_platform/application/worker/activities.py
# — new handler alongside _handle_get_code, same file/pattern —
async def _handle_find_code() -> int:
    from investigation_agent_platform.ports.evidence.code_search import CodeSearchRequest

    query = str(parameters.get("query", "")).strip()
    if not query:
        logger.warning("FIND_CODE without query degrades to empty", extra=_log_extra(...))
        return 0
    req = CodeSearchRequest(
        query=query,
        kind=str(parameters.get("kind", "any")),
        limit=min(int(parameters.get("limit", 10)), 25),
    )
    try:
        result = await code_search.search(tenant_id, application_id or "example-app", req)
    except (ProviderNotConfiguredError, CodePathNotFoundError) as exc:
        logger.warning("FIND_CODE degrades to empty", extra=_log_extra(..., error=str(exc)))
        return 0
    banked = _bank([_hit_to_evidence(h, tenant_id, investigation_id) for h in result.hits])
    if evidence_repo is not None and banked:
        await evidence_repo.save_batch(tenant_id, banked, investigation_id)
    return len(banked)
```

**Alternative considered:** Shell out to `rg`/`grep` over the checked-out worktree. Rejected on
two grounds: (1) it introduces a subprocess/command-construction surface this codebase has
deliberately avoided everywhere else (every existing code action goes through `pygit2`'s in-process
tree API with `_validate_tree_path`/`_require_tenant_scope`); (2) it duplicates work the
tree-sitter pipeline already does better — `rg` finds text, the symbol graph finds *routes and
definitions*, which is what "find the handler for X" actually needs.

**Alternative considered:** Build a new, separate lightweight regex-based path/symbol index
instead of reusing `CodebaseGraphPipeline`. Rejected — the pipeline exists, is tested
(`tests/unit/test_correlation_repository.py`-adjacent coverage for the graph layer, verify at
implementation time), and reusing it is strictly less code than building a parallel index that
would need its own ORM/route-detection logic from scratch.

**Rationale:** This closes Gap 1 and Gap 2 from Context in one action: the model gets a real
"find it" step, and the platform stops carrying a fully-built, fully-dead capability.

**Trade-offs:** First `FIND_CODE` call per repository+commit pays the one-time graph-build cost
(tree-sitter parse of the whole `source_roots` tree). Mitigated by `GraphCache` keyed on commit
SHA (checkouts are static git worktrees per `tmp/TO_DO_list_001.md` item 5's documented layout, so
the cache is valid until the worktree is re-pointed) — proportional for the current single-repo,
single-worktree deployment; a multi-repo/high-churn future would want an on-disk persisted graph,
noted as a deferred trigger in Risks.

---

### D3: `PROBE_API` — sandboxed, profile-scoped HTTP probe as a first-class action

**Decision:** Add `ActionType.PROBE_API`. The application's own profile declares which endpoints
are probeable (extending the existing `Service.endpoints: list[str]` with a structured
allowlist rather than a bare string list — see Data Storage), and a new `ApiProbeAdapter` built on
the already-present `httpx` makes a bounded, read-only call through an **egress allowlist** (host
must match the profile's own declared service address; method restricted to `GET`/`HEAD` unless
the profile explicitly opts an endpoint into mutating probes; no redirects followed cross-host;
hard timeout; response body capped like `GET_CODE`'s file-size cap). The response (status code,
headers allowlist, body truncated/redacted) becomes a new `EvidenceType.API_PROBE` evidence row —
this is **observed, live behavior**, the one evidence type nothing else in the catalog can produce.

```python
# src/investigation_agent_platform/ports/evidence/api_probe.py
from pydantic import BaseModel, Field


class ApiProbeRequest(BaseModel):
    endpoint_id: str = Field(..., max_length=128)  # must match a profile-declared ProbeableEndpoint.id
    path_params: dict[str, str] = Field(default_factory=dict, max_length=10)
    query_params: dict[str, str] = Field(default_factory=dict, max_length=10)


class ApiProbeResult(BaseModel):
    endpoint_id: str
    method: str
    resolved_url: str              # fully-resolved URL actually called, for audit
    status_code: int
    response_headers: dict[str, str]   # allowlisted headers only (never Set-Cookie/Authorization)
    response_body_excerpt: str = Field(max_length=4096)
    response_body_truncated: bool
    latency_ms: int


class ApiProbePort:
    """Read-only, profile-scoped HTTP probe against the investigated
    application's own declared endpoints. Never a general HTTP client —
    every call MUST resolve through a ProbeableEndpoint allowlist entry;
    there is no raw-URL calling mode."""

    async def probe(
        self, tenant_id: str, application_id: str, request: ApiProbeRequest
    ) -> ApiProbeResult: ...
```

```python
# src/investigation_agent_platform/infrastructure/evidence/api/probe_adapter.py
# (signature-level design; full body is implementer-owned)
class HttpxApiProbeAdapter(ApiProbePort):
    def __init__(
        self,
        profile_repo: ApplicationProfileRepository,
        client_factory: Callable[[], httpx.AsyncClient],  # short-lived client per call, bounded timeout
        max_response_bytes: int = 65_536,
        default_timeout_seconds: float = 5.0,
    ) -> None: ...

    async def probe(self, tenant_id, application_id, request) -> ApiProbeResult:
        # 1. profile = await self._profile_repo.get_by_application_id(tenant_id, application_id)
        # 2. endpoint = next((e for e in profile.probeable_endpoints if e.id == request.endpoint_id), None)
        #    -> raise ProbeEndpointNotFoundError if missing (same degrade-to-zero convention)
        # 3. validate path_params against endpoint.path_template's declared {placeholders} only —
        #    reject any path segment not in the template (no path injection into the URL)
        # 4. build URL: endpoint.base_url (profile-declared, loopback/internal by convention in
        #    dev; production posture is a Risk item below) + endpoint.path_template.format(**path_params)
        #    + urlencode(query_params ∩ endpoint.allowed_query_params)
        # 5. enforce endpoint.method (profile-declared; default GET) — reject anything else
        # 6. inject tenant header the platform itself requires (X-Tenant-ID: tenant_id) so the
        #    probe calls the API exactly as a real caller would, never bypassing tenant scoping
        # 7. one bounded httpx request: timeout=default_timeout_seconds, follow_redirects=False
        # 8. redact response_headers to an allowlist (content-type, x-correlation-id, date);
        #    truncate body at max_response_bytes; strip anything matching the existing
        #    sanitize_extra()-style secret patterns before it ever becomes evidence content
        ...
```

Dispatch handler mirrors `_handle_get_code`: missing/unknown `endpoint_id` degrades to zero
evidence with a warning log (never fails the investigation); successful probes bank one
`EvidenceType.API_PROBE` evidence row via the existing `evidence_repo.save` path.

**Alternative considered:** Let the model call the public API with an unrestricted URL parameter.
Rejected outright — this is an SSRF vector (the model could be prompted, directly or via evidence
injection, to probe arbitrary internal hosts) and violates the platform's existing
instruction/data separation principle (`EvidenceContextFormatter`'s untrusted-payload envelope
exists precisely because evidence content must never become instructions; an unrestricted URL
parameter would let evidence content *become a network destination*).

**Alternative considered:** Route probes through the existing `QUERY_STATE` action with a new
`datasource.kind = "http"`. Rejected — `QUERY_STATE`'s template system is built around read-only,
pre-vetted SQL templates with bind-parameter extraction; stretching it to cover arbitrary HTTP
verbs/bodies conflates two different trust models (a DBA-reviewed SQL template vs. a live call to
the system under investigation) and would make the `state_templates` prompt clause ambiguous
about what "template_id" means.

**Rationale:** This is the capability literally named in the incident follow-up: "self-
investigations of API behavior need a sandboxed API-call action." The allowlist-of-declared-
endpoints model matches the platform's existing posture exactly (profiles already declare
`services[].endpoints: list[str]` and `allowed_capabilities`) — this decision gives that
declaration teeth instead of adding a new, separate trust mechanism.

**Trade-offs:** Probing a running system during its own investigation can itself perturb state
(e.g., a probe against a non-idempotent endpoint). Mitigated by defaulting every
`ProbeableEndpoint` to `GET`/`HEAD` only; mutating methods require an explicit
`allow_mutating: true` on the profile entry, which is an operator decision, not a model decision —
documented as a deferred/opt-in path in Risks, not built as a default-on capability.

---

### D4: Full action-history and truncation disclosure in the reasoning context

**Decision:** Two additions to `ReasoningCoordinator.reason()`'s context assembly:

1. `recent_actions` history entries gain `parameters` (the actual action parameters, size-capped)
   and `outcome` (`"evidence_added": int, "miss_reason": str | None` — the same classification
   `_handle_get_code`/`_handle_find_code`/`_handle_probe_api` already log, just propagated into the
   decision metadata instead of staying log-only). This lets the model see *that* a prior
   `GET_CODE` call with `file_path="src/wrong/guess.py"` missed, not just that *a* `GET_CODE`
   happened — the single biggest lever for the model to choose `FIND_CODE` before guessing again.
2. Every evidence record in `facts_block["evidence_records"]` gains `"truncated": bool` (summary
   length hit the 1000-char cap) so the model can explicitly request the untruncated item via a
   follow-up action rather than silently reasoning over a cut fragment as if it were complete.

```python
# src/investigation_agent_platform/application/investigation/reasoning.py
# (within reason(), replacing the current history/evidence_records assembly)
history: list[dict[str, Any]] = []
for entry in (recent_actions or [])[-10:]:
    if not isinstance(entry, dict):
        continue
    history.append(
        {
            "iteration": entry.get("iteration"),
            "action": entry.get("action"),
            "parameters": _redact_action_parameters(entry.get("parameters", {})),
            "readiness": entry.get("readiness"),
            "hypotheses": entry.get("hypotheses"),
            "observations": entry.get("observations"),
            "evidence_added": entry.get("evidence_added", 0),
            "miss_reason": entry.get("miss_reason"),  # e.g. "code_path_not_found", "no_llm_gateway" — None on success
        }
    )
facts_block["recent_actions"] = history

evidence_records = [
    {
        "evidence_id": str(getattr(e, "evidence_id", "")),
        "evidence_type": str(getattr(getattr(e, "evidence_type", ""), "value", e)),
        "source": str(getattr(e, "source", "")),
        "summary": str(getattr(e, "summary", ""))[:1000],
        "truncated": len(str(getattr(e, "summary", ""))) > 1000,
    }
    for e in getattr(state, "evidence", [])[:100]
]
```

`_redact_action_parameters` reuses the existing `sanitize_extra`-style field-name/value filtering
already used for log extras elsewhere in `activities.py` — parameters never carry secrets by
contract (they are model-chosen query inputs), but the same belt-and-suspenders filtering applied
to logs is applied here before anything enters a prompt.

**Alternative considered:** Pass the full, untruncated `recent_actions` list (no `[-10:]` cap) and
full evidence summaries (no 1000-char cap) now that the model needs more signal. Rejected —
widening caps trades one failure mode (reasoning over missing information) for another (context
bloat inflating `llm_tokens_used` against `max_llm_tokens`, which TO_DO-list item 1 only just
wired up). The fix is **disclosure of what was cut**, not **removal of the cap** — the model can
always issue a targeted follow-up action for a specific truncated item.

**Rationale:** This directly targets the compounding factors named in Context — the model cannot
currently distinguish "I tried this and it worked" from "I tried this and it silently failed,"
which is the single largest reason an agent re-tries the same unproductive action or abandons a
capability class after one miss.

**Trade-offs:** None beyond the modest, bounded token increase from richer (but still capped)
history entries — strictly smaller than the token cost of the model re-attempting a missed action
blind, which is what happens today.

---

### D5: Capability-class gating — `ConclusionGate` requires the complaint's *shape* to be exercised

**Decision:** Two changes, one in prompt-time guidance and one in the hard gate:

1. **Prompt-time (soft, model-facing):** `facts_block["constraints"]` gains
   `required_capability_classes` — derived heuristically from the problem description (keyword
   signals: "endpoint"/"API"/"response"/"status code"/"returns" → `{"code", "api"}`; "row"/"count"/
   "table"/"query" → `{"state"}`; "slow"/"latency"/"timeout" → `{"log", "api"}`) and surfaced as
   plain instruction text, not a blocking rule — the LLM is told which capability classes this
   complaint's shape implies and reminded to exercise at least one before `FORMULATE_HYPOTHESIS`'s
   companion `VERIFY_HYPOTHESIS` call, not just told generically to "diversify."
2. **Gate-time (hard, enforced in code, not prompt-trusted):** `VerificationEngine.evaluate_hypothesis`
   and `ConclusionGate.evaluate` both gain a `capability_classes_exercised: set[str]` parameter
   (derived server-side from the investigation's actual action history — which `ActionSpec.capability_class`
   values produced evidence bound to this hypothesis — never from anything the model claims).
   `cross_layer` corroboration is redefined from "`len(providers) >= 2`" to
   "`len(capability_classes_exercised & {"code","api","state","log"}) >= 2` **and**, when
   `required_capability_classes` (computed the same way as the prompt hint, but now gate-side and
   authoritative) is non-empty, at least one of those required classes is present." This is the
   exact fix for the scoring hole the incident evidence exploited: 10 Postgres rows + 1 Elastic hit
   satisfied the old `providers >= 2` test while exercising zero code/API capability for an
   API-behavior complaint.

```python
# src/investigation_agent_platform/application/investigation/verification.py
# — signature changes to evaluate_hypothesis / ConclusionGate.evaluate —

def evaluate_hypothesis(
    self,
    hypothesis: Hypothesis,
    evidence_items: list[Evidence],
    contradictions: list[Contradiction],
    causal_chain: list[CausalRelationship],
    policy: RootCauseVerificationPolicy,
    capability_classes_exercised: set[str],       # NEW — server-derived, never model-supplied
    required_capability_classes: set[str],        # NEW — derived from problem_description
) -> VerificationResult:
    ...
    cross_layer = (
        len(capability_classes_exercised) >= 2
        and (not required_capability_classes or bool(required_capability_classes & capability_classes_exercised))
    ) if policy.require_cross_layer_evidence else True
    ...
    # explanation text names the capability classes, not just provider counts, so a human
    # reviewing a VERIFIED conclusion can see *what kinds* of checks actually happened.
```

`capability_classes_exercised` is computed once per `VERIFY_HYPOTHESIS`/`CONCLUDE` call from the
investigation's accumulated action history (every entry already carries `action`; map through
`ACTION_CATALOG` to `capability_class` and union with the set of actions whose evidence is
bound to this hypothesis) — a pure function over data the platform already owns, with no new
persistence required.

**Alternative considered:** Keep `providers >= 2` and simply add a new "must call FIND_CODE or
PROBE_API at least once" rule enforced purely at the dispatch layer (reject `CONCLUDE` if neither
action ever ran this investigation, regardless of complaint shape). Rejected as too blunt — it
would force a code/API call on every investigation (including ones genuinely resolvable from logs
alone, e.g., a clear exception stack trace), wasting budget on complaints where it adds nothing.
The complaint-shape-derived `required_capability_classes` targets the requirement at investigations
that actually need it.

**Rationale:** The gate is the only place in the system that cannot be prompt-injected or
reasoned around — the model can *claim* it checked the code, but `capability_classes_exercised`
is derived from what evidence-producing actions it actually dispatched. This is the same posture as
the existing `ConclusionGate` (`policy.require_causal_relationship and not causal_chain` is checked
server-side, never model-asserted); D5 extends that posture to cover the exact failure mode that
let `a301ece4` reach `ROOT_CAUSE_ESTABLISHED` without ever reading a line of code.

**Trade-offs:** Investigations whose genuine root cause really is log-only (e.g., an OOM kill, a
clear panic trace) now carry a heuristic `required_capability_classes` guess that *might*
incorrectly demand a code/API check they don't need, costing one extra iteration. Mitigated by
making the heuristic keyword-based and permissive (`required_capability_classes` defaults to
empty — i.e., no extra requirement — unless the problem description contains a reasonably
confident behavioral-complaint signal); false positives cost at most one bounded action, never an
investigation failure.

## Data Storage

No new relational tables. Two additive, backward-compatible profile-schema extensions (both
default to empty, so every existing profile — including `iap-self.yaml` v1-v5 — remains valid
without a migration):

```python
# src/investigation_agent_platform/domain/profile/models.py
# — additions to the existing file, not a new module —

class ProbeableEndpoint(BaseModel):
    """One API surface this profile authorizes PROBE_API to call. Declared
    once per profile, reused by every investigation against this application
    — the model never invents a URL, only selects an endpoint_id."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    id: str = Field(..., max_length=128)                 # the endpoint_id the model references
    description: str = Field(..., max_length=300)
    method: str = Field(default="GET", max_length=10)
    base_url: str = Field(..., max_length=512)            # internal/loopback base in dev; see Risks for prod posture
    path_template: str = Field(..., max_length=256)        # e.g. "/api/v1/profiles/{app_id}/revisions"
    path_placeholders: list[str] = Field(default_factory=list, max_length=10)
    allowed_query_params: list[str] = Field(default_factory=list, max_length=20)
    allow_mutating: bool = Field(default=False)            # operator opt-in; False means method is clamped to GET/HEAD

    @field_validator("method")
    @classmethod
    def validate_method(cls, v: str) -> str:
        allowed = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}
        if v.upper() not in allowed:
            raise ValueError(f"Unknown HTTP method {v!r}. Valid: {sorted(allowed)}.")
        return v.upper()


class CodeSearchScope(BaseModel):
    """Optional narrowing for FIND_CODE. Defaults to ServiceCode.source_roots
    when unset — a profile with code evidence already configured for GET_CODE
    needs no additional declaration to enable FIND_CODE."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    include_globs: list[str] = Field(default_factory=list, max_length=20)
    exclude_globs: list[str] = Field(default_factory=list, max_length=20)


class Service(BaseModel):
    # ... existing fields unchanged ...
    probeable_endpoints: list[ProbeableEndpoint] = Field(default_factory=list, max_length=50)
    code_search_scope: CodeSearchScope | None = Field(default=None)
```

`allowed_capabilities` (already on `ApplicationProfile`) gains two new recognized values —
`"code.search"` and `"api.probe"` — validated the same way existing capability strings are
(`ProfileBasedCapabilityRegistry`, unchanged, reused).

## Data Structures

Covered inline in Decisions D1–D3 (`ActionSpec`/`ActionCatalog`, `CodeSearchRequest`/
`CodeSearchHit`/`CodeSearchResult`, `ApiProbeRequest`/`ApiProbeResult`). One additional output
schema, the evidence-domain extension both new actions write through:

```python
# src/investigation_agent_platform/domain/evidence/models.py
# — addition to the existing EvidenceType enum —
class EvidenceType(StrEnum):
    RUNTIME_LOG = "RUNTIME_LOG"
    RUNTIME_TRACE = "RUNTIME_TRACE"
    LOG = "LOG"
    TRACE = "TRACE"
    DATABASE_STATE = "DATABASE_STATE"
    SOURCE_CODE = "SOURCE_CODE"
    COMMIT_HISTORY = "COMMIT_HISTORY"
    CALL_GRAPH = "CALL_GRAPH"
    EXCEPTION_PATH = "EXCEPTION_PATH"
    METRIC = "METRIC"
    DOCUMENTATION = "DOCUMENTATION"
    SYSTEM_EVENT = "SYSTEM_EVENT"
    API_PROBE = "API_PROBE"   # NEW — live request/response evidence from PROBE_API
```

`FIND_CODE` hits bank as `EvidenceType.SOURCE_CODE` (manifest-only — `content_uri` points at the
file, no `content_snippet`, distinguishing "found" from "read" without a new type).
`PROBE_API` results bank as the new `EvidenceType.API_PROBE`, `source` set to the resolved URL
(mirrors `GET_CODE`'s `source_location`/provenance pattern exactly — see `_values()` in
`infrastructure/persistence/evidence_repository.py`, unchanged by this design).

## Interfaces

No REST/CLI/MCP surface changes — these are internal reasoner/worker actions, not
externally-callable endpoints. The interface that changes is the **LLM decision contract**
(`InvestigationDecision.action.action_type`/`parameters`), documented as a table for implementer
clarity:

### Reasoner action catalog (internal contract)

| action_type | required parameters | capability_class | profile gate | evidence produced |
| --- | --- | --- | --- | --- |
| `SEARCH_LOGS` | `keywords` | `log` | — (existing) | `RUNTIME_LOG`/`LOG` |
| `QUERY_STATE` | `template_id` | `state` | — (existing) | `DATABASE_STATE` |
| `FIND_CODE` | `query` | `code` | `code.search` (new) | `SOURCE_CODE` (manifest-only) |
| `GET_CODE` | `file_path` | `code` | `code.read` (new, replaces implicit always-on) | `SOURCE_CODE` (with content) |
| `PROBE_API` | `endpoint_id` | `api` | `api.probe` (new) | `API_PROBE` |
| `CORRELATE` | `root_evidence_ids` | `graph` | — (existing) | `CALL_GRAPH`/relationship edges |
| `FORMULATE_HYPOTHESIS` | `statement` | `meta` | — (existing) | hypothesis record |
| `VERIFY_HYPOTHESIS` | `hypothesis_id` | `meta` | — (existing) | verification result |
| `CONCLUDE` | — | `meta` | — (existing) | conclusion |

`code.read` is a new named capability rather than `GET_CODE` staying implicitly always-on — this
is a deliberate tightening: profiles that declare code evidence today (via `ServiceCode`) get
`code.read`/`code.search` auto-granted at profile-validation time (`_check_semantics` in
`api/v1/routers/profiles.py`) if they already configure `source_roots`, so no existing profile
loses capability; profiles with no code config simply never had working `GET_CODE` anyway (it was
already degrading to `ProviderNotConfiguredError` → empty).

## Accessibility

Not applicable — no user-facing interface (UI, CLI, TUI) is introduced or changed by this design;
all surfaces are internal reasoner/worker contracts consumed by the platform itself.

## Testing Philosophy

### `ActionCatalog` completeness and drift

One test asserts a bijection between `ActionType` enum members and `ACTION_CATALOG` entries —
every enum value has exactly one `ActionSpec`, and no `ActionSpec` references a retired
`ActionType`. A second test renders `catalog_for_prompt()` and asserts every `required_parameters`
name appears in that action's `parameter_hints` keys (the prompt must explain every parameter it
demands) — this is the regression guard against the exact drift pattern that left
`available_actions` dead for the project's entire history to date.

### `FIND_CODE` search correctness and scoping

Build a small fixture repository (a handful of files with known routes/functions/classes) and
assert: an exact symbol-name query returns it first; a route-path query (`"/api/v1/profiles"`)
matches the decorator line, not an unrelated string occurrence; `kind` filtering excludes
non-matching symbol kinds; queries outside `code_search_scope`/`source_roots` return zero hits
(never silently search outside the declared scope); a repository-not-configured profile degrades
to zero evidence exactly like today's `GET_CODE`-without-provider case, with the matching log
event and no investigation failure.

### `PROBE_API` sandboxing and allowlist enforcement

Verify: calling an `endpoint_id` not present in `profile.probeable_endpoints` is rejected before
any network call is attempted; `path_params` outside the endpoint's declared
`path_placeholders` are rejected (no path-template injection); `query_params` outside
`allowed_query_params` are dropped, not passed through; a non-`GET`/`HEAD` request against an
endpoint with `allow_mutating=False` is rejected at the adapter layer regardless of what the model
requested; response headers are filtered to the allowlist (assert `Set-Cookie`/`Authorization`
never appear in banked evidence); a response body exceeding the byte cap is truncated with
`response_body_truncated=True` surfaced, never silently dropped; the probe injects
`X-Tenant-ID` matching the investigation's own tenant (never cross-tenant, never omitted); a
timeout or connection failure degrades to zero evidence with a `miss_reason`, never raises past
the dispatch handler.

### Capability-class gating end-to-end

Replay the exact incident scenario as a regression fixture: a problem description containing
API-behavior signal words ("endpoint", "returns"), evidence consisting only of `log`/`state`
capability-class items (mirroring the 10 Postgres + 1 Elastic rows from `a301ece4`), and assert
`VerificationEngine.evaluate_hypothesis` now returns `INCONCLUSIVE` (not `VERIFIED`) because
`required_capability_classes = {"code", "api"}` is disjoint from `capability_classes_exercised =
{"log", "state"}`. A second case with identical evidence plus one `FIND_CODE`/`PROBE_API`-sourced
item asserts the gate now passes (holding every other precondition constant) — proving the fix is
the capability-class check specifically, not a general tightening that would also block the
legitimate case.

### Context richness regression

Assert `facts_block["recent_actions"]` entries carry non-null `evidence_added` and (for a
deliberately-missed `GET_CODE` call in the fixture) a non-`None` `miss_reason`; assert an
evidence record whose source summary exceeds 1000 characters carries `truncated: true` — both
asserted against the literal JSON sent to the LLM gateway (captured via the existing gateway-mock
pattern used elsewhere in `tests/unit/test_gateway_composition.py`-adjacent tests), not just
against the Python objects before serialization.

## Documentation Plan

### `docs/IAP-architecture-v1.0.md`

**Audience:** Platform engineers, on-call, future architects

Add a subsection under "§5 API, MCP, providers, knowledge" (or the nearest matching as-built
section) documenting the two new action types, the data-driven `ActionCatalog` as the new source
of truth for the reasoner's capability set (replacing the prior "hardcoded system-prompt string"
characterization), the `ProbeableEndpoint`/`CodeSearchScope` profile extensions, and the
capability-class corroboration model replacing the old provider-count-only cross-layer check in
`VerificationEngine`. Update "§7 Delta appendix" to record this change as a deliberate hardening
following the `a301ece4` incident, so a future "why does the gate work this way" question resolves
to this design doc and the incident evidence, not an archaeology dig through git blame.

### `config/profiles/iap-self.yaml`

**Audience:** Operators maintaining the self-investigation profile

Document (in a comment block, matching the file's existing convention of inline rationale
comments) how to populate `probeable_endpoints` for the IAP self profile itself — this profile is
the one every regression fixture and the next self-investigation will exercise, so it should be
the reference example other profile authors copy. Values: at minimum the endpoints named in this
incident (`GET /api/v1/profiles/{app_id}`, `GET /api/v1/profiles/{app_id}/revisions`,
`GET /api/v1/profiles`) with `base_url` pointed at the dev-loopback API address already used by
`scripts/start-platform.sh`.

### `tmp/TO_DO_list_001.md`

**Audience:** The team tracking residual platform gaps

Add a new dated entry recording this design as the resolution path for "self-investigations of
API behavior need a sandboxed API-call action" (the exact gap this design closes), cross-
referencing this file, so the existing backlog convention (symptom → analysis → fix, DONE-stamped
on completion) stays the single place residual-gap status is tracked.

## Risks / Trade-offs

### `PROBE_API` against a production or shared environment

**Risk:** In a deployment where the investigated application's `base_url` is a real,
shared-traffic production host rather than a dev loopback, an automated investigation issuing
probes — even read-only `GET`s — adds load and, for any endpoint with side effects hidden behind a
nominally-safe verb (cache warming, rate-limit counters, audit logging), has observable effect on
the system under investigation.

**Mitigation:** `allow_mutating` defaults to `False` and is an explicit per-endpoint operator
opt-in, never a model choice. For this phase, `ProbeableEndpoint.base_url` is scoped to the same
environment the profile already declares (`ApplicationProfile.environment`) — a `production`-
environment profile probing a `production` base_url is a deliberate operator decision already
implied by declaring the endpoint at all, identical in posture to today's `QUERY_STATE` templates
(also operator-authored, also capable of hitting production data). Rate-limiting probes per
investigation (e.g., cap `PROBE_API` calls at N per run via the existing budget ledger's
per-action-type counters) is a reasonable future addition if usage data shows it is needed —
deferred rather than built speculatively, consistent with the platform's existing
"good enough now, clean later" posture on budget knobs.

### `FIND_CODE` graph cache staleness

**Risk:** The symbol graph is cached per repository+commit SHA. If the worktree is updated
in-place without the commit SHA changing (e.g., an uncommitted local edit during active
development), `FIND_CODE` would search a stale graph.

**Mitigation:** Checkouts in the documented pattern (`tmp/TO_DO_list_001.md` item 5,
`data/code-repos/<org>/<repo>` git worktrees) are read via `pygit2.Repository(...).revparse_single(branch)`
exactly as `GET_CODE` already does — the commit SHA is read fresh on every `FIND_CODE` call (cache
key includes it), so an actual commit on the worktree invalidates the cache automatically; only an
*uncommitted* dirty-tree edit would be invisible, which is already true of `GET_CODE` today (it
also reads the committed tree, not the working directory) — this design introduces no new
staleness class, it inherits the existing one.

### Heuristic `required_capability_classes` false negatives

**Risk:** The keyword-based complaint-shape detector (Decision D5) could miss a behavioral
complaint phrased without any of the trigger words, silently reverting to the old
provider-count-only gate for that investigation.

**Mitigation:** This is fail-open by design, not a silent regression — in the absence of a
detected signal, the gate behaves exactly as it does today (no new requirement, no behavior
change), so the worst case is "no improvement for this investigation," never "a previously-passing
investigation now fails incorrectly" or "a new false-pass mode is introduced." The keyword list is
a `tmp/TO_DO`-trackable, easily-extended data table (not hardcoded inline), so missed phrasings are
a one-line fix, not a redesign, once observed in practice — proportional for a first version of a
heuristic that has exactly one incident's worth of evidence behind it so far.

### Token/iteration budget pressure from two new mandatory-feeling actions

**Risk:** If the model over-applies the new capability-class guidance (calling `FIND_CODE` and
`PROBE_API` on every investigation regardless of complaint shape, "just in case"), it burns
iterations and tokens against `InvestigationLimits.max_tool_calls`/`max_llm_tokens` without
proportional benefit for log-only complaints.

**Mitigation:** The prompt-time guidance (D5, part 1) is explicitly conditional on detected
complaint shape, not a blanket "always try code and API" instruction — `required_capability_classes`
defaults to empty, and the system-prompt rendering only mentions the capability-class requirement
when non-empty for this investigation. Combined with the existing diversity rule (don't repeat a
non-productive action 3+ times) this bounds worst-case waste to a small, fixed number of extra
iterations per investigation, not an unbounded exploration loop.

## Open Questions (not blocking, flagged for the implementer)

- Should `FIND_CODE`'s route-detection (`ROUTE_PATTERNS`, mirroring `ORM_PATTERNS`/`SQL_KEYWORDS`
  in `intelligence.py`) start Python/FastAPI-only (matching the current single self-profile's
  `language: python`) or be designed multi-language from day one? Given `tree-sitter-language-pack`
  is already a dependency supporting many grammars, a language-dispatch table analogous to
  `EXT_TO_LANGUAGE` is cheap to make multi-language-ready even if only Python patterns ship first —
  recommend doing so, but this is an implementer call, not a blocking design decision.
- Should `GraphCache` (D2) be process-local (simplest, matches today's single-worker dev topology)
  or shared via the existing Postgres/buffered-store pattern used elsewhere in the platform? Given
  the current deployment is single-worker and worktrees are local to that worker's filesystem,
  process-local is proportional for now; flag as a migration point if/when multi-worker horizontal
  scaling is adopted (the trigger condition for revisiting this decision).
