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
        logger.exception("Failed to parse YAML configuration", extra={"config_path": str(config_path)})
        raise PlatformConfigurationError(f"Invalid configuration file {config_path}: {exc}") from exc


def load_application_profile_from_file(path: str | Path) -> ApplicationProfile:
    """Loads an `config/profiles/*.yaml` document into an ApplicationProfile."""
    profile_path = Path(path)
    if not profile_path.is_file():
        logger.error("Profile file missing", extra={"profile_path": str(profile_path)})
        raise PlatformConfigurationError(f"Profile file not found: {profile_path}")
    try:
        raw = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.exception("Failed to parse YAML profile document", extra={"profile_path": str(profile_path)})
        raise PlatformConfigurationError(f"Invalid profile file {profile_path}: {exc}") from exc

    application = raw.get("application") or {}
    profile_doc = {**application, **{k: v for k, v in raw.items() if k != "application"}}
    try:
        return ApplicationProfile.model_validate(profile_doc)
    except Exception as exc:
        logger.exception("Profile schema validation failure", extra={"profile_path": str(profile_path)})
        raise PlatformConfigurationError(f"Invalid profile document {profile_path}: {exc}") from exc


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
                    model_name=os.environ.get("IAP_LLM_MODEL", str(reasoning.get("model", "gpt-4o"))),
                    provider=os.environ.get("IAP_LLM_PROVIDER", str(reasoning.get("provider", "openai"))),
                    temperature=float(reasoning.get("temperature", 0.0)),
                    max_tokens=int(reasoning.get("max_tokens", 4096)),
                ),
                budget=BudgetConfig(
                    max_tool_calls=int(
                        os.environ.get("IAP_MAX_TOOL_CALLS", str(investigation.get("max_tool_calls", 50)))
                    ),
                    max_reasoning_calls=int(
                        os.environ.get(
                            "IAP_MAX_REASONING_CALLS", str(investigation.get("max_reasoning_calls", 20))
                        )
                    ),
                    max_duration_seconds=int(
                        os.environ.get(
                            "IAP_MAX_DURATION_SECONDS", str(investigation.get("max_duration_seconds", 1800))
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
                    bucket_name=os.environ.get("IAP_STORAGE_BUCKET", "investigation-evidence-payloads"),
                    region=os.environ.get("IAP_STORAGE_REGION", "us-east-1"),
                    access_key=SecretStr(os.environ.get("IAP_STORAGE_ACCESS_KEY", "")),
                    secret_key=SecretStr(os.environ.get("IAP_STORAGE_SECRET_KEY", "")),
                ),
                telemetry=TelemetryConfig(
                    service_name=os.environ.get("IAP_TELEMETRY_SERVICE", platform.name),
                    otlp_endpoint=os.environ.get("IAP_OTLP_ENDPOINT", "http://localhost:4317"),
                    enabled=os.environ.get("IAP_TELEMETRY_ENABLED", "true").lower() == "true",
                ),
            )
        except Exception as exc:
            logger.critical("Boot halted: invalid configuration settings", exc_info=exc)
            raise PlatformConfigurationError(f"Boot halted: invalid configuration settings: {exc!s}") from exc


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
                    bucket_name=os.environ.get("IAP_STORAGE_BUCKET", "investigation-evidence-payloads"),
                    region=os.environ.get("IAP_STORAGE_REGION", "us-east-1"),
                    access_key=SecretStr(os.environ.get("IAP_STORAGE_ACCESS_KEY", "")),
                    secret_key=SecretStr(os.environ.get("IAP_STORAGE_SECRET_KEY", "")),
                ),
                telemetry=TelemetryConfig(
                    service_name=os.environ.get("IAP_TELEMETRY_SERVICE", "investigation-agent-platform"),
                    otlp_endpoint=os.environ.get("IAP_OTLP_ENDPOINT", "http://localhost:4317"),
                    enabled=os.environ.get("IAP_TELEMETRY_ENABLED", "true").lower() == "true",
                ),
            )
        except Exception as exc:
            if isinstance(exc, PlatformConfigurationError):
                raise exc
            logger.critical("Boot halted: invalid configuration settings from env", exc_info=exc)
            raise PlatformConfigurationError(
                f"Boot halted: invalid configuration settings: {exc!s}"
            ) from exc