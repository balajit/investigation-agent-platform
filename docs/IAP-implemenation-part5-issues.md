# IAP Part 5 — Open Issues Needing a Fix

Companion to `docs/IAP-implemenation-part5-v1.md` (design) and `prompt1_v1.md`
(implementation prompt). Each issue below is a self-contained unit of work:
background, exact gap, scope, acceptance criteria, and test requirements.
Issues are ordered by priority. Resolved issues are kept at the bottom as a
record — do not delete them; they document why the current code looks the
way it does.

Related code lives under
`src/investigation_agent_platform/infrastructure/evidence/code/` (pipeline,
graph builder, parser, intelligence provider),
`src/investigation_agent_platform/infrastructure/evidence/schema/` (DDL
parser), and `src/investigation_agent_platform/domain/topology/` (Layer 3
bounded context).

---

## ISSUE-1 (medium, open): Auto code→table linking driven by runtime evidence

**Status:** Open — deliberately deferred, not forgotten.

**Background.** `CodeSymbolGraphBuilder.link_code_to_tables(file_path,
function_name, table_name)` is now a working explicit API (converted from
uncallable module-level functions; see Resolved-1 below). It currently has
zero production callers. The pipeline builds code graphs and database graphs
side by side but never connects them automatically — because connecting them
requires knowing *which function accesses which table*, and guessing that
from symbol/table name similarity would manufacture false `ACCESSES_TABLE`
edges.

**What exists today that unblocks this.** `TreeSitterCodeIntelligenceProvider.find_database_operations`
(`infrastructure/evidence/code/intelligence.py`) already returns
`CodeLocation` records (file path, line number, snippet) proving a database
access was observed in code. Those locations are runtime evidence, not name
guesses.

**Scope of the fix.**

1. Add a pipeline step (or builder method, e.g.
   `link_evidence_to_tables(locations: Sequence[CodeLocation], tables: Sequence[str])`)
   that, for each `CodeLocation`:
   - finds enclosing code-symbol nodes in the same file whose
     `[start_line, end_line]` range contains `location.line_number`;
   - calls `link_code_to_tables` only on an unambiguous match
     (exactly one enclosing symbol; reuse the exact-then-suffix resolution
     already in `link_code_to_tables` by passing the qualified name through
     when known);
   - skips and debug-logs ambiguous or unmatched locations — never links
     opportunistically.
2. Thread the table inventory through: the step needs the set of known
   table names (from the ingested `TableSchema` records) so a `CodeLocation`
   can be attributed to a specific table. Locations that match SQL text
   without a known table must not create edges.
3. Keep the operation tenant-scoped: file paths resolve under the profile's
   `source_roots`, and the whole run inherits the pipeline's existing tenant
   authorization (`_require_tenant_scope`).
4. Do not change the `ACCESSES_TABLE` edge semantics or the existing
   `link_code_to_tables` resolution order; this issue only adds the caller
   that feeds it provenanced inputs.

**Acceptance criteria.**

- A pipeline run over a repo containing a DDL file and a Python file whose
  function contains a SQL string referencing a known table produces an
  `ACCESSES_TABLE` edge between the correct symbol node and the correct
  table node.
- A file referencing an unknown table produces no edge.
- A file where two same-named symbols enclose the location produces no edge
  (ambiguity refusal).
- Existing behavior without `ddl_paths` is unchanged.

**Tests required.**

- Symbol resolution: location inside one function links; location in module
  scope with a single candidate links via suffix rule.
- Multi-table files: each location links to its own table, no cross-wiring.
- Ambiguity: two enclosing same-named symbols → no edge, edge count unchanged.
- Unknown table: SQL text naming a table absent from ingested schemas → no edge.
- Tenant scoping: run under the existing provider allow-list tests.

**Quality gates.** Same as the repo standard (`ruff check`, `ruff format
--check`, `mypy` strict, `pytest`, `bandit -ll`).

---

## ISSUE-2 (low, open): Production callers for `link_code_to_tables`

**Status:** Open — blocked by ISSUE-1.

**Background.** Outside tests, nothing calls `link_code_to_tables` yet, so
`ACCESSES_TABLE` edges never appear in production graphs.

