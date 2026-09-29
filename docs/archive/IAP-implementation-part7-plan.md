# Plan: Implement Part 7 Batch GitHub Ingest (Neo4j end-to-end, full edges, real registry)

## Decisions locked

- First slice targets Neo4j end-to-end (not in-memory only).
- Full edges in first slice (CALLS + references + DB-access, not symbols-only).
- Real registry writes (RepositoryRegistryPort + CODEOWNERS derivation per D4).
- Live smoke test with operator-provided GH_TOKEN + target org/repo.

## Starting point

`scripts/ingest_github.py` already implements D1–D3 scaffolding (REST listing, blobless SHA-pinned fetch, skip-lists, per-repo containment, manifest JSONL). Gaps: payload is a dict stub, `ingest()` never called, registration skipped, `--resume` is a stub print, manifest lacks FETCHING/SKIPPED states.

## Phase 0 — Test harness + fixtures

Add `tests/unit/test_ingest_github.py` (synthetic fixture repo: Python + JS, CODEOWNERS catch-all, oversized file, binary) and `tests/integration/test_ingest_neo4j.py` (compose Neo4j: READY, idempotent replay, tenant isolation). Acceptance: unit green; integration green locally.

## Phase 1 — Real payload (D5 + D6 input)

`build_payload_for_repo -> ASTTopologyPayload`: CodeSymbol to `ASTNodeIdentity.create` (uuid5, TopologyNodeType, parent linkage, signature_hash), MODULE per file + `SourceFileIdentity` with content_hash, Call/Reference/DatabaseAccess edges (RESOLVED only when both endpoints macro-tier same-payload), macro/micro split, all three caps with truncated + FAILED on breach, SHA-256 payload_hash, real parser_version. Promote manifest dict to frozen `IngestManifestRow`. Acceptance: fixture validates; oversized yields truncated=true.

## Phase 2 — Real registration (D4)

`register_identities()` between fetch and parse: derive_git_organization to upsert GitOrganizationIdentity; RepositoryIdentity (stable id, --repo-type + --repo-types mapping) to register_repository; derive_repository_domain to set domain_id only when resolvable. Acceptance: catch-all sets domain, missing CODEOWNERS leaves unset, re-runs stable.

## Phase 3 — Real ingest (D6)

`TopologyIngestionPort.ingest(payload)` via TopologyConfig-built adapter (Neo4j when enabled, in-memory otherwise); 3 bounded retries on TOPOLOGY_PROVIDER_UNAVAILABLE only; result counts into manifest. Acceptance: fixture ingests, replay stable, cross-tenant invisible.

## Phase 4 — Manifest + resume (D7)

FETCHING rows pre-fetch, SKIPPED rows with reasons, real --resume (skip READY, retry FAILED, new SHAs as new snapshots), 429/403 backoff honoring retry-after (max 5). Acceptance: kill-and-resume zero re-fetch of READY.

## Phase 5 — Start script + live smoke + docs

Temporal :7233 wait loop, IAP_TOPOLOGY_REQUIRED fail-hard, smoke sequence (dry-run to max-repos 2 to full target) on one manifest path, "Batch onboarding" section appended to docs/dev/topology.md. Acceptance: live manifest complete with zero silent drops; no new tables or API surface.

## Out of scope

No Temporal ingest workflow, Kafka, multi-branch mining, GitHub App flow, Mem0/Graphiti writes, or parallel-repo ingest (deferred with triggers).

## Order

P0 to P1 to P2 to P3 to P4 to P5, sequential. P5 needs GH_TOKEN + target + tenant.
