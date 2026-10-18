# Addendum to Part 8 v1.0: Cognee Read Path (Port-Level Retrieval)

Status: design approved for implementation. Parent: `docs/IAP-implementation-part8-knowledge-v1.0.md` (pointer added there). Implements WIP §1.2.

## Problem

The pilot writes (`project_snapshot`, `drop_projection`) and owns a gated retrieval primitive (`search_symbols`), but no `DomainAttributionPort` implementation reads Cognee data — attribution reads stay on the Neo4j adapter by default, and the two projected points gaps block a real comparison: points carry empty `ownership_path`, and there is no specified composition order between the two readers.

## Design

### 1. Populate ownership at project time

Extend `project_snapshot(payload, ownership_path: list[str] | None = None)`; the pilot script passes the path from the loader's `register_identities` result (org + domain, empty when unresolvable — never manufactured). Points store it as a plain property. Existing projections with empty paths remain valid; they simply join no ownership until re-projected. No re-ingest of Layer 3 is required — projection is independent of snapshots.

### 2. `CogneeAttributionReader` implements the port read contract

New reader class in `cognee_adapter.py` implementing `DomainAttributionPort.resolve_source_location` with the exact port semantics:

- Query Cognee with retrieval-only types (`CYPHER` primary, `CHUNKS_LEXICAL` fallback) constrained to the opaque dataset for `(tenant, repo, revision)`; `assert_retrieval_only` + `ALLOW_COMPLETION` gates unchanged.
- Filter to `tenant_id`/`repository_id`/`revision` in this layer before ranking (namespaces never trusted); resolve against READY projections only (track projected revisions in the adapter; unprojected → `TOPOLOGY_SNAPSHOT_NOT_READY`).
- Select the most specific enclosing point by smallest line range, then stable `node_id` — mirroring the Neo4j adapter's deterministic tie-break, including the ambiguity raise.
- Return `StaticOwnershipResult` with the point's stored ownership path, `snapshot_id` of the projected snapshot, and `fallback_level=AST_NODE` (or `REPOSITORY` when only file-level points match).

### 3. Composition order (production stays Neo4j until verdict)

The reader never replaces the Neo4j adapter. For the benchmark comparison both are queried and their `StaticOwnershipResult`s diffed field-by-field; in production the Neo4j result remains authoritative and the Cognee result is logged alongside (enrichment telemetry, never evidence) until the verdict flips it per-application with quotas. Gateway routing applies identically — the reader is called through `DomainAttributionPort` like any adapter.

### 4. Benchmark method and verdict criteria

Extend `tests/benchmarks/test_topology_enrichment.py` with multi-hop/impact cases (call-chain traversal, blast-radius listing) alongside the existing exact-ownership cases, which keep their higher weight. Verdict to keep: statistically significant multi-hop wins **with zero exact-attribution regression** across the pilot window, plus operator sign-off. Otherwise the removal trigger fires: delete the adapter module plus pilot labels, Layer 3 untouched.

## Tests

- Ownership population: project with a path, read back point properties, assert path present; project without, assert empty (not absent).
- Read parity: fixture repo projected to both stores; same file+line queries return the same qualified symbol from both readers.
- Ambiguity parity: tied enclosing points raise `TopologyAmbiguousMatchError` like the Neo4j path.
- Tenant isolation: swapped-tenant reads return nothing; dataset IDs reveal no tenant/repo strings.
- Completion containment (already covered, kept): every `*_COMPLETION` type raises before network.

## Rollout

Reader lands default-off behind `IAP_COGNEE_ENABLED`; benchmark comparison runs offline against the pilot projection; no production traffic touches the reader before the verdict. `drop_projection` already deletes points; extend it to the reader's revision tracking in the same change.
