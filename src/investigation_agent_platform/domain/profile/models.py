# src/investigation_agent_platform/domain/profile/models.py
"""Application Profile canonical content model (IAP appl-profile 1.0, in-place).

Single schema — no v1/v2 dualism (pre-production codebase). Topology is
service-oriented; databases and external stores are typed, reference-based
datasources; investigation/correlation/knowledge bound every consumer.
Secrets are never valid profile content: connections are ``secret://`` /
``env://`` references resolved server-side.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from investigation_agent_platform.domain.observability.mapping import GENERIC_ECS_SOURCE_ID

VALID_PLANES = frozenset({"backend", "frontend", "mobile", "workers", "data-pipeline"})
VALID_DATA_CLASSIFICATIONS = frozenset({"PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"})
VALID_SOURCE_TYPES = frozenset({"authored", "manifest", "spring_config"})

VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "DRAFT": frozenset({"ACTIVE", "ARCHIVED"}),
    "ACTIVE": frozenset({"DEPRECATED", "ARCHIVED"}),
    "DEPRECATED": frozenset({"ARCHIVED"}),
    "ARCHIVED": frozenset(),
}

_SECRET_URL_RE = re.compile(r"://[^/\s@]+@")
_SECRET_TOKEN_RE = re.compile(r"AKIA|BEGIN .*PRIVATE KEY|password", re.IGNORECASE)


def contains_secret_text(value: str) -> bool:
    return bool(_SECRET_URL_RE.search(value) or _SECRET_TOKEN_RE.search(value))


def check_status_transition(from_status: str, to_status: str) -> bool:
    if from_status == to_status:
        return True
    return to_status in VALID_TRANSITIONS.get(from_status, frozenset())


class ProfileStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    ARCHIVED = "ARCHIVED"


class ServiceTelemetry(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    log_indices: list[str] = Field(default_factory=list, max_length=50)
    service_names: list[str] = Field(default_factory=list, max_length=50)
    trace_backend: str | None = Field(default=None, max_length=64)
    metrics_backend: str | None = Field(default=None, max_length=64)
    default_lookback_s: int | None = Field(default=None, ge=60, le=7 * 86400)


class ServiceCode(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    org: str | None = Field(default=None, max_length=128)
    repos: list[str] = Field(default_factory=list, max_length=20)
    default_branch: str = Field(default="main", max_length=128)
    language: str | None = Field(default=None, max_length=64)
    source_roots: list[str] = Field(default_factory=list, max_length=50)
    build_system: str | None = Field(default=None, max_length=128)
    module_structure: str | None = Field(default=None, max_length=256)
    paths: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("repos", "source_roots", "paths")
    @classmethod
    def validate_no_traversal(cls, v: list[str]) -> list[str]:
        for item in v:
            if ".." in item or "\0" in item:
                raise ValueError("Path traversal sequence in ServiceCode.")
        return v


class ServiceDependency(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    service: str = Field(..., max_length=128)
    type: str = Field(default="internal", max_length=32)
    critical: bool = False


class ProbeableEndpoint(BaseModel):
    """One API surface this profile authorizes PROBE_API to call (P2-2).

    Declared once per profile; the model selects an endpoint_id, never a
    URL. Initial methods are GET/HEAD only — no mutating probes.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    id: str = Field(..., max_length=128)
    description: str = Field(default="", max_length=300)
    method: str = Field(default="GET", max_length=10)
    base_url: str = Field(..., max_length=512)
    path_template: str = Field(..., max_length=256)
    path_placeholders: list[str] = Field(default_factory=list, max_length=10)
    allowed_query_params: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("method")
    @classmethod
    def validate_method(cls, v: str) -> str:
        method = v.upper()
        if method not in ("GET", "HEAD"):
            raise ValueError(f"Probe method must be GET or HEAD, got {v!r}.")
        return method


