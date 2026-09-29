# IAP Implementation Part 8 v1.0: Optional Cognee Enrichment

Supersedes `docs/archive/IAP-implementation-part8-knowledge-v1.md` (rejected-proposal analysis preserved in archive). Verified against code and live data 2026-09-27. Living issues: `docs/IAP-issues-0927-wip.md`.

## Facts (current platform)

Cognee (`cognee[neo4j]` 1.6.1, pinned; imports isolated to adapter + pilot script) is enrichment-only behind `IAP_COGNEE_ENABLED=false` default. Our models are the universe: `ASTTopologyPayload` maps deterministically to `TopologySymbolPoint` nodes plus `(Edge, target)` tuples expanding to exact-label CALLS/REFERENCES edges; writes go through `add_data_points(graph_only=True)` — no vectors, no embedder, no LLM calls, never `add()`/`cognify()` on AST data. Tenancy enforced in our layer (opaque salted dataset IDs, tenant-filtered reads; namespaces never trusted). Reads restricted to `CHUNKS/SUMMARIES/CHUNKS_LEXICAL/CYPHER/CODE` via `search_symbols`, gated twice (`assert_retrieval_only` + non-overridable `ALLOW_COMPLETION=false`); attribution reads stay on the Neo4j adapter until verdict. `drop_projection` deletes tenant+revision-scoped points for the janitor. `scripts/cognee_pilot.py` (`--repo/--checkout/--drop`) reuses the Part 7 builder; Part 7 knows nothing about Cognee. Benchmark harness (`tests/benchmarks/`) records exact-ownership baselines the pilot compares against. Live proof: `offermgmt-service` projected 5,153/5,153 points with replay-identical hashes. Verdict recorded: KEEP as enrichment path.

Addendum: the port-level read path (ownership population at project time, `CogneeAttributionReader`, composition order, benchmark method, verdict criteria) is designed in `docs/IAP-implementation-part8-knowledge-v1.0-addendum.md` — the remaining implementation for WIP §1.2.

## Decisions kept

IAP parsers stay the AST authority (Cognee never re-parses); one topology authority (Neo4j adapter); no new stateful service (shared Neo4j, Cognee-labeled nodes); FalkorDB deferred (SSPL + community adapter); vectors deferred to a measured recall gap.

## Future extensions

- **Manageability**: port-level read wiring (`DomainAttributionPort` via the retrieval primitive) post-verdict; janitor `drop_projection` hookup; benchmark comparison report on multi-hop queries.
- **Scalability**: per-dataset quotas; Enterprise/Aura evaluation shared with Layer 3/Graphiti triggers.
- **Performance**: Fastembed-local vectors only if lexical/CYPHER retrieval is proven the bottleneck; `custom_edges` batch tuning for large repos.
