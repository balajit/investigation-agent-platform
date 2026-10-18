# src/investigation_agent_platform/infrastructure/configuration/layers.py
"""Hierarchical configuration loading (TO_DO-004).

Precedence, lowest to highest: process defaults < ``.env*`` files <
``config/application.yaml`` (when present) < runtime environment <
secret-manager values (applied at resolve time by the ``SecretStore`` chain,
never written back into the environment).

``.env*`` files are secret-free by policy (see ``.env.example``); this loader
never requires them and never lets them override real runtime variables.
YAML contributes only a fixed allowlist of non-secret scalar keys and only
for ``IAP_*`` names that are still unset, so YAML can never smuggle a secret
or silently win over an explicit environment setting.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

DOTENV_FILES = (".env",)

YAML_TO_ENV: dict[tuple[str, ...], str] = {
    ("platform", "environment"): "IAP_ENVIRONMENT",
    ("platform", "name"): "IAP_APP_ID",
    ("investigation", "max_tool_calls"): "IAP_MAX_TOOL_CALLS",
    ("investigation", "max_duration_seconds"): "IAP_MAX_DURATION_SECONDS",
    ("persistence", "connection_pool_size"): "IAP_DB_POOL_SIZE",
    ("reasoning", "provider"): "IAP_LLM_PROVIDER",
    ("reasoning", "temperature"): "IAP_LLM_TEMPERATURE",
    ("reasoning", "max_tokens"): "IAP_LLM_MAX_TOKENS",
    ("observability", "tracing_enabled"): "IAP_TELEMETRY_ENABLED",
    ("observability", "otlp_endpoint"): "IAP_OTLP_ENDPOINT",
}


class LayerReport(BaseModel):
    """Which layers contributed values (names and counts only, never values)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dotenv_files: list[str] = Field(default_factory=list)
    yaml_path: str | None = Field(default=None)
    yaml_keys_applied: list[str] = Field(default_factory=list)
    provider_model_key: str | None = Field(default=None)
    unknown_vars: list[str] = Field(default_factory=list)


def effective_environment(config: BaseModel | None = None) -> str:
    """Single source of truth for environment gating (TO_DO-005).

    A loaded config wins (its loader already defaulted unset to
    ``"production"``); without one the process cannot be production
    (production requires full config), so it is ``"development"``.
    Callers must use this instead of re-reading ``os.environ`` with
    divergent defaults.
    """
    if config is not None:
        environment = getattr(config, "environment", "") or ""
        if environment.strip():
            return environment.strip().lower()
    return os.environ.get("IAP_ENVIRONMENT", "development").strip().lower() or "development"