**Scope of the fix (after ISSUE-1 lands).** Either wire the ISSUE-1 step
into the default pipeline path (preferred — zero new API surface), or expose
code→table linking as an evidence-gateway code-topology operation
(`resolve_code_ownership`-style, authorized via `QUERY_CODE_TOPOLOGY`) so
Temporal activities can request it per investigation. Do not do both without
deduplicating — one authority for edge creation.

**Acceptance criteria.**

- At least one production path creates `ACCESSES_TABLE` edges end to end.
- The path is covered by an integration test using a real temp repo + DDL.

---

## ISSUE-3 (medium, implemented): Graph explosion from fine-grained per-revision AST nodes

**Status:** Implemented. `MACRO_NODE_TYPES` in
`domain/topology/models.py` is the single declarative mapping (everything
except `FIELD`); both adapters filter ingestion to macro nodes and report
`micro_skipped_count`; Neo4j rows carry a `granularity` property;
`MicroSymbolResolver` parses on demand with file-size cap, timeout, tenant
allowlist, and best-effort revision verification; the service records
`resolution_tier` and revision caveats. Original gap text preserved below
for context.

**Scope of the fix.** Adopt the hybrid resolution model:

1. **Neo4j (macro-topology):** persist `Domain`, `GitOrganization`,
   `Repository`, `Package`, `SourceFile`, and top-level symbols only
   (`CLASS`, top-level `FUNCTION`/`METHOD`, `ROUTE`, `MESSAGE_HANDLER`)
   with signatures. Add a `granularity` marker on `ASTNode` rows so macro
   vs. micro nodes are distinguishable in queries.
2. **Tree-sitter on demand (micro-topology):** keep precise intra-function
   line-range lookup inside the existing parser/provider path at query time
   against the checked-out revision, instead of projecting every nested
   symbol, closure, and variable declaration into Neo4j.
3. Define which `TopologyNodeType` values are macro-eligible in one place
   (extend the canonical enum section of the design doc, don't scatter the
   rule across adapters).
4. Attribution lookup tries the macro graph first and drops to on-demand
   micro resolution only when the macro match is insufficient, recording
   which tier answered in `DomainAttributionResult` provenance.

**Acceptance criteria.**

- Ingesting a large revision stores an order of magnitude fewer AST nodes
  than today while line-level attribution accuracy on a representative
  evaluation set does not regress.
- `DomainAttributionResult` (or its provenance) records macro vs. micro
  resolution tier.
- Node-type eligibility is a single declarative mapping, covered by a unit
  test.

**Tests required.**

- Ingestion volume test: fixed fixture revision produces only macro-tier
  nodes in the adapter.
- Attribution parity test: macro-first + micro-fallback resolves the same
  fixtures the full-graph path resolves today.
- Micro-tier isolation test: on-demand parsing is bounded (file size cap,
  timeout) and tenant-scoped like the existing provider path.

---

## ISSUE-4 (medium, open): Snapshot retention and garbage collection policy

**Status:** Open — design gap from `docs/ISSUES_0926.md` risk 2. D9 makes
snapshots immutable with a `SUPERSEDED` state, but nothing ever deletes
anything: per-commit `(tenant_id, repository_id, revision)` snapshots
accumulate forever and Neo4j eventually runs out of disk.

**Scope of the fix.**

1. Define the retention policy in the design doc's Data Storage section:
   - `READY` snapshots referenced by open investigations (or under tenant
     SLA) are pinned and never collected;
   - otherwise keep HEAD snapshots plus a bounded window (e.g. last 30
     days of active branches, LRU-bounded per repository).
2. Add a background cleanup job (Temporal scheduled workflow or worker cron,
   not a request path) that deletes orphan AST subgraphs snapshot by
   snapshot, marks the audit row collected, and never touches pinned
   snapshots.
3. Historical investigation audit links must survive collection: attribution
   evidence already stores snapshot ID + revision + ownership path, so verify
   (by test) that concluded investigations remain fully interpretable after
   their snapshots are collected.

**Acceptance criteria.**

- Policy documented with exact pinning rules and window sizes.
- Cleanup job deletes only unpinned, fully-ingested snapshots and is
  idempotent under retry.
