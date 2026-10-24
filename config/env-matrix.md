# IAP environment matrix (per-env contract, key names only)

Source of truth for every `IAP_*` variable read in `src/` (see `infrastructure/configuration/config.py` builders). Re-verify on every config change and on every release branch cut; any new `os.environ.get("IAP_*")` or secret-manager lookup must add a row before merge. Secrets are key names only — values come from runtime injection or a secret manager, never from committed files (TO_DO-001/002). Legend: `R` required, `O` optional, `—` not applicable; `secret source` is `runtime-env` unless the `secret://` backend lands (TO_DO-002).

| variable | development | test | staging | production | secret? | secret source | fail-closed? |
|---|---|---|---|---|---|---|---|
| IAP_ENVIRONMENT | O (`development`) | O (`test`) | R | R (`production`) | no | — | yes (helper-default mismatch, TO_DO-005) |
| IAP_APP_ID | O | O | O | O | no | — | no |
| IAP_DATABASE_URI | R | R | R | R | yes | runtime-env | yes |
| IAP_DB_POOL_SIZE | O (`10`) | O | O | O | no | — | no |
| IAP_DB_MAX_OVERFLOW | O (`20`) | O | O | O | no | — | no |
| IAP_LLM_PROVIDER | O (`openai`) | O | O | R | no | — | yes (selects key var) |
| IAP_LLM_TEMPERATURE | O (`0.0`) | O | O | O | no | — | no |
| IAP_LLM_MAX_TOKENS | O (`4096`) | O | O | O | no | — | no |
| IAP_OPENAI_API_KEY | R if provider=openai | R if openai | R if openai | R if openai | yes | runtime-env | yes |
| IAP_OPENAI_MODEL | O (`gpt-4o`) | O | O | O | no | — | no |
| IAP_OPENAI_ENDPOINT | O | O | O | O | no | — | no |
| IAP_ANTHROPIC_API_KEY | R if provider=anthropic | R if anthropic | R if anthropic | R if anthropic | yes | runtime-env | yes |
| IAP_ANTHROPIC_MODEL | O | O | O | O | no | — | no |
| IAP_AZURE_API_KEY | R if provider=azure | R if azure | R if azure | R if azure | yes | runtime-env | yes |
| IAP_AZURE_MODEL | O | O | O | O | no | — | no |
| IAP_AZURE_ENDPOINT | R if azure | R if azure | R if azure | R if azure | no | — | yes |
| IAP_AZURE_DEPLOYMENT | R if azure | R if azure | R if azure | R if azure | no | — | yes |
| IAP_AZURE_API_VERSION | O | O | O | O | no | — | no |
| IAP_AZURE_EMBEDDING_DEPLOYMENT | O | O | O | O | no | — | no |
| IAP_AZURE_EMBEDDING_ENDPOINT | O | O | O | O | no | — | no |
| IAP_AZURE_EMBEDDING_API_VERSION | O | O | O | O | no | — | no |
| IAP_AZURE_EMBEDDING_API_KEY | O | O | O | O | yes | runtime-env | no |
| IAP_MUSE_API_KEY | R if provider=muse | R if muse | R if muse | R if muse | yes | runtime-env | yes |
| IAP_MUSE_MODEL | O | O | O | O | no | — | no |
| IAP_MUSE_ENDPOINT | R if muse | R if muse | R if muse | R if muse | no | — | yes |
| IAP_MAX_TOOL_CALLS | O (`50`) | O | O | O | no | — | no |
| IAP_MAX_REASONING_CALLS | O (`20`) | O | O | O | no | — | no |
| IAP_MAX_DURATION_SECONDS | O (`1800`) | O | O | O | no | — | no |
| IAP_TEMPORAL_HOST | O (`localhost:7233`) | O | R | R | no | — | no (best-effort dev) |
| IAP_TEMPORAL_NAMESPACE | O (`default`) | O | O | O | no | — | no |
| IAP_TEMPORAL_TASK_QUEUE | O | O | O | O | no | — | no |
| IAP_TEMPORAL_INDEXING_QUEUE | O | O | O | O | no | — | no |
| IAP_TEMPORAL_ANALYTICS_QUEUE | O | O | O | O | no | — | no |
| IAP_TEMPORAL_MAX_ACTIVITIES | O (`100`) | O | O | O | no | — | no |
| IAP_TEMPORAL_MAX_WORKFLOWS | O (`50`) | O | O | O | no | — | no |
| IAP_KAFKA_SERVERS | O (`localhost:9092`) | O | R | R | no | — | no (dev skips quietly) |
| IAP_KAFKA_TOPIC_PREFIX | O | O | O | O | no | — | no |
| IAP_KAFKA_GROUP | O | O | O | O | no | — | no |
| IAP_QUOTA_MAX_CONCURRENT_INV | O (`10`) | O | O | O | no | — | no |
| IAP_QUOTA_MAX_CONCURRENT_JOBS | O (`5`) | O | O | O | no | — | no |
| IAP_QUOTA_MAX_QUEUED_JOBS | O (`50`) | O | O | O | no | — | no |
| IAP_QUOTA_MAX_BATCH_RECORDS | O (`500`) | O | O | O | no | — | no |
| IAP_QUOTA_MAX_REF_SOURCE_BYTES | O | O | O | O | no | — | no |
| IAP_QUOTA_MAX_REF_FILES | O | O | O | O | no | — | no |
| IAP_QUOTA_MAX_VECTOR_ROWS | O | O | O | O | no | — | no |
| IAP_QUOTA_MAX_LLM_TOKENS_DAY | O | O | O | O | no | — | no |
| IAP_QUOTA_MAX_CHAT_SESSIONS | O (`100`) | O | O | O | no | — | no |
| IAP_QUOTA_MAX_STREAMS | O (`100`) | O | O | O | no | — | no |
| IAP_QUOTA_MAX_REPORT_HISTORY | O (`50`) | O | O | O | no | — | no |
| IAP_QUOTA_MAX_ARTIFACT_BYTES | O | O | O | O | no | — | no |
| IAP_STORAGE_ENDPOINT | O | O | R | R | no | — | no |
| IAP_STORAGE_BUCKET | O | O | O | O | no | — | no |
| IAP_STORAGE_REGION | O | O | O | O | no | — | no |
| IAP_STORAGE_ACCESS_KEY | O | O | R | R | yes | runtime-env | no |
| IAP_STORAGE_SECRET_KEY | O | O | R | R | yes | runtime-env | no |
| IAP_TELEMETRY_SERVICE | O | O | O | O | no | — | no |
| IAP_OTLP_ENDPOINT | O | O | O | O | no | — | no |
| IAP_TELEMETRY_ENABLED | O (`true`) | O | O | O | no | — | no |
| IAP_LOG_FILE | O (`data/iap.log`) | O | O | O | no | — | no |
| IAP_LOG_LEVEL | O (`INFO`) | O | O | O | no | — | no |
| IAP_TOPOLOGY_ENABLED | O (`false`) | O | O | O | no | — | no |
| IAP_TOPOLOGY_REQUIRED | O (`false`) | O | O | O | no | — | yes (when true, missing wiring halts) |
| IAP_TOPOLOGY_NEO4J_URI | O | O | R if enabled | R if enabled | yes | runtime-env | no |
| IAP_TOPOLOGY_NEO4J_USERNAME | O | O | R if enabled | R if enabled | yes | runtime-env | no |
| IAP_TOPOLOGY_NEO4J_PASSWORD | O | O | R if enabled | R if enabled | yes | runtime-env | no |
| IAP_TOPOLOGY_NEO4J_DATABASE | O (`neo4j`) | O | O | O | no | — | no |
| IAP_TOPOLOGY_NEO4J_ENCRYPTED | O (`true`) | O | O | O | no | — | no |
| IAP_TOPOLOGY_CONNECTION_TIMEOUT_SECONDS | O | O | O | O | no | — | no |
| IAP_TOPOLOGY_QUERY_TIMEOUT_SECONDS | O | O | O | O | no | — | no |
| IAP_TOPOLOGY_MAX_POOL_SIZE | O | O | O | O | no | — | no |
| IAP_TOPOLOGY_BATCH_SIZE | O | O | O | O | no | — | no |
| IAP_TOPOLOGY_MAX_LOOKUP_NODES | O | O | O | O | no | — | no |
| IAP_TOPOLOGY_MAX_LOOKUP_EDGES | O | O | O | O | no | — | no |
| IAP_CODE_REPO_BASE | O (`""`, disables file tiers) | O | O | O | no | — | no |
| IAP_TOPOLOGY_RETENTION_ENABLED | O (`false`) | O | O | O | no | — | no |
| IAP_TOPOLOGY_RETENTION_WINDOW_DAYS | O (`30`) | O | O | O | no | — | no |
| IAP_TOPOLOGY_RETENTION_MAX_SNAPSHOTS | O (`20`) | O | O | O | no | — | no |
| IAP_COGNEE_ENABLED | O (`false`) | O | O | O | no | — | no |
| IAP_COGNEE_DATASET_SALT | O | O | R if cognee | R if cognee | yes | runtime-env | no |
| IAP_KNOWLEDGE_ENABLED | O (`true`) | O | O | O | no | — | no |
| IAP_KNOWLEDGE_MAX_REVERIFY_ATTEMPTS | O (`3`) | O | O | O | no | — | no |
| IAP_KNOWLEDGE_MAX_EPISODES | O (`50`) | O | O | O | no | — | no |
| IAP_KNOWLEDGE_EVIDENCE_TTL_DAYS | O (`90`) | O | O | O | no | — | no |
| IAP_WP_A3_MAX_ATTEMPTS | O (`3`) | O | O | O | no | — | no (recorded as `over_budget` only, never enforced; read directly via `os.environ`, not the config builders) |
| IAP_KNOWLEDGE_MAX_EPISODE_BYTES | O (`8192`) | O | O | O | no | — | no |
| IAP_KNOWLEDGE_MEM0_ENABLED | O (`false`) | O | O | O | no | — | no |
| IAP_KNOWLEDGE_MEM0_MODEL | O | O | O | O | no | — | no |
| IAP_KNOWLEDGE_MEM0_EMBEDDER | O | O | O | O | no | — | no |
| IAP_KNOWLEDGE_MEM0_DIMS | O | O | O | O | no | — | no |
| IAP_KNOWLEDGE_MEM0_COLLECTION | O | O | O | O | no | — | no |
| IAP_KNOWLEDGE_MEM0_PGVECTOR_URL | O | O | R if mem0 | R if mem0 | yes | runtime-env | no |
| IAP_KNOWLEDGE_GRAPHITI_MODEL | O | O | O | O | no | — | no |
| IAP_KNOWLEDGE_GRAPHITI_EMBEDDER | O | O | O | O | no | — | no |
| IAP_KNOWLEDGE_GRAPHITI_NEO4J_URI | O | O | R if graphiti | R if graphiti | yes | runtime-env | no |
| IAP_KNOWLEDGE_GRAPHITI_NEO4J_USER | O | O | R if graphiti | R if graphiti | yes | runtime-env | no |
| IAP_KNOWLEDGE_GRAPHITI_NEO4J_PASSWORD | O | O | R if graphiti | R if graphiti | yes | runtime-env | no |
| IAP_KNOWLEDGE_GRAPHITI_NEO4J_DATABASE | O (`neo4j`) | O | O | O | no | — | no |
| IAP_KNOWLEDGE_GRAPHITI_SEMAPHORE_LIMIT | O (`5`) | O | O | O | no | — | no |
| IAP_KNOWLEDGE_GRAPHITI_ENABLED | O (`false`) | O | O | O | no | — | no |
| IAP_DISPOSITION_RETENTION_DAYS | O (`30`) | O | O | O | no | — | no |
| IAP_DISPOSITION_SIMILARITY_THRESHOLD | O (`0.4`) | O | O | O | no | — | no |
| IAP_DISPOSITION_MAX_CANDIDATES | O (`25`) | O | O | O | no | — | no |
| IAP_DISPOSITION_LOG_RETENTION_DAYS | O (`30`) | O | O | O | no | — | no |
| IAP_MCP_LOGS_DB_URL | O (`http://localhost:8100/sse`, enhance-logs script) | O | O | O | no | — | no |
| IAP_KNOWLEDGE_CACHE_ENABLED | O (`false`) | O | O | O | no | — | no |
| IAP_KNOWLEDGE_CACHE_TTL_SECONDS | O (`120`, `0` disables store) | O | O | O | no | — | no |
| IAP_REDIS_URL | O (empty → in-memory backend; compose `redis` in dev, managed endpoint in cloud) | O | O | O | no | runtime-env | no |
| IAP_MCP_FANOUT_CONCURRENCY | O (empty/unbounded; `1` restores serial fan-out) | O | O | O | no | — | no |
| IAP_COMPILER_FULL_EXCERPT_RECENT | O (`5`) | O | O | O | no | — | no |
| IAP_COMPILER_FULL_EXCERPT_RELEVANCE | O (`0.8`) | O | O | O | no | — | no |
| IAP_COMPILER_LOG_GROUP | O (on; `0`/`false` restores per-row excerpts) | O | O | O | no | — | no |
| IAP_ACCESS_LOG_BODY | O (`true`; `false` = summary fields only) | O | O | O | no | — | no |
| IAP_ACCESS_LOG_BODY_CHARS | O (`4096`) | O | O | O | no | — | no |
| IAP_LOG_APP_NAME | O (`iap`, directory-style log paths only) | O | O | O | no | — | no |
| IAP_GITHUB_WEBHOOK_SECRET | R if webhook (fail-closed 503 when unset) | O | O | O | yes | runtime-env | no |
| IAP_ELASTICSEARCH_URL | O | O | R if elastic | R if elastic | no | — | no |
| IAP_ELASTICSEARCH_TIMEOUT | O (`30`) | O | O | O | no | — | no |
| IAP_ELASTICSEARCH_QUERY_TIMEOUT | O (`25.0`) | O | O | O | no | — | no |
| IAP_ELASTIC_CURSOR_SIGNING_KEY | O (dev) | O | R | R | yes | runtime-env | yes (production) |
| IAP_ELASTIC_CURSOR_TTL_SECONDS | O (`3600`) | O | O | O | no | — | no |
| IAP_ELASTICSEARCH_MAX_HITS | O (`200`) | O | O | O | no | — | no (operator may only lower) |
| IAP_ELASTICSEARCH_MAX_TIME_WINDOW_SECONDS | O | O | O | O | no | — | no (operator may only lower) |
| IAP_ELASTICSEARCH_MAX_KEYWORD_TERMS | O (`20`) | O | O | O | no | — | no (operator may only lower) |
| IAP_ELASTICSEARCH_MAX_SERVICES | O (`10`) | O | O | O | no | — | no (operator may only lower) |
| IAP_ORACLE_DSN | O | O | R if oracle | R if oracle | yes | runtime-env | no |
| IAP_ORACLE_QUERY_TIMEOUT | O (`30.0`) | O | O | O | no | — | no |
| IAP_POSTGRES_DSN | O | O | R if pg-state | R if pg-state | yes | runtime-env | no |
| IAP_POSTGRES_QUERY_TIMEOUT | O (`30.0`) | O | O | O | no | — | no |
| IAP_GATEWAY_TIMEOUT_SECONDS | O (`30.0`) | O | O | O | no | — | no |
| IAP_MAPPING_PROFILES_PATH | O | O | O | O | no | — | no (missing dir = generic fallback) |
| IAP_MCP_ENABLED | O (`false`) | O | O | O | no | — | no |
| IAP_MCP_TRANSPORT | O (`stdio`) | O | O | O | no | — | no |
| IAP_MCP_HOST | O | O | O | O | no | — | no |
| IAP_MCP_PORT | O (`8899`) | O | O | O | no | — | no |
| IAP_MCP_SERVER_NAME | O | O | O | O | no | — | no |
| IAP_MCP_TENANT_HEADER | O | O | O | O | no | — | no |
| IAP_MCP_INVESTIGATION_HEADER | O | O | O | O | no | — | no |
| IAP_MCP_TENANT_ID | R if stdio | — | — | — | no | — | yes (stdio refused when empty) |
| IAP_MCP_INVESTIGATION_ID | R if stdio | — | — | — | no | — | yes (stdio refused when empty) |
| IAP_MCP_ACTOR_ID | O (`local-operator`) | O | O | O | no | — | no (TO_DO-029: coarse audit) |
| IAP_BUFFER_ENABLED | O (`true`) | O | O | — (prod ignores) | no | — | no |
| IAP_BUFFER_PATH | O | O | O | — (prod ignores) | no | — | no |
| IAP_BUFFER_FLUSH_INTERVAL_SECONDS | O (`5.0`) | O | O | — (prod ignores) | no | — | no |
| IAP_DEV_SEED_TENANTS | O (unset = no seed) | O | — | — (prod raises) | no | — | no |
| IAP_SCHEDULING_TENANTS | O | O | O | O | no | — | no |
| IAP_ARTIFACT_DIR | O | O | O | O | no | — | no |
| IAP_CORS_ALLOW_ORIGINS | O | O | O | O | no | — | no |
| IAP_TRUSTED_HOSTS | O | O | O | O | no | — | no |
| IAP_ENABLE_DOCS | O | O | O | O | no | — | no |
| IAP_RATE_LIMIT_ENABLED | O | O | O | O | no | — | no |
| IAP_SECRET_<PATH> | O (dev fallback) | O | — | — (prod fail-closed) | yes | secret-manager (pending TO_DO-002) | yes (prod `secret://` without backend raises) |
| IAP_SECRET_STORE_PROVIDER | O (unset = no store) | O | O | O | no | — | yes (unknown provider halts boot) |
| IAP_OPENBAO_ADDR | O | O | R if openbao | R if openbao | no | — | no |
| IAP_OPENBAO_TOKEN | O | O | R if openbao | R if openbao | yes | runtime-env | no |
| IAP_OPENBAO_MOUNT | O (`secret`) | O | O | O | no | — | no |
| IAP_OPENBAO_NAMESPACE | O | O | O | O | no | — | no |
| IAP_OPENBAO_TIMEOUT_SECONDS | O (`10.0`) | O | O | O | no | — | no |
| IAP_OPENBAO_VERIFY_TLS | O (`true`) | O | O | O | no | — | no |
| IAP_AWS_REGION | O | O | R if aws | R if aws | no | — | no |
| IAP_AWS_SECRET_PREFIX | O | O | O | O | no | — | no |
| IAP_AZURE_VAULT_URL | O | O | R if azure | R if azure | no | — | yes (adapter requires it) |
| IAP_GCP_PROJECT | O | O | R if gcp | R if gcp | no | — | yes (adapter requires it) |
| IAP_GCP_SECRET_PREFIX | O | O | O | O | no | — | no |

Notes: `IAP_DS_<TENANT>_<ID>` documents the intended `env://` scope per appl-profile §7.4, but `EnvSecretResolver` currently accepts any valid env name (TO_DO-009 in `TO_DO_001.md` tracks aligning doc to code or enforcing the prefix). Unknown `IAP_*` vars are silently ignored (TO_DO-021 tracks warn-or-fail). `config/application.yaml` keys are non-secret defaults only and the YAML loader is dead code pending the TO_DO-004 decision.