# TO_DO-021: exact names owned by config.py builders (keep in sync with
# config/env-matrix.md); dynamic families below are matched by prefix.
KNOWN_IAP_VARS = frozenset(
    {
        "IAP_ENVIRONMENT",
        "IAP_APP_ID",
        "IAP_DATABASE_URI",
        "IAP_DB_POOL_SIZE",
        "IAP_DB_MAX_OVERFLOW",
        "IAP_LLM_PROVIDER",
        "IAP_LLM_TEMPERATURE",
        "IAP_LLM_MAX_TOKENS",
        "IAP_OPENAI_API_KEY",
        "IAP_OPENAI_MODEL",
        "IAP_OPENAI_ENDPOINT",
        "IAP_ANTHROPIC_API_KEY",
        "IAP_ANTHROPIC_MODEL",
        "IAP_AZURE_API_KEY",
        "IAP_AZURE_MODEL",
        "IAP_AZURE_ENDPOINT",
        "IAP_AZURE_DEPLOYMENT",
        "IAP_AZURE_API_VERSION",
        "IAP_AZURE_EMBEDDING_DEPLOYMENT",
        "IAP_AZURE_EMBEDDING_ENDPOINT",
        "IAP_AZURE_EMBEDDING_API_VERSION",
        "IAP_AZURE_EMBEDDING_API_KEY",
        "IAP_MUSE_API_KEY",
        "IAP_MUSE_MODEL",
        "IAP_MUSE_ENDPOINT",
        "IAP_MAX_TOOL_CALLS",
        "IAP_MAX_REASONING_CALLS",
        "IAP_MAX_DURATION_SECONDS",
        "IAP_TEMPORAL_HOST",
        "IAP_TEMPORAL_NAMESPACE",
        "IAP_TEMPORAL_TASK_QUEUE",
        "IAP_TEMPORAL_INDEXING_QUEUE",
        "IAP_TEMPORAL_ANALYTICS_QUEUE",
        "IAP_TEMPORAL_MAX_ACTIVITIES",
        "IAP_TEMPORAL_MAX_WORKFLOWS",
        "IAP_KAFKA_SERVERS",
        "IAP_KAFKA_TOPIC_PREFIX",
        "IAP_KAFKA_GROUP",
        "IAP_QUOTA_MAX_CONCURRENT_INV",
        "IAP_QUOTA_MAX_CONCURRENT_JOBS",
        "IAP_QUOTA_MAX_QUEUED_JOBS",
        "IAP_QUOTA_MAX_BATCH_RECORDS",
        "IAP_QUOTA_MAX_REF_SOURCE_BYTES",
        "IAP_QUOTA_MAX_REF_FILES",
        "IAP_QUOTA_MAX_VECTOR_ROWS",
        "IAP_QUOTA_MAX_LLM_TOKENS_DAY",
        "IAP_QUOTA_MAX_CHAT_SESSIONS",
        "IAP_QUOTA_MAX_STREAMS",
        "IAP_QUOTA_MAX_REPORT_HISTORY",
        "IAP_QUOTA_MAX_ARTIFACT_BYTES",
        "IAP_STORAGE_ENDPOINT",
        "IAP_STORAGE_BUCKET",
        "IAP_STORAGE_REGION",
        "IAP_STORAGE_ACCESS_KEY",
        "IAP_STORAGE_SECRET_KEY",
        "IAP_TELEMETRY_SERVICE",
        "IAP_OTLP_ENDPOINT",
        "IAP_TELEMETRY_ENABLED",
        "IAP_TOPOLOGY_ENABLED",
        "IAP_TOPOLOGY_REQUIRED",
        "IAP_TOPOLOGY_NEO4J_URI",
        "IAP_TOPOLOGY_NEO4J_USERNAME",
        "IAP_TOPOLOGY_NEO4J_PASSWORD",
        "IAP_TOPOLOGY_NEO4J_DATABASE",
        "IAP_TOPOLOGY_NEO4J_ENCRYPTED",
        "IAP_TOPOLOGY_CONNECTION_TIMEOUT_SECONDS",
        "IAP_TOPOLOGY_QUERY_TIMEOUT_SECONDS",
        "IAP_TOPOLOGY_MAX_POOL_SIZE",
        "IAP_TOPOLOGY_BATCH_SIZE",
        "IAP_TOPOLOGY_MAX_LOOKUP_NODES",
        "IAP_TOPOLOGY_MAX_LOOKUP_EDGES",
        "IAP_CODE_REPO_BASE",
        "IAP_TOPOLOGY_RETENTION_ENABLED",
        "IAP_TOPOLOGY_RETENTION_WINDOW_DAYS",
        "IAP_TOPOLOGY_RETENTION_MAX_SNAPSHOTS",
        "IAP_COGNEE_ENABLED",
        "IAP_COGNEE_DATASET_SALT",
        "IAP_KNOWLEDGE_ENABLED",
        "IAP_KNOWLEDGE_MAX_REVERIFY_ATTEMPTS",
        "IAP_KNOWLEDGE_MAX_EPISODES",
        "IAP_KNOWLEDGE_EVIDENCE_TTL_DAYS",
        "IAP_KNOWLEDGE_MAX_EPISODE_BYTES",
        "IAP_KNOWLEDGE_MEM0_ENABLED",
        "IAP_KNOWLEDGE_MEM0_MODEL",
        "IAP_KNOWLEDGE_MEM0_EMBEDDER",
        "IAP_KNOWLEDGE_MEM0_DIMS",
        "IAP_KNOWLEDGE_MEM0_COLLECTION",
        "IAP_KNOWLEDGE_MEM0_PGVECTOR_URL",
        "IAP_KNOWLEDGE_GRAPHITI_ENABLED",
        "IAP_KNOWLEDGE_GRAPHITI_MODEL",
        "IAP_KNOWLEDGE_GRAPHITI_EMBEDDER",
        "IAP_KNOWLEDGE_GRAPHITI_NEO4J_URI",
        "IAP_KNOWLEDGE_GRAPHITI_NEO4J_USER",
        "IAP_KNOWLEDGE_GRAPHITI_NEO4J_PASSWORD",
        "IAP_KNOWLEDGE_GRAPHITI_NEO4J_DATABASE",
        "IAP_KNOWLEDGE_GRAPHITI_SEMAPHORE_LIMIT",
        "IAP_ELASTICSEARCH_URL",
        "IAP_ELASTICSEARCH_TIMEOUT",
        "IAP_ELASTICSEARCH_QUERY_TIMEOUT",
        "IAP_ELASTIC_CURSOR_SIGNING_KEY",
        "IAP_ELASTIC_CURSOR_TTL_SECONDS",
        "IAP_ELASTICSEARCH_MAX_HITS",
        "IAP_ELASTICSEARCH_MAX_TIME_WINDOW_SECONDS",
        "IAP_ELASTICSEARCH_MAX_KEYWORD_TERMS",
        "IAP_ELASTICSEARCH_MAX_SERVICES",
        "IAP_ORACLE_DSN",
        "IAP_ORACLE_QUERY_TIMEOUT",
        "IAP_POSTGRES_DSN",
        "IAP_POSTGRES_QUERY_TIMEOUT",
        "IAP_GATEWAY_TIMEOUT_SECONDS",
        "IAP_MAPPING_PROFILES_PATH",
        "IAP_MCP_ENABLED",
        "IAP_MCP_TRANSPORT",
        "IAP_MCP_HOST",
        "IAP_MCP_PORT",
        "IAP_MCP_SERVER_NAME",
        "IAP_MCP_TENANT_HEADER",
        "IAP_MCP_INVESTIGATION_HEADER",
        "IAP_MCP_TENANT_ID",
        "IAP_MCP_INVESTIGATION_ID",
        "IAP_MCP_ACTOR_ID",
        "IAP_BUFFER_ENABLED",
        "IAP_BUFFER_PATH",
        "IAP_BUFFER_FLUSH_INTERVAL_SECONDS",
        "IAP_DEV_SEED_TENANTS",
        "IAP_SCHEDULING_TENANTS",
        "IAP_ARTIFACT_DIR",
        "IAP_CORS_ALLOW_ORIGINS",
        "IAP_TRUSTED_HOSTS",
        "IAP_ENABLE_DOCS",
        "IAP_RATE_LIMIT_ENABLED",
        "IAP_HEALTH_DETAIL",
        "IAP_SECRET_STORE_PROVIDER",
        "IAP_OPENBAO_ADDR",
        "IAP_OPENBAO_TOKEN",
        "IAP_OPENBAO_MOUNT",
        "IAP_OPENBAO_NAMESPACE",
        "IAP_OPENBAO_TIMEOUT_SECONDS",
        "IAP_OPENBAO_VERIFY_TLS",
        "IAP_AWS_REGION",
        "IAP_AWS_SECRET_PREFIX",
        "IAP_AZURE_VAULT_URL",
        "IAP_GCP_PROJECT",
        "IAP_GCP_SECRET_PREFIX",
        "IAP_LOG_FILE",
        "IAP_LOG_LEVEL",
        "IAP_LOG_APP_NAME",
        "IAP_ACCESS_LOG_BODY",
        "IAP_ACCESS_LOG_BODY_CHARS",
        "IAP_KNOWLEDGE_CACHE_ENABLED",
        "IAP_KNOWLEDGE_CACHE_TTL_SECONDS",
        "IAP_REDIS_URL",
        "IAP_MCP_LOGS_DB_URL",
        "IAP_MCP_FANOUT_CONCURRENCY",
        "IAP_COMPILER_FULL_EXCERPT_RECENT",
        "IAP_COMPILER_FULL_EXCERPT_RELEVANCE",
        "IAP_DISPOSITION_RETENTION_DAYS",
        "IAP_DISPOSITION_SIMILARITY_THRESHOLD",
        "IAP_DISPOSITION_MAX_CANDIDATES",
        "IAP_DISPOSITION_LOG_RETENTION_DAYS",
    }
)

