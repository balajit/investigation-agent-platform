# src/investigation_agent_platform/infrastructure/configuration/config.py
"""Infrastructure configuration models and strongly-typed environment loaders."""

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError
from investigation_agent_platform.domain.profile.models import ApplicationProfile

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class DatabaseConfig(BaseModel):
    """Database connection and pooling parameters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connection_uri: SecretStr = Field(..., min_length=1)
    pool_size: int = Field(default=10, ge=1, le=100)
    max_overflow: int = Field(default=20, ge=0)


class LLMConfig(BaseModel):
    """Large Language Model provider configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_key: SecretStr = Field(..., min_length=1)
    model_name: str = Field(default="gpt-4o", min_length=1)
    provider: str = Field(default="openai", description="openai|anthropic")
    temperature: float = Field(default=0.0, ge=0.0, le=1.0)
    max_tokens: int = Field(default=4096, ge=128)


class BudgetConfig(BaseModel):
    """Platform budget and tool call limit configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_tool_calls: int = Field(default=50, ge=1)
    max_reasoning_calls: int = Field(default=20, ge=1)
    max_duration_seconds: int = Field(default=1800, ge=10)


class TemporalConfig(BaseModel):
    """Temporal workflow engine configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_host: str = Field(default="localhost:7233", min_length=1)
    namespace: str = Field(default="default", min_length=1)
    task_queue: str = Field(default="investigation-tasks", min_length=1)


class KafkaConfig(BaseModel):
    """Kafka messaging broker configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bootstrap_servers: str = Field(default="localhost:9092", min_length=1)
    topic_prefix: str = Field(default="investigation", min_length=1)
    consumer_group: str = Field(default="investigation-agent-group", min_length=1)


class ObjectStorageConfig(BaseModel):
    """S3/MinIO tier-2 payload storage configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint_url: str | None = Field(default=None)
    bucket_name: str = Field(default="investigation-evidence-payloads", min_length=1)
    region: str = Field(default="us-east-1", min_length=1)
    access_key: SecretStr = Field(default=SecretStr(""))
    secret_key: SecretStr = Field(default=SecretStr(""))


class TelemetryConfig(BaseModel):
    """OpenTelemetry instrumentation setup."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    service_name: str = Field(default="investigation-agent-platform", min_length=1)
    otlp_endpoint: str = Field(default="http://localhost:4317", min_length=1)
    enabled: bool = Field(default=True)


class TopologyConfig(BaseModel):
    """Layer 3 static-code/organizational topology (Neo4j) configuration.

    Credentials are ``SecretStr`` and must never be logged. ``required``
    controls whether production startup fails hard when topology cannot be
    wired (F-001 precedent: production must never silently degrade).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = Field(default=False)
    required: bool = Field(default=False)
    uri: SecretStr = Field(default=SecretStr(""))
    username: SecretStr = Field(default=SecretStr(""))
    password: SecretStr = Field(default=SecretStr(""))
    database: str = Field(default="neo4j", min_length=1)
    encrypted: bool = Field(default=True)
    connection_timeout_seconds: float = Field(default=10.0, ge=1.0, le=120.0)
    query_timeout_seconds: float = Field(default=15.0, ge=1.0, le=120.0)
    max_connection_pool_size: int = Field(default=50, ge=1, le=500)
    ingestion_batch_size: int = Field(default=500, ge=1, le=10000)
    max_lookup_nodes: int = Field(default=200, ge=1, le=100000)
    max_lookup_edges: int = Field(default=1000, ge=1, le=1000000)
    code_repo_base_path: str = Field(
        default="",
        max_length=1024,
        description="Filesystem base for on-demand tiers (CODEOWNERS, micro "
        "parse). Empty disables file-based tiers gracefully; same IAP_CODE_REPO_BASE "
        "convention as the code intelligence provider.",
    )
    # ISSUE-4: snapshot retention / garbage collection. Disabled by default —
    # an operator opts in explicitly once a cleanup schedule is wired.
    retention_enabled: bool = Field(default=False)
    retention_window_days: int = Field(
        default=30,
        ge=1,
        le=3650,
        description="Snapshots ingested within this many days are always kept, "
        "regardless of the LRU bound (D9/ISSUES_0926 risk 2).",
    )
    retention_max_snapshots_per_repository: int = Field(
        default=20,
        ge=1,
        le=10000,
        description="LRU bound: the N most-recently-ingested READY/SUPERSEDED "
        "snapshots per repository are always kept even if older than the window.",
    )