- A concluded investigation's attribution evidence still resolves after its
  snapshot is collected.

**Tests required.**

- Retention selection test: pinned vs. collectible snapshots classified
  correctly from fixture audit rows.
- Collection test (mocked driver): orphan subgraph deleted, audit row
  updated, pinned snapshot untouched.
- Post-collection readability test: concluded-investigation evidence
  interprets without the live snapshot.

---

## ISSUE-5 (medium, open): Cross-repository hops via runtime trace evidence

**Status:** Open — design gap from `docs/ISSUES_0926.md` risk 3. Static
`CALLS` edges stop at API boundaries: traversal dead-ends at an HTTP client,
gRPC stub, or queue publisher with no knowledge of the target domain.

**Scope of the fix.**

1. When static traversal reaches a `ROUTE` or `MESSAGE_HANDLER` node (or a
   known client/publish call site), `FailureAttributionService` must attempt
   a cross-repository hop using OpenTelemetry trace headers / endpoint
   metadata from runtime evidence (trace ID, parent span, target
   service/queue name) already available through the evidence gateway.
2. Resolve the target repository via `RepositoryRegistryPort` (by service
   name → application → repository mapping; add the mapping if the registry
   cannot express it today), then continue attribution in the target
   snapshot with the same tenant/revision discipline.
3. Record every hop (source node, trace/span IDs, target repository +
   revision) in the attribution result's provenance and limitations; a hop
   that cannot be corroborated by trace evidence must not be taken
   silently — fall back to the current single-repository result instead.

**Acceptance criteria.**

- A checkout-service → payments-library HTTP call chain attributes across
  both repositories when trace evidence corroborates the hop.
- Without corroborating trace evidence, attribution stays
  single-repository (no fabricated cross-repo edge).
- Each hop is fully traced in provenance (span IDs, both revisions).

**Tests required.**

- Hop test: fixture with client call site + trace evidence resolves the
  target domain.
- No-hop test: same fixture without trace evidence stays single-repo.
- Tenant isolation test: trace evidence from another tenant never triggers
  a hop.

---

## ISSUE-6 (low, implemented): Graceful attribution degradation before INCONCLUSIVE

**Status:** Implemented. Fallback chain is now macro → micro → CODEOWNERS →
`INCONCLUSIVE`, with `ownership_source` (`static`|`codeowners`) and
`resolution_tier` (`macro`|`micro`) recorded on every result and rule
version bumped to `v2`.

**As built (deviations from the proposal above, all deliberate):**

- CODEOWNERS fires whenever static ownership yields no domain owner — including
  on AST-precise matches (the Neo4j adapter never populates ownership, so
  without this the production path would degrade on every lookup). Location
  precision (`fallback_level`) is preserved independently; only ownership is
  macro-sourced, marked, and capped at 0.3.
- Shared-library ambiguous cases (missing caller frame or disambiguation
  signal) keep `INCONCLUSIVE` as the verdict even with a CODEOWNERS hit —
  manufacturing defect-vs-misuse verdicts is precisely the false-culprit
  risk the dual-frame design exists to prevent. The owner is preserved as a
  lead (`alternatives` + `limitations`), never as a verdict.
- `ConclusionGate` untouched: a 0.3-capped result cannot pass the 0.8
  default threshold (proven by test).

**Scope of the fix.**

1. Add an optional `CODEOWNERS` (or equivalent ownership-file) tier between
   repository fallback and `UNATTRIBUTED`: when reached, return the owning
   team with a `macro-attribution` marker and capped confidence (e.g. ≤
   0.3), never a precise symbol-level claim.
2. Review the `INCONCLUSIVE` return sites in
   `FailureAttributionService.attribute_failure` against the principle
   "only `INCONCLUSIVE` when no repository profile or file mapping exists
   at all"; convert premature ones to the appropriate fallback tier with
   recorded limitations.
3. Keep `ConclusionGate` semantics unchanged: macro-attributed evidence must
   still pass provenance/freshness/confidence thresholds to influence a
   conclusion.

**Acceptance criteria.**

