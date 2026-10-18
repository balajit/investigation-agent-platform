# IAP Implementation Part 5 v1.0: Context Layers and Domain Attribution

Supersedes `docs/archive/IAP-implemenation-part5-v1.md`. Verified against code 2026-09-27. Living issues: `docs/IAP-issues-0927-wip.md`.

## Facts (current platform)

Layer 3 is a tenant-owned, revision-pinned static-code topology bounded context. Identities (`domain/topology/models.py`): `DomainIdentity`, `GitOrganizationIdentity`, `RepositoryIdentity` (+`RepositoryType`), `PackageIdentity`, `SourceFileIdentity`, deterministic `ASTNodeIdentity` (uuid5 over tenant/repo/revision/path/symbol/range), edge inputs (`CallEdgeInput`, `ReferenceEdgeInput`, `DatabaseAccessEdgeInput`), `ASTTopologyPayload` (caps 20k files / 200k nodes+edges, tenant/revision validators), snapshot lifecycle `PENDING → INGESTING → READY | FAILED` plus `SUPERSEDED`/`COLLECTED` retention states, `MACRO_NODE_TYPES` split with on-demand micro resolution (`MicroSymbolResolver`), `StaticOwnershipResult`, `DomainAttributionResult` (rule `v2`, `resolution_tier`, confidence-capped CODEOWNERS tier at 0.3 vs 0.80 gate).

One shared Neo4j 5.26 Community database holds Layer 3 labels, Graphiti groups, and Cognee points under separate namespaces, with tenant-qualified uniqueness constraints installed as an explicit admin step. Adapters: `InMemoryTopologyAdapter` (dev/test) and `Neo4jTopologyAdapter` (production; writes nodes, CALLS, REFERENCES, and ACCESSES_TABLE-to-`DatabaseTable` edges). Services: `FailureAttributionService` (dual-frame, fail-closed, trace-hop resolver injectable but unwired in production), `classify_snapshots` pure retention policy, evidence-backed pinned revisions, `TopologySnapshotRetentionWorkflow` + janitor activity. Reads route through the evidence gateway into provenanced evidence. Live proof: TENANT-1 holds 47 repos / 87,991 AST nodes with AST_NODE attribution resolving against real snapshot IDs.

Not built (recorded, not forgotten): `ContextOrchestrator` + unified budget, named L1/L2 ports (Part 6 ports cover the roles), gateway composition into `AppContext` (open issue — hops stay dormant until then).

## Decisions kept

Authority vs projection (stores never invent validity); tenant identity server-derived only; immutable revisions; `INCONCLUSIVE` over false culprits; one topology authority at a time.

## Future extensions

- **Manageability**: orchestrator + unified budget when three or more enrichment adapters are selectable; per-dataset quotas at the application layer.
- **Scalability**: Enterprise/Aura when per-dataset isolation or noisy-neighbor pain demands it (documented trigger); read-replica or second Neo4j for attribution reads under load.
- **Performance**: bounded `UNWIND` batches already; add query-plan monitoring on hot ownership lookups; CODEOWNERS-index caching per revision.