class KnowledgeConfig(BaseModel):
    """Execution knowledge layer configuration (Part 6 Slice 0).

    Slice 0 needs no external services: envelopes live in Postgres and the
    Mem0/Graphiti adapters default to disabled. Later slices flip `mem0` /
    `graphiti` on with their own connection settings.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = Field(default=True)
    max_reverify_attempts: int = Field(default=3, ge=1, le=10)
    max_episodes_per_investigation: int = Field(default=50, ge=1, le=1000)
    mem0_enabled: bool = Field(default=False)
    graphiti_enabled: bool = Field(default=False)
    # Slice 1 (Mem0): model + embedder + vector-store wiring. pgvector
    # reuses the application Postgres via connection string override.
    mem0_model: str = Field(default="gpt-4o-mini", min_length=1)
    mem0_embedder_model: str = Field(default="text-embedding-3-small", min_length=1)
    mem0_embedding_dims: int = Field(default=1536, ge=1, le=16384)
    mem0_collection: str = Field(default="iap_memories", min_length=1)
    mem0_pgvector_url: SecretStr = Field(default=SecretStr(""))
    # Slice 2 (Graphiti): self-hosted graphiti-core against Neo4j. Shares
    # the Layer 3 Neo4j connection settings by default; override only when
    # the knowledge graph must live on a separate instance.
    graphiti_model: str = Field(default="gpt-4o-mini", min_length=1)
    graphiti_embedder_model: str = Field(default="text-embedding-3-small", min_length=1)
    graphiti_neo4j_uri: SecretStr = Field(default=SecretStr(""))
    graphiti_neo4j_user: SecretStr = Field(default=SecretStr(""))
    graphiti_neo4j_password: SecretStr = Field(default=SecretStr(""))
    graphiti_neo4j_database: str = Field(default="neo4j", min_length=1)
    graphiti_semaphore_limit: int = Field(default=5, ge=1, le=100)


def _knowledge_config_from_env() -> KnowledgeConfig:
    """Build ``KnowledgeConfig`` from ``IAP_KNOWLEDGE_*`` environment variables."""
    return KnowledgeConfig(
        enabled=os.environ.get("IAP_KNOWLEDGE_ENABLED", "true").lower() == "true",
        max_reverify_attempts=int(os.environ.get("IAP_KNOWLEDGE_MAX_REVERIFY_ATTEMPTS", "3")),
        max_episodes_per_investigation=int(os.environ.get("IAP_KNOWLEDGE_MAX_EPISODES", "50")),
        mem0_enabled=os.environ.get("IAP_KNOWLEDGE_MEM0_ENABLED", "false").lower() == "true",
        graphiti_enabled=os.environ.get("IAP_KNOWLEDGE_GRAPHITI_ENABLED", "false").lower()
        == "true",
        mem0_model=os.environ.get("IAP_KNOWLEDGE_MEM0_MODEL", "gpt-4o-mini"),
        mem0_embedder_model=os.environ.get("IAP_KNOWLEDGE_MEM0_EMBEDDER", "text-embedding-3-small"),
        mem0_embedding_dims=int(os.environ.get("IAP_KNOWLEDGE_MEM0_DIMS", "1536")),
        mem0_collection=os.environ.get("IAP_KNOWLEDGE_MEM0_COLLECTION", "iap_memories"),
        mem0_pgvector_url=SecretStr(os.environ.get("IAP_KNOWLEDGE_MEM0_PGVECTOR_URL", "")),
        graphiti_model=os.environ.get("IAP_KNOWLEDGE_GRAPHITI_MODEL", "gpt-4o-mini"),
        graphiti_embedder_model=os.environ.get(
            "IAP_KNOWLEDGE_GRAPHITI_EMBEDDER", "text-embedding-3-small"
        ),
        graphiti_neo4j_uri=SecretStr(os.environ.get("IAP_KNOWLEDGE_GRAPHITI_NEO4J_URI", "")),
        graphiti_neo4j_user=SecretStr(os.environ.get("IAP_KNOWLEDGE_GRAPHITI_NEO4J_USER", "")),
        graphiti_neo4j_password=SecretStr(
            os.environ.get("IAP_KNOWLEDGE_GRAPHITI_NEO4J_PASSWORD", "")
        ),
        graphiti_neo4j_database=os.environ.get("IAP_KNOWLEDGE_GRAPHITI_NEO4J_DATABASE", "neo4j"),
        graphiti_semaphore_limit=int(
            os.environ.get("IAP_KNOWLEDGE_GRAPHITI_SEMAPHORE_LIMIT", "5")
        ),
    )


class ApplicationConfig(BaseModel):
    """Root application runtime configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: str = Field(default="production", min_length=1)
    application_id: str = Field(default="investigation-agent-platform", min_length=1)
    database: DatabaseConfig
    llm: LLMConfig
    budget: BudgetConfig
    temporal: TemporalConfig = Field(default_factory=TemporalConfig)
    kafka: KafkaConfig = Field(default_factory=KafkaConfig)
    storage: ObjectStorageConfig = Field(default_factory=ObjectStorageConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    topology: TopologyConfig = Field(default_factory=TopologyConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)


class PlatformHeader(BaseModel):
    """Platform section header inside application settings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(default="investigation-platform", min_length=1)
    environment: str = Field(default="production", min_length=1)


class PlatformSettings(BaseModel):
    """Strongly typed application configuration settings document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: PlatformHeader = Field(default_factory=PlatformHeader)
    investigation: dict[str, Any] = Field(default_factory=dict)
    persistence: dict[str, Any] = Field(default_factory=dict)
    reasoning: dict[str, Any] = Field(default_factory=dict)
    observability: dict[str, Any] = Field(default_factory=dict)


def load_platform_settings(path: str | Path) -> PlatformSettings:
    """Loads `config/application.yaml` into its strongly typed structure."""
    config_path = Path(path)
    if not config_path.is_file():
        logger.error("Configuration file missing", extra={"config_path": str(config_path)})
        raise PlatformConfigurationError(f"Configuration file not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        return PlatformSettings.model_validate(raw)
    except Exception as exc:
        logger.exception(
            "Failed to parse YAML configuration", extra={"config_path": str(config_path)}
        )
        raise PlatformConfigurationError(
            f"Invalid configuration file {config_path}: {exc}"
        ) from exc


def load_application_profile_from_file(path: str | Path) -> ApplicationProfile:
    """Loads an `config/profiles/*.yaml` document into an ApplicationProfile."""
    profile_path = Path(path)
    if not profile_path.is_file():
        logger.error("Profile file missing", extra={"profile_path": str(profile_path)})
        raise PlatformConfigurationError(f"Profile file not found: {profile_path}")
    try:
        raw = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.exception(
            "Failed to parse YAML profile document", extra={"profile_path": str(profile_path)}
        )
        raise PlatformConfigurationError(f"Invalid profile file {profile_path}: {exc}") from exc

    application = raw.get("application") or {}
    profile_doc = {**application, **{k: v for k, v in raw.items() if k != "application"}}
    try:
        return ApplicationProfile.model_validate(profile_doc)
    except Exception as exc:
        logger.exception(
            "Profile schema validation failure", extra={"profile_path": str(profile_path)}
        )
        raise PlatformConfigurationError(f"Invalid profile document {profile_path}: {exc}") from exc


def _topology_config_from_env() -> TopologyConfig:
    """Build ``TopologyConfig`` from ``IAP_TOPOLOGY_*`` environment variables.

    Defaults to disabled/not-required so existing deployments are unaffected
    until Neo4j is explicitly configured.
    """
    return TopologyConfig(
        enabled=os.environ.get("IAP_TOPOLOGY_ENABLED", "false").lower() == "true",
        required=os.environ.get("IAP_TOPOLOGY_REQUIRED", "false").lower() == "true",
        uri=SecretStr(os.environ.get("IAP_TOPOLOGY_NEO4J_URI", "")),
        username=SecretStr(os.environ.get("IAP_TOPOLOGY_NEO4J_USERNAME", "")),
        password=SecretStr(os.environ.get("IAP_TOPOLOGY_NEO4J_PASSWORD", "")),
        database=os.environ.get("IAP_TOPOLOGY_NEO4J_DATABASE", "neo4j"),
        encrypted=os.environ.get("IAP_TOPOLOGY_NEO4J_ENCRYPTED", "true").lower() == "true",
        connection_timeout_seconds=float(
            os.environ.get("IAP_TOPOLOGY_CONNECTION_TIMEOUT_SECONDS", "10.0")
        ),
        query_timeout_seconds=float(os.environ.get("IAP_TOPOLOGY_QUERY_TIMEOUT_SECONDS", "15.0")),
        max_connection_pool_size=int(os.environ.get("IAP_TOPOLOGY_MAX_POOL_SIZE", "50")),
        ingestion_batch_size=int(os.environ.get("IAP_TOPOLOGY_BATCH_SIZE", "500")),
        max_lookup_nodes=int(os.environ.get("IAP_TOPOLOGY_MAX_LOOKUP_NODES", "200")),
        max_lookup_edges=int(os.environ.get("IAP_TOPOLOGY_MAX_LOOKUP_EDGES", "1000")),
        code_repo_base_path=os.environ.get("IAP_CODE_REPO_BASE", ""),
        retention_enabled=os.environ.get("IAP_TOPOLOGY_RETENTION_ENABLED", "false").lower()
        == "true",
        retention_window_days=int(os.environ.get("IAP_TOPOLOGY_RETENTION_WINDOW_DAYS", "30")),
        retention_max_snapshots_per_repository=int(
            os.environ.get("IAP_TOPOLOGY_RETENTION_MAX_SNAPSHOTS", "20")
        ),
    )


def load_application_config_from_yaml(
    path: str | Path = Path("config/application.yaml"),
    database_uri: str | None = None,
    llm_api_key: str | None = None,
) -> ApplicationConfig:
    """Binds YAML platform settings to an ApplicationConfig, enforcing secret environment sources."""
    with tracer.start_as_current_span("load_application_config_from_yaml"):
        settings = load_platform_settings(path)

        db_uri = database_uri or os.environ.get("IAP_DATABASE_URI", "")
        llm_key = llm_api_key or os.environ.get("IAP_LLM_API_KEY", "")
        if not db_uri or not llm_key:
            missing = [
                name
                for name, value in (("IAP_DATABASE_URI", db_uri), ("IAP_LLM_API_KEY", llm_key))
                if not value
            ]
            msg = f"Boot halted: missing required environment variables: {', '.join(missing)}"
            logger.critical(msg, extra={"missing_vars": missing})
            raise PlatformConfigurationError(msg)

        persistence = settings.persistence or {}
        reasoning = settings.reasoning or {}
        investigation = settings.investigation or {}
        platform = settings.platform

        try:
            return ApplicationConfig(
                environment=os.environ.get("IAP_ENVIRONMENT", platform.environment),
                application_id=os.environ.get("IAP_APP_ID", platform.name),
                database=DatabaseConfig(
                    connection_uri=SecretStr(db_uri),
                    pool_size=int(persistence.get("connection_pool_size", 10)),
                    max_overflow=int(persistence.get("max_overflow", 20)),
                ),
                llm=LLMConfig(
                    api_key=SecretStr(llm_key),
                    model_name=os.environ.get(
                        "IAP_LLM_MODEL", str(reasoning.get("model", "gpt-4o"))
                    ),
                    provider=os.environ.get(
                        "IAP_LLM_PROVIDER", str(reasoning.get("provider", "openai"))
                    ),
                    temperature=float(reasoning.get("temperature", 0.0)),
                    max_tokens=int(reasoning.get("max_tokens", 4096)),
                ),
                budget=BudgetConfig(
                    max_tool_calls=int(
                        os.environ.get(
                            "IAP_MAX_TOOL_CALLS", str(investigation.get("max_tool_calls", 50))
                        )
                    ),
                    max_reasoning_calls=int(
                        os.environ.get(
                            "IAP_MAX_REASONING_CALLS",
                            str(investigation.get("max_reasoning_calls", 20)),
                        )
                    ),
                    max_duration_seconds=int(
                        os.environ.get(
                            "IAP_MAX_DURATION_SECONDS",
                            str(investigation.get("max_duration_seconds", 1800)),
                        )
                    ),
                ),
                temporal=TemporalConfig(
                    target_host=os.environ.get("IAP_TEMPORAL_HOST", "localhost:7233"),
                    namespace=os.environ.get("IAP_TEMPORAL_NAMESPACE", "default"),
                    task_queue=os.environ.get("IAP_TEMPORAL_TASK_QUEUE", "investigation-tasks"),
                ),
                kafka=KafkaConfig(
                    bootstrap_servers=os.environ.get("IAP_KAFKA_SERVERS", "localhost:9092"),
                    topic_prefix=os.environ.get("IAP_KAFKA_TOPIC_PREFIX", "investigation"),
                    consumer_group=os.environ.get("IAP_KAFKA_GROUP", "investigation-agent-group"),
                ),
                storage=ObjectStorageConfig(
                    endpoint_url=os.environ.get("IAP_STORAGE_ENDPOINT"),
                    bucket_name=os.environ.get(
                        "IAP_STORAGE_BUCKET", "investigation-evidence-payloads"
                    ),
                    region=os.environ.get("IAP_STORAGE_REGION", "us-east-1"),
                    access_key=SecretStr(os.environ.get("IAP_STORAGE_ACCESS_KEY", "")),
                    secret_key=SecretStr(os.environ.get("IAP_STORAGE_SECRET_KEY", "")),
                ),
                telemetry=TelemetryConfig(
                    service_name=os.environ.get("IAP_TELEMETRY_SERVICE", platform.name),
                    otlp_endpoint=os.environ.get("IAP_OTLP_ENDPOINT", "http://localhost:4317"),
                    enabled=os.environ.get("IAP_TELEMETRY_ENABLED", "true").lower() == "true",
                ),
                topology=_topology_config_from_env(),
                knowledge=_knowledge_config_from_env(),
            )
        except Exception as exc:
            logger.critical("Boot halted: invalid configuration settings", exc_info=exc)
            raise PlatformConfigurationError(
                f"Boot halted: invalid configuration settings: {exc!s}"
            ) from exc


def load_application_config_from_env() -> ApplicationConfig:
    """Loads and validates configuration from environment, halting boot on failure."""
    with tracer.start_as_current_span("load_application_config_from_env"):
        db_uri = os.environ.get("IAP_DATABASE_URI", "")
        llm_key = os.environ.get("IAP_LLM_API_KEY", "")

        if not db_uri or not llm_key:
            missing = []
            if not db_uri:
                missing.append("IAP_DATABASE_URI")
            if not llm_key:
                missing.append("IAP_LLM_API_KEY")
            msg = f"Boot halted: missing required environment variables: {', '.join(missing)}"
            logger.critical(msg, extra={"missing_vars": missing})
            raise PlatformConfigurationError(msg)

        try:
            return ApplicationConfig(
                environment=os.environ.get("IAP_ENVIRONMENT", "production"),
                application_id=os.environ.get("IAP_APP_ID", "investigation-agent-platform"),
                database=DatabaseConfig(
                    connection_uri=SecretStr(db_uri),
                    pool_size=int(os.environ.get("IAP_DB_POOL_SIZE", "10")),
                    max_overflow=int(os.environ.get("IAP_DB_MAX_OVERFLOW", "20")),
                ),
                llm=LLMConfig(
                    api_key=SecretStr(llm_key),
                    model_name=os.environ.get("IAP_LLM_MODEL", "gpt-4o"),
                    provider=os.environ.get("IAP_LLM_PROVIDER", "openai"),
                    temperature=float(os.environ.get("IAP_LLM_TEMPERATURE", "0.0")),
                    max_tokens=int(os.environ.get("IAP_LLM_MAX_TOKENS", "4096")),
                ),
                budget=BudgetConfig(
                    max_tool_calls=int(os.environ.get("IAP_MAX_TOOL_CALLS", "50")),
                    max_reasoning_calls=int(os.environ.get("IAP_MAX_REASONING_CALLS", "20")),
                    max_duration_seconds=int(os.environ.get("IAP_MAX_DURATION_SECONDS", "1800")),
                ),
                temporal=TemporalConfig(
                    target_host=os.environ.get("IAP_TEMPORAL_HOST", "localhost:7233"),
                    namespace=os.environ.get("IAP_TEMPORAL_NAMESPACE", "default"),
                    task_queue=os.environ.get("IAP_TEMPORAL_TASK_QUEUE", "investigation-tasks"),
                ),
                kafka=KafkaConfig(
                    bootstrap_servers=os.environ.get("IAP_KAFKA_SERVERS", "localhost:9092"),
                    topic_prefix=os.environ.get("IAP_KAFKA_TOPIC_PREFIX", "investigation"),
                    consumer_group=os.environ.get("IAP_KAFKA_GROUP", "investigation-agent-group"),
                ),
                storage=ObjectStorageConfig(
                    endpoint_url=os.environ.get("IAP_STORAGE_ENDPOINT"),
                    bucket_name=os.environ.get(
                        "IAP_STORAGE_BUCKET", "investigation-evidence-payloads"
                    ),
                    region=os.environ.get("IAP_STORAGE_REGION", "us-east-1"),
                    access_key=SecretStr(os.environ.get("IAP_STORAGE_ACCESS_KEY", "")),
                    secret_key=SecretStr(os.environ.get("IAP_STORAGE_SECRET_KEY", "")),
                ),
                telemetry=TelemetryConfig(
                    service_name=os.environ.get(
                        "IAP_TELEMETRY_SERVICE", "investigation-agent-platform"
                    ),
                    otlp_endpoint=os.environ.get("IAP_OTLP_ENDPOINT", "http://localhost:4317"),
                    enabled=os.environ.get("IAP_TELEMETRY_ENABLED", "true").lower() == "true",
                ),
                topology=_topology_config_from_env(),
                knowledge=_knowledge_config_from_env(),
            )
        except Exception as exc:
            if isinstance(exc, PlatformConfigurationError):
                raise exc
            logger.critical("Boot halted: invalid configuration settings from env", exc_info=exc)
            raise PlatformConfigurationError(
                f"Boot halted: invalid configuration settings: {exc!s}"
            ) from exc