- Truncated stack traces / missing boundary payloads degrade through tiers
  instead of jumping to `INCONCLUSIVE` where ownership data exists.
- Macro-attributed results are visibly marked and confidence-capped.
- `INCONCLUSIVE` remains for genuinely unattributable inputs.

**Tests required.**

- Tier-walk test: node match → file → CODEOWNERS → repository, asserting
  recorded fallback level at each stage.
- Confidence-cap test: macro tier never exceeds the cap.
- Gate test: macro-attributed evidence still subject to `ConclusionGate`
  thresholds.

(All covered by `TestGracefulFallbackChain` / `TestCodeownersResolver` in
`tests/unit/test_topology_coverage.py`.)

---

## ISSUE-7 (medium, open): Neo4j adapter never resolves domain ownership

**Status:** Open — discovered while implementing ISSUE-6. The Neo4j
`resolve_source_location` returns no `ownership_path`, `domain_id`, or real
`snapshot_id` (nil UUID): nothing in any ingestion path writes `Domain` /
`GitOrganization` nodes or `BELONGS_TO` edges — only `Repository`,
`SourceFile`, `ASTNode`, and `TopologySnapshot` exist. The org-label
constraints exist but constrain nothing. Consequence: on Neo4j every result
currently depends on the CODEOWNERS tier for ownership; static ownership
only works on the in-memory adapter's seeded mappings.

**Scope of the fix (once org data has an authoritative source).**

1. Define where organizational ownership comes from (profile extension,
   CODEOWNERS-derived bootstrap import, HR/CMDB sync — pick one; do not
   hand-maintain Cypher).
2. Write `Domain`/`GitOrganization` nodes + `BELONGS_TO` edges during
   ingestion (both adapters for parity), with tenant-qualified MERGE keys
   matching the existing constraints.
3. Extend the Neo4j lookup to traverse the ownership chain and return the
   real path, domain, org, and snapshot ID instead of empty/nil values.
4. Add parity tests: same payload through both adapters yields the same
   ownership.

**Not in scope for this issue:** changing the fallback hierarchy or the
macro cap — those behave correctly given whatever ownership data exists.

---

## Resolved-1 (closed): Schema→graph ingestion gap

**Was incomplete.** `SchemaParser` (`infrastructure/evidence/schema/sql.py`)
existed but nothing called it; `add_database_schema_nodes` /
`link_code_to_tables` were uncallable module-level functions taking a phantom
`self`; `CodebaseGraphPipeline.build_repository_graph` built code-only graphs
with no path for database topology to enter.

**Fix applied.** Both helpers are now real `CodeSymbolGraphBuilder` methods
(`add_database_schema_nodes` is idempotent; `link_code_to_tables` resolves
exact-then-unambiguous-suffix and returns `bool`); the pipeline accepts
optional `ddl_paths` parsed via `SchemaParser`. Covered by
`TestCodeSchemaGraphIntegration` (9 tests) in
`tests/unit/test_infra_coverage.py`. Default pipeline behavior unchanged.

---

## Resolved-2 (closed): Last-night work repaired without behavior change

**Was incomplete.** Uncommitted work-in-progress broke `mypy` strict (10
errors): `Optional` attribute access in `schema/sql.py`, a duplicated
`content` declaration in `openai_adapter.py`, undefined `TableSchema` in
`graphify_adapter.py`, missing annotations in `pipeline.py` and the
`approve-action` endpoint (which also called nonexistent
`ctx.get_temporal_client()` and crashed with `AttributeError` when Temporal
was unwired).

**Fix applied.** Narrowed the `Optional`s (unknown types yield `"UNKNOWN"`,
unresolvable FK targets are skipped); removed the duplicate annotation;
`TableSchema` now resolves to the real schema model; annotations added; the
endpoint uses the file's existing `getattr(ctx, "temporal_client", None)`
pattern with 404/403/503 states mirroring sibling endpoints. Covered by 3
new regression tests in `tests/unit/test_remediation_coverage.py`. Full
gates green: `ruff check`, `ruff format --check`, `mypy` strict (140 files,
0 errors), `pytest` (285 passed), `bandit -ll`, `pip-audit`, `uv build`.
