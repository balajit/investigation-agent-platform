# IAP Implementation Part 7 v1.0: Batch GitHub Ingest into Layer 3

Supersedes `docs/archive/IAP-implementation-part7-knowledge-v1.md` (plans in `docs/archive/` completed). Verified against code and live TENANT-1 data 2026-09-27. Living issues: `docs/IAP-issues-0927-wip.md`.

## Facts (current platform)

`scripts/ingest_github.py` (~900 lines, stdlib + pydantic, zero new deps) loads a GitHub org or single repo per tenant: paginated REST with `Link` following and 403/429 backoff; skip rules (archived/disabled/empty/fork/private-exclusion); per-repo SHA pin with `rev-parse` assertion; blobless single-branch C-`git` fetch (token scrubbed, remote URL reset); `register_identities` (org + repository + CODEOWNERS-or-unset domain, `--repo-type`/`--repo-types` validated); `build_payload_for_repo` (working-tree walk with skip-lists, 512 KB cap, MODULE-per-file, deterministic uuid5 IDs, macro-only CALLS/REFERENCES/ACCESSES_TABLE edges, hard caps fail-closed); `TopologyIngestionPort.ingest()` with transient-only retries; frozen `IngestManifestRow` JSONL (`LISTED/FETCHING/READY/FAILED/SKIPPED`) with real resume (READY skips, FAILED retries); exit codes 0/1/2. The loader knows nothing about Cognee. `scripts/start-platform.sh` brings up compose, waits (postgres/Neo4j/Temporal), migrates, and launches API + worker with fail-hard topology requirement. Tests: 42 loader unit tests, container-backed Neo4j contract tests, shared fixtures (`acme--payments`, `acme--billing`). Live proof: TENANT-1 — 47 repos, 87,991 nodes, 10,409 files, 137,220 CALLS, 352 ACCESSES_TABLE/163 tables, 0 FAILED.

## Decisions kept

Single-process operator CLI (no Temporal/Kafka/endpoints); SHA pinning as the only revision truth; ownership never manufactured; idempotent replay by payload hash; manifest as the audit trail.

## Future extensions

- **Manageability**: GitHub App + scheduled delta sync on second-org/weekly-trigger; `--repo-types` mapping file support for large orgs.
- **Scalability**: multi-process repo sharding past the 2-hour operator window (never threads — tree-sitter lock); per-repo concurrency caps.
- **Performance**: full-clone mode only for offline parses; blobless default keeps fetch incremental; `payload_hash`-indexed skip lists for monorepo-scale runs.
