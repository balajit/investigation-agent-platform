# Topology ops (Layer 3)

Audience: developers and operators. Authority for topology behavior is `docs/IAP-implemenation-part5-v1.md`; batch onboarding rationale is `docs/IAP-implementation-part7-knowledge-v1.md`.

## Batch onboarding

Load a GitHub organization or a single repository into Layer 3 with the Part 7 loader. It lists, SHA-pins, blobless-fetches, registers identities, parses with Tree-sitter, and ingests one `ASTTopologyPayload` per repo revision.

Token scope: fine-grained PAT with `Metadata:read` minimum (`GH_TOKEN` or `GITHUB_TOKEN` from environment only; never logged or written to the manifest).

Smoke sequence (each step appends to one manifest path for continuity):

```bash
uv run python scripts/ingest_github.py --org <login> --tenant-id <t> --dry-run
uv run python scripts/ingest_github.py --org <login> --tenant-id <t> --max-repos 2
uv run python scripts/ingest_github.py --org <login> --tenant-id <t>
```

Single repo, per-repo types, resume, and full clone:

```bash
uv run python scripts/ingest_github.py --repo <owner/name> --tenant-id <t>
uv run python scripts/ingest_github.py --org <login> --tenant-id <t> --repo-types <owner/name>=SERVICE
uv run python scripts/ingest_github.py --org <login> --tenant-id <t> --resume ./tmp/ingest-<org>-<ts>.jsonl
uv run python scripts/ingest_github.py --org <login> --tenant-id <t> --full-clone
```

Resume semantics: `READY` SHAs skip fetch+parse entirely; `FAILED` rows retry; new SHAs become new snapshots (retention collects old ones, never the loader). Manifest rows move `LISTED` to `FETCHING` to `READY`/`FAILED`, with `SKIPPED` (+ reason) for archived/disabled/empty/fork repos.

`--full-clone` trigger: fully-offline parses or multi-revision analysis. Shallow and treeless clones are refused by the driver.

## Platform start order

`scripts/start-platform.sh` runs compose (postgres, neo4j, temporal, elasticsearch, kafka), waits for readiness (postgres, Neo4j, Temporal UI), runs `alembic upgrade head`, then launches the API and the Temporal worker with PID files under `tmp/`. With `IAP_TOPOLOGY_REQUIRED=true` it refuses to start when Neo4j is unreachable (fail-hard, never silent degrade).

## Cognee pilot (Part 8)

Optional enrichment only — the Part 7 loader knows nothing about Cognee; this script reuses its payload builder as a library. Our models are the universe: AST flows one way, platform → Cognee.

Enable and run one repo (graph-only writes, no vectors, no LLM calls):

```bash
export IAP_COGNEE_ENABLED=true IAP_COGNEE_DATASET_SALT=<salt>
export GRAPH_DATABASE_PROVIDER=neo4j GRAPH_DATABASE_URL=bolt://localhost:7687
export GRAPH_DATABASE_USERNAME=neo4j GRAPH_DATABASE_PASSWORD=<neo4j-password>
uv run python scripts/cognee_pilot.py --repo <owner/name> --tenant-id <t>
```

Offline against an existing checkout, or drop a projection (retention path):

```bash
uv run python scripts/cognee_pilot.py --checkout ./tmp/repos/<slug> --tenant-id <t> --repository-id <rid> --revision <sha>
uv run python scripts/cognee_pilot.py --drop --tenant-id <t> --repository-id <rid> --revision <sha>
```

Read the benchmark comparison (`tests/benchmarks/test_topology_enrichment.py` baselines vs pilot point/edge counts and replay hashes), then keep or remove per the verdict in `docs/IAP-implementation-part7-8-issues.md`. Projections are `TopologySymbolPoint` nodes in the shared Neo4j, tenant-scoped and label-separated from Layer 3. Retrieval is restricted to `CHUNKS/SUMMARIES/CHUNKS_LEXICAL/CYPHER/CODE` (`ALLOW_COMPLETION=false`); completion types raise before any network call.
