# Knowledge layer operations

Audience: operators.

## Services

| Service | Container / process | Data | Disposable? |
|---|---|---|---|
| Postgres + pgvector | `postgres` (compose) | Envelopes (source of truth) + Mem0 vectors | Vectors yes, envelopes **no** |
| Neo4j 5.26 Community | `neo4j` (compose) | Graphiti episodes/edges + Layer 3 topology | Yes (rebuild from envelopes / re-ingest) |
| Mem0 projection | in-process library (API/worker) | none local | n/a |
| Graphiti projection | in-process via `temporal_port` | none local | n/a |

## Provisioning

- pgvector: migration `006_pgvector_extension` runs `CREATE EXTENSION IF
  NOT EXISTS vector`. Requires a superuser (or pre-provisioned image) at
  migrate time.
- Neo4j: `docker compose up neo4j`; first startup with knowledge or
  topology enabled runs index/constraint creation
  (`ensure_indices` / `install_constraints`). APOC plugin is enabled in
  compose for export procedures.

## Neo4j backup / restore (runbook stub)

Community Edition has no online backup. Procedure:

1. Stop writes: set `IAP_KNOWLEDGE_GRAPHITI_ENABLED=false` (and topology
   equivalent) and restart API/worker, **or** quiesce intake. Reads may continue.
2. `docker compose stop neo4j`.
3. Back up the `neodat` volume (`docker run --rm -v iap_neodat:/data -v
   $(pwd)/backup:/backup alpine tar czf /backup/neodat-$(date +%F).tgz -C / data`).
4. `docker compose start neo4j`.

Restore is the reverse (stop, replace volume contents, start). After any
restore, verify with the retention-classification smoke query in
`docs/dev/knowledge-artifacts.md` and re-run index creation.

## Rebuilding indexes from envelopes

Both projections are disposable:

- Mem0/pgvector: drop the `iap_memories` collection (or point
  `IAP_KNOWLEDGE_MEM0_COLLECTION` at a fresh name) and re-run capture for
  the desired investigations; `project_artifact` re-indexes.
- Graphiti: delete group(s) via a Neo4j shell (`MATCH (n) WHERE
  n.group_id IN [...] DETACH DELETE n` — tenant-scoped by construction of
  group IDs) and re-project episodes from envelopes.

## Tuning

- `IAP_KNOWLEDGE_GRAPHITI_SEMAPHORE_LIMIT` (default 5): concurrent Graphiti
  operations. Raise only with LLM rate-limit headroom; watch for 429s.
- `IAP_KNOWLEDGE_MAX_EPISODES` (default 50): per-investigation episode cap.
- Embedder dimensions must match `IAP_KNOWLEDGE_MEM0_DIMS` across
  re-indexes; changing embedder models requires a full collection rebuild.

## Retention (ISSUE-4)

Topology snapshot retention policy lives in the Part 5 design doc. The
cleanup job is the `TopologyRetentionWorkflow` (Temporal, see
`application/worker/retention.py`); schedule it via Temporal Schedules
(daily recommended). It deletes only unpinned, fully-ingested snapshots
and marks audit rows collected. Monitor `topology_retention_collected_total`
vs `topology_retention_pinned_total` (logged per run).

## Alerts worth having

- `excluded_stale_count` trending up per tenant (supersession janitor or
  reverify backlog falling behind).
- Graphiti/Neo4j connectivity failures at startup (fail-hard) vs runtime
  projection failures (degrade-to-envelopes, warning logs).
- pgvector extension missing at migrate time (superuser requirement).
