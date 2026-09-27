# Issues: Parts 7 + 8 Future Work (Deferred Triggers, Debt, Follow-ups)

This file tracks everything deliberately left out of the combined plan in `docs/IAP-implementation-part7-8-plan.md`. Nothing here blocks the sequential run. Each item carries its adoption trigger or fix condition — do not implement without the trigger firing.

## Deferred adoption triggers (from Part 7 design)

### GitHub App + scheduled sync

- Status: deferred. PAT rotation becomes toil.
- Trigger: second org onboarded or weekly re-ingest requested.
- Work: App issuance flow + cron/Temporal schedule that re-lists, pins new SHAs, and ingests deltas from the manifest.

### Full-history / multi-branch mining

- Status: deferred. One pinned SHA per repo is enough today.
- Trigger: `TOPOLOGY_SNAPSHOT_NOT_READY` on a non-default revision in a real investigation.
- Work: per-repo multi-SHA manifests plus retention-window planning.

### Parallel-repo ingest

- Status: deferred. Single-process throughput is unmeasured.
- Trigger: p95 org ingest exceeds a 2-hour operator window on the runner.
- Work: shard by repo across processes (never threads, per tree-sitter `PARSE_LOCK`), one port client per shard.

## Deferred adoption triggers (from Part 8 design)

### Full Cognee adoption as enrichment standard

- Status: deferred until pilot verdict.
- Trigger: Slice 1 shows statistically significant multi-hop/impact-query wins with zero exact-attribution regression across the pilot window, plus operator sign-off.
- Work: enable per-application with quotas.

### Cognee vector indexing

- Status: deferred. Lexical/CYPHER retrieval is the starting point.
- Trigger: benchmark failure analysis names retrieval (not coverage) as the cause.
- Work: Fastembed-local embeddings only; still no LLM key requirement.

### FalkorDB backend

- Status: deferred indefinitely for platform-operated paths (SSPLv1 service trigger + community-only adapter).
- Trigger: written licensing clearance plus core-tier adapter support.
- Note: self-hosted customer deployments may revisit sooner.

### Cross-repository same-dataset paths

- Status: deferred.
- Trigger: `INCONCLUSIVE` attributions on ISSUE-5 trace hops where the missing link is enrichment coverage, not evidence.
- Work: dedicated cross-repo dataset policy with its own threat review.

### Cognee multi-user mode and per-dataset databases

- Status: deferred.
- Trigger: noisy-neighbor pain or an isolation audit finding.
- Work: evaluate Enterprise/Aura against a documented cost comparison.

## Known tech debt (discovered during gap analysis)

### `CodebaseGraphPipeline` single-language builds

- `pipeline.py: build_repository_graph` takes one `language` with an `ext_map` covering only python/javascript/typescript. The Part 7 loader needs the 12-grammar priority set per repo, not one language per run.
- Fix condition: Phase 1 payload work should bypass or extend this (multi-language walk in the loader), not patch the pipeline signature mid-run. Revisit pipeline unification after Track A.

### Unbounded `asyncio.gather` in pipeline file processing

- `pipeline.py: _process_file` fans out over all files with no semaphore — FD and memory blowup on large repos.
- Fix condition: loader-side parsing (Phase 1) uses bounded concurrency from day one; fix the pipeline itself when it is next used for bulk work.

### Graphify node IDs collide on overloads and history

- `graphify_adapter.py: node_id = file::parent.name` — overloaded, nested same-name, and cross-revision symbols collide. Layer 3 uses `ASTNodeIdentity` uuid5 and must remain the identity authority.
- Fix condition: do not fix Graphify IDs in this run; keep the boundary (Graphify for ephemeral analysis, topology models for durable identity). Revisit only if Graphify artifacts become durable.

### Manifest `recorded_at` extra field

- The loader's manifest row carries `recorded_at`, which is not part of the frozen `IngestManifestRow` contract. Harmless operationally but diverges from the design.
- Fix condition: Phase 1 promotion decides — either drop the field or document it as driver-local metadata outside the contract.

### `scripts/setup.py` is stale scaffolding

- One-time repo scaffolding, not an operator loop. Left untouched by both parts; ingest must not be wired into it.
- Fix condition: none planned. Flag for deletion in the consolidation pass noted in `REVIEW_GAPS.md`.

## Operational follow-ups

### Shared-Neo4j third-tenant accounting

- Cognee pilot projections will join Graphiti groups and Layer 3 labels in one Community database. Per-dataset quotas stay application-layer until pain proves otherwise.
- Trigger: sustained noisy-neighbor symptoms — then run the Enterprise/Aura cost comparison covering all three consumers.

### pygit2 GPL-2.0-only review before hosted packaging

- Confined to local read-only traversal; no action for internal tooling.
- Trigger: any hosted-product packaging change — get legal review first.

### `docs/dev/architecture.md` still missing

- Parts 5–8 each note sections owed to this file (topology bounded context, batch-loader swimlane, Cognee dashed optional branch).
- Fix condition: create the file after Track A lands, covering all three sections at once rather than piecemeal.

## Live smoke test requirements (needed at Phase 5)

- Operator provides: GH_TOKEN (fine-grained PAT, Metadata:read minimum), target org or owner/repo, tenant ID to ingest under.
- Without these, Phase 5 stops after script hardening and docs; smoke becomes its own follow-up run.
- Pilot live comparison additionally needs `cognee` dependency approval plus Neo4j; until then the pilot verdict is "scaffolded behind flag, unevaluated".

## Pilot verdict (TENANT-1 live run, 2026-09-27)

- KEEP as enrichment path. `offermgmt-service` (5,153 symbols) projected 5,153/5,153 `TopologySymbolPoint` nodes into the shared Neo4j, tenant-scoped, label-separated from Layer 3, graph-only (no embeddings, no LLM calls), replay-idempotent.
- Tenant partition: 47 repositories, 2 git orgs, 87,991 AST nodes, 10,409 files, 47 snapshots, 0 FAILED across 3 named repos + 46-repo org.
- Next if kept: edge projection (CALLS/REFERENCES via `custom_edges` or DataPoint edge fields), gateway-routed enrichment reads, janitor `drop_projection` wiring, benchmark comparison report on multi-hop queries.
- `cognee` 1.6.1 pinned in `pyproject.toml` (`cognee[neo4j]` extra); all imports isolated to `cognee_adapter.py` + `scripts/cognee_pilot.py`.

## Pre-existing failure (not introduced by this run)

- `tests/unit/test_infra_coverage.py::TestBootstrap::test_build_app_context_production` fails on clean HEAD (verified via stash: fails with and without the `TopologyConfig` change). Left untouched as out of scope.