# Dynamic families owned by convention (never enumerated literally).
DYNAMIC_IAP_PREFIXES = ("IAP_DS_", "IAP_SECRET_")


def find_unknown_vars() -> list[str]:
    """Return set-but-unowned ``IAP_*`` names (typo/misconfig detector)."""
    unknown: list[str] = []
    for name in os.environ:
        if not name.startswith("IAP_"):
            continue
        if name in KNOWN_IAP_VARS:
            continue
        if name.startswith(DYNAMIC_IAP_PREFIXES):
            continue
        unknown.append(name)
    return sorted(unknown)


def _load_dotenv_layer(*, project_root: Path) -> list[str]:
    """Load ``.env`` plus ``.env.<environment>`` without overriding runtime vars."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - dependency is required in prod
        logger.warning("python-dotenv unavailable; skipping .env* layer")
        return []
    loaded: list[str] = []
    environment = os.environ.get("IAP_ENVIRONMENT", "development").strip() or "development"
    candidates = [project_root / ".env", project_root / f".env.{environment}"]
    for candidate in candidates:
        if candidate.is_file():
            if load_dotenv(candidate, override=False):
                loaded.append(candidate.name)
    return loaded


def _dig(mapping: dict, *, path: tuple[str, ...]):  # type: ignore[no-untyped-def]
    current = mapping
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _load_yaml_layer(*, yaml_path: Path) -> tuple[list[str], str | None]:
    """Apply allowlisted YAML scalars as defaults for unset ``IAP_*`` names."""
    import yaml

    if not yaml_path.is_file():
        return [], None
    try:
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("Ignoring unreadable config YAML", extra={"error": str(exc)})
        return [], None
    if not isinstance(raw, dict):
        return [], None
    applied: list[str] = []
    provider_model_key: str | None = None
    for path, env_name in YAML_TO_ENV.items():
        if os.environ.get(env_name):
            continue
        value = _dig(raw, path=path)
        if value is None or isinstance(value, (dict, list)):
            continue
        text = str(value).strip().lower() if isinstance(value, bool) else str(value)
        os.environ[env_name] = text
        applied.append(env_name)
    provider = os.environ.get("IAP_LLM_PROVIDER", "").strip().lower()
    model = _dig(raw, path=("reasoning", "model"))
    if provider and model and not isinstance(model, (dict, list)):
        provider_model_key = f"IAP_{provider.upper()}_MODEL"
        if not os.environ.get(provider_model_key):
            os.environ[provider_model_key] = str(model)
            applied.append(provider_model_key)
    return applied, provider_model_key


def apply_layered_config(
    *,
    project_root: str | Path | None = None,
    yaml_path: str | Path | None = None,
) -> LayerReport:
    """Apply the file layers in order; runtime env and manager always win.

    Safe to call multiple times (later calls only fill still-unset names).
    Returns a report of contributing layers for startup logging.
    """
    root = Path(project_root) if project_root else Path.cwd()
    yaml_file = Path(yaml_path) if yaml_path else (root / "config" / "application.yaml")
    dotenv_files = _load_dotenv_layer(project_root=root)
    yaml_keys, provider_model_key = _load_yaml_layer(yaml_path=yaml_file)
    unknown_vars = find_unknown_vars()
    if unknown_vars:
        logger.warning(
            "Ignoring unrecognized IAP_* variables",
            extra={"unknown_vars": unknown_vars},
        )
    report = LayerReport(
        dotenv_files=dotenv_files,
        yaml_path=str(yaml_file) if yaml_file.is_file() else None,
        yaml_keys_applied=yaml_keys,
        provider_model_key=provider_model_key,
        unknown_vars=unknown_vars,
    )
    logger.info(
        "Layered config applied",
        extra={"dotenv": dotenv_files, "yaml_keys": len(yaml_keys)},
    )
    return report