class Service(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    name: str = Field(..., max_length=128)
    role: str | None = Field(default=None, max_length=32)
    description: str | None = Field(default=None, max_length=512)
    telemetry: ServiceTelemetry = Field(default_factory=ServiceTelemetry)
    code: ServiceCode = Field(default_factory=ServiceCode)
    dependencies: list[ServiceDependency] = Field(default_factory=list, max_length=50)
    endpoints: list[str] = Field(default_factory=list, max_length=50)
    repository_id: str | None = Field(default=None, max_length=128)
    probeable_endpoints: list[ProbeableEndpoint] = Field(default_factory=list, max_length=50)


class DatasourceKind(StrEnum):
    ORACLE = "oracle"
    POSTGRES = "postgres"
    MYSQL = "mysql"
    SQLSERVER = "sqlserver"
    S3 = "s3"
    DYNAMODB = "dynamodb"
    REDIS = "redis"
    MONGO = "mongo"
    CASSANDRA = "cassandra"
    ELASTICSEARCH = "elasticsearch"
    KAFKA = "kafka"
    FILE_LOG = "file_log"
    RABBITMQ = "rabbitmq"
    MCP = "mcp"
    OTHER = "other"


class DatasourceRole(StrEnum):
    STATE = "state"
    READONLY = "readonly"
    ANALYTICS = "analytics"
    CACHE = "cache"
    QUEUE = "queue"
    OBJECT_STORE = "object_store"
    SEARCH = "search"
    METRICS = "metrics"
    LOGS = "logs"
    TELEMETRY = "telemetry"


class QueryTemplate(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    description: str | None = Field(default=None, max_length=256)
    sql: str = Field(..., min_length=1, max_length=8192)
    read_only: bool = True
    allowed_tables: list[str] = Field(default_factory=list, max_length=50)
    timeout_ms: int | None = Field(default=None, ge=100, le=300000)

    @field_validator("sql")
    @classmethod
    def validate_sql_safety(cls, sql: str) -> str:
        upper = f" {sql.upper()} "
        for kw in ["DROP", "DELETE", "TRUNCATE", "ALTER", "GRANT", "REVOKE"]:
            if f" {kw} " in upper:
                raise ValueError(f"Forbidden DDL/DML keyword '{kw}' in query template.")
        return sql


class S3DatasourceSpec(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    bucket: str = Field(..., max_length=256)
    prefix: str | None = Field(default=None, max_length=512)
    region: str | None = Field(default=None, max_length=64)
    endpoint_url: str | None = Field(default=None, max_length=512)


class DynamoDBDatasourceSpec(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    table: str = Field(..., max_length=256)
    region: str | None = Field(default=None, max_length=64)
    endpoint_url: str | None = Field(default=None, max_length=512)


class KafkaDatasourceSpec(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    bootstrap_servers: list[str] = Field(default_factory=list, max_length=10)
    topics: list[str] = Field(default_factory=list, max_length=50)
    group_id: str | None = Field(default=None, max_length=256)


class RedisDatasourceSpec(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    host: str | None = Field(default=None, max_length=256)
    port: int | None = Field(default=None, ge=1, le=65535)
    db: int | None = Field(default=None, ge=0, le=63)


class FileLogDatasourceSpec(BaseModel):
    """File-backed log sources for the file-log evidence provider.

    Paths are relative to the provider's allowlisted roots
    (``IAP_FILE_LOG_ROOTS``) and may be globs (``* ? [...]``) to cover
    rotation schemes such as date-seq archives
    (``{app}_{DDMMYY_HHMMSS}[_vN].log``). Absolute paths and ``..`` are
    rejected at read time, never here — this spec stays declarative.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    paths: list[str] = Field(default_factory=list, max_length=20)
    service_names: list[str] = Field(default_factory=list, max_length=20)
    max_bytes_per_file: int | None = Field(default=None, ge=1024, le=104857600)


class Datasource(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    id: str = Field(..., max_length=128)
    name: str = Field(..., max_length=256)
    kind: DatasourceKind
    role: DatasourceRole
    description: str | None = Field(default=None, max_length=512)
    connection_ref: str | None = Field(default=None, max_length=512)
    read_only: bool = True
    allowed_tables: list[str] = Field(default_factory=list, max_length=200)
    query_templates: dict[str, QueryTemplate] = Field(default_factory=dict, max_length=100)
    mcp_server: str | None = Field(default=None, max_length=128)
    s3: S3DatasourceSpec | None = None
    dynamodb: DynamoDBDatasourceSpec | None = None
    kafka: KafkaDatasourceSpec | None = None
    redis: RedisDatasourceSpec | None = None
    file_log: FileLogDatasourceSpec | None = None
    tenants: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("connection_ref")
    @classmethod
    def validate_connection_ref(cls, v: str | None) -> str | None:
        if v is None or v.strip() in {"", "TBD"}:
            return v
        if v.startswith(("secret://", "env://")):
            return v
        if contains_secret_text(v):
            raise ValueError("connection_ref must be secret:// or env:// (secrets never in profile).")
        if "://" in v and "@" in v.split("://", 1)[1].split("/", 1)[0]:
            raise ValueError("connection_ref must not embed credentials; use secret:// or env://.")
        return v


class CorrelationConfig(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    fields: list[str] = Field(default_factory=list, max_length=50)
    priority: list[str] = Field(default_factory=list, max_length=50)


class InvestigationConfig(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    default_time_window: int = Field(default=3600, alias="defaultTimeWindow", ge=60, le=86400)
    max_evidence_per_query: int = Field(
        default=50, alias="maximumEvidencePerQuery", ge=1, le=500
    )
    max_investigation_duration: int = Field(
        default=7200, alias="maximumInvestigationDuration", ge=60, le=86400
    )
    enabled_evidence_types: list[str] = Field(
        default_factory=lambda: ["LOG", "TRACE", "DATABASE_STATE", "COMMIT_HISTORY"],
        alias="enabledEvidenceTypes",
        max_length=30,
    )
    correlation_depth: int = Field(default=2, alias="correlationDepth", ge=1, le=10)
    max_hypotheses: int = Field(default=5, alias="maximumHypotheses", ge=1, le=50)
    evidence_budget: dict[str, int] | None = None
    parameters_schema: dict[str, Any] | None = Field(default=None, alias="parametersSchema")


class KnowledgeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    reuse_scope: str = Field(default="tenant_app", max_length=64)
    retention_days: int = Field(default=30, ge=1, le=3650)
    min_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    enable_cross_tenant_fingerprints: bool = False


class SLO(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    name: str = Field(..., max_length=256)
    target: float = Field(...)
    window: str = Field(default="30d", max_length=32)
    objective: str | None = Field(default=None, max_length=512)


class Runbook(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    name: str = Field(..., max_length=256)
    url: str | None = Field(default=None, max_length=1024)
    path: str | None = Field(default=None, max_length=1024)
    type: str = Field(default="doc", max_length=32)
    ref: str | None = Field(default=None, max_length=512)


class ProfileSource(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    type: str = Field(default="spring_config", max_length=32)
    server_url: str = Field(..., max_length=1024)
    spring_application: str = Field(..., max_length=256)
    spring_profile: str = Field(..., max_length=256)
    label: str | None = Field(default=None, max_length=256)
    fetched_at: datetime | None = None
    refresh_cadence: str | None = Field(default=None, max_length=64)

    @field_validator("type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in VALID_SOURCE_TYPES:
            raise ValueError(f"Unknown source type '{v}'.")
        return v


class DerivationReport(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    fetched_at: datetime = Field(...)
    property_sources: list[str] = Field(default_factory=list, max_length=50)
    property_count: int = Field(default=0, ge=0)
    unmapped_keys: list[str] = Field(default_factory=list, max_length=200)
    needs_review: list[str] = Field(default_factory=list, max_length=200)
    dropped_sensitive_keys: list[str] = Field(default_factory=list, max_length=200)
    notes: list[str] = Field(default_factory=list, max_length=200)


class ReferenceDocsProfile(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    config_version: str = Field(default="1.0", alias="configVersion", min_length=1, max_length=32)
    sources: list[dict[str, Any]] = Field(default_factory=list, alias="sources", max_length=10)
    allowed_hosts: list[str] = Field(default_factory=list, alias="allowedHosts", max_length=100)
    local_allowed_roots: list[str] = Field(
        default_factory=list, alias="localAllowedRoots", max_length=20
    )
    embedding_model: str = Field(
        default="text-embedding-3-small", alias="embeddingModel", min_length=1, max_length=128
    )


class ApplicationProfile(BaseModel):
    """Canonical tenant-authored description of what the platform investigates."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    id: str = Field(..., max_length=128)
    tenant_id: str = Field(..., max_length=128)
    name: str = Field(..., max_length=256)
    description: str = Field(..., max_length=2048)
    environment: str = Field(..., max_length=64)
    environments: list[str] = Field(default_factory=list, max_length=10)
    version: int = Field(default=1, ge=1)
    status: ProfileStatus = ProfileStatus.DRAFT
    owner: str = Field(default="system", max_length=128)
    owners: list[str] = Field(default_factory=list, max_length=20)
    domain: str | None = Field(default=None, max_length=128)
    planes: list[str] = Field(default_factory=list, max_length=10)
    data_classification: str | None = Field(default=None, max_length=32)
    allowed_capabilities: list[str] = Field(default_factory=list, max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=50)
    source: ProfileSource | None = None
    derivation: DerivationReport | None = None
    services: list[Service] = Field(default_factory=list, max_length=50)
    datasources: list[Datasource] = Field(default_factory=list, max_length=100)
    mapping_source_id: str = Field(default=GENERIC_ECS_SOURCE_ID, max_length=128)
    correlation: CorrelationConfig = Field(default_factory=CorrelationConfig)
    investigation: InvestigationConfig = Field(default_factory=InvestigationConfig)
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    reference_docs: ReferenceDocsProfile | None = Field(default=None, alias="referenceDocs")
    slos: list[SLO] = Field(default_factory=list, max_length=20)
    runbooks: list[Runbook] = Field(default_factory=list, max_length=20)
    oncall: dict[str, Any] | None = None

    @field_validator("planes")
    @classmethod
    def validate_planes(cls, v: list[str]) -> list[str]:
        for plane in v:
            if plane not in VALID_PLANES:
                raise ValueError(f"Unknown plane '{plane}'. Valid: {sorted(VALID_PLANES)}.")
        return v

    @field_validator("data_classification")
    @classmethod
    def validate_classification(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_DATA_CLASSIFICATIONS:
            raise ValueError(f"Unknown data_classification '{v}'.")
        return v

    @model_validator(mode="after")
    def check_no_secrets(self) -> ApplicationProfile:
        dumped = self.model_dump(mode="json", by_alias=True)
        # Derivation/source subtrees carry key NAMES only (never values) by
        # construction — safe for logs, responses, and audit rows (§3.6).
        dumped.pop("derivation", None)
        dumped.pop("source", None)

        def _scan(node: Any, path: str, out: list[str]) -> None:
            if isinstance(node, dict):
                for k, val in node.items():
                    _scan(val, f"{path}.{k}", out)
            elif isinstance(node, list):
                for i, val in enumerate(node):
                    _scan(val, f"{path}[{i}]", out)
            elif isinstance(node, str):
                if node.strip() in {"", "TBD"} or node.startswith(("secret://", "env://")):
                    return
                if contains_secret_text(node):
                    out.append(path)

        violations: list[str] = []
        _scan(dumped, "$", violations)
        if violations:
            raise ValueError(f"Secrets are never valid profile content: {violations[:5]}")
        return self

    # -- canonical helpers (single read path, no legacy fallback) ------------
    @property
    def primary_service(self) -> Service | None:
        return self.services[0] if self.services else None

    def service_by_name(self, name: str) -> Service | None:
        for svc in self.services:
            if svc.name == name or name in svc.telemetry.service_names:
                return svc
        return None

    def all_source_roots(self) -> list[str]:
        roots: list[str] = []
        for svc in self.services:
            roots.extend(svc.code.source_roots)
            roots.extend(svc.code.paths)
        seen: list[str] = []
        for root in roots:
            if root not in seen:
                seen.append(root)
        return seen

    def all_service_names(self) -> list[str]:
        names: list[str] = []
        for svc in self.services:
            names.append(svc.name)
            names.extend(svc.telemetry.service_names)
        return list(dict.fromkeys(names))

    def all_log_indices(self) -> list[str]:
        indices: list[str] = []
        for svc in self.services:
            indices.extend(svc.telemetry.log_indices)
        return list(dict.fromkeys(indices))

    def query_template_index(self) -> dict[str, QueryTemplate]:
        index: dict[str, QueryTemplate] = {}
        for ds in self.datasources:
            for name, tpl in ds.query_templates.items():
                index[f"{ds.id}.{name}"] = tpl
        return index

    def correlation_priority(self) -> list[str]:
        if self.correlation.priority:
            return list(self.correlation.priority)
        return list(self.correlation.fields)

    def sanitized(self) -> dict[str, Any]:
        data = self.model_dump(mode="json", by_alias=True)
        for ds in data.get("datasources", []) or []:
            if not isinstance(ds, dict):
                continue
            ref = ds.get("connection_ref")
            if ref is not None:
                scheme = str(ref).split("://", 1)[0] if "://" in str(ref) else ""
                ds["connection_ref"] = {"present": True, "scheme": scheme}
            templates = ds.get("query_templates", {})
            if isinstance(templates, dict):
                ds["query_templates"] = {
                    name: {
                        "description": (tpl.get("description") if isinstance(tpl, dict) else None)
                    }
                    for name, tpl in templates.items()
                }
        return data


# Back-compat import aliases for modules not yet renamed (removed in cleanup).
CorrelationProfile = CorrelationConfig
InvestigationProfile = InvestigationConfig
