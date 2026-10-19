# src/investigation_agent_platform/api/v1/schemas/profiles.py
"""Response models for the profiles router (appl-profile 1.0 §5).

The wire shape is the SANITIZED profile: no connection values, no template SQL.
ProfileView.from_domain() is the single conversion point; declaring
response_model=ProfileView is safe precisely because it cannot express a secret.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.profile.models import ApplicationProfile


class ConnectionRefView(BaseModel):
    """Redacted connection reference. NEVER the raw `secret://`/`env://` value."""

    model_config = ConfigDict(extra="forbid")

    present: bool = Field(description="Whether a connection reference is configured.")
    scheme: str = Field(description="Reference scheme, e.g. 'secret' or 'env'.")


class QueryTemplateSummary(BaseModel):
    """Redacted query template: description only, NEVER the SQL body."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(
        default=None, description="Human description; SQL is never returned."
    )


class ServiceTelemetryView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    log_indices: list[str]
    service_names: list[str]
    trace_backend: str | None
    metrics_backend: str | None
    default_lookback_s: int | None
    session_key: str | None = None
    tracking_key: str | None = None


class ServiceCodeView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    org: str | None
    repos: list[str]
    default_branch: str
    language: str | None
    source_roots: list[str]
    build_system: str | None
    module_structure: str | None
    paths: list[str]


class ServiceDependencyView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: str
    type: str
    critical: bool


class S3DatasourceSpecView(BaseModel):
    """Non-secret object-store coordinates (bucket/prefix/region only)."""

    model_config = ConfigDict(extra="forbid")

    bucket: str
    prefix: str | None = None
    region: str | None = None
    endpoint_url: str | None = None


class DynamoDBDatasourceSpecView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table: str
    region: str | None = None
    endpoint_url: str | None = None


class KafkaDatasourceSpecView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bootstrap_servers: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    group_id: str | None = None


class RedisDatasourceSpecView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str | None = None
    port: int | None = None
    db: int | None = None


class FileLogDatasourceSpecView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: list[str] = []
    service_names: list[str] = []
    max_bytes_per_file: int | None = None


class ProbeableEndpointView(BaseModel):
    """Wire shape for profile-declared PROBE_API endpoints (P2-2)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    description: str | None = None
    method: str = "GET"
    base_url: str
    path_template: str
    path_placeholders: list[str] = Field(default_factory=list)
    allowed_query_params: list[str] = Field(default_factory=list)


class ServiceView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    role: str | None
    description: str | None
    telemetry: ServiceTelemetryView
    code: ServiceCodeView
    dependencies: list[ServiceDependencyView]
    endpoints: list[str]
    repository_id: str | None
    probeable_endpoints: list[ProbeableEndpointView] = Field(default_factory=list)


class DatasourceView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    kind: str
    role: str
    description: str | None
    connection_ref: ConnectionRefView | None
    read_only: bool
    allowed_tables: list[str]
    query_templates: dict[str, QueryTemplateSummary]
    mcp_server: str | None
    schema_name: str | None = None
    s3: S3DatasourceSpecView | None
    dynamodb: DynamoDBDatasourceSpecView | None
    kafka: KafkaDatasourceSpecView | None
    redis: RedisDatasourceSpecView | None
    file_log: FileLogDatasourceSpecView | None
    tenants: list[str]
    relationships: list[EntityRelationshipView] = Field(default_factory=list)
    session_entity: str | None = None
    session_key: str | None = None


class EntityRelationshipView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_entity: str
    from_key: str
    to_entity: str
    to_key: str
    source: str = "derived"
    tombstoned: bool = False


class DomainHubView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    score: float = 0.0
    fan_in: int = 0
    fan_out: int = 0
    witnesses: list[str] = Field(default_factory=list)
    fingerprint: str = ""


class CorrelationConfigView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: list[str]
    priority: list[str]


class AttributeExpectationView(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    required: bool = False
    allowed_values: list[str] = Field(default_factory=list, alias="allowedValues")
    min_value: float | None = Field(default=None, alias="minValue")
    max_value: float | None = Field(default=None, alias="maxValue")


class EndpointExpectationView(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    endpoint_id: str = Field(alias="endpointId")
    attributes: list[AttributeExpectationView] = Field(default_factory=list)


class ExpectationsConfigView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoints: list[EndpointExpectationView] = Field(default_factory=list)


class InvestigationConfigView(BaseModel):
    """Aliased field names are the wire contract — keep them verbatim."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    default_time_window: int = Field(alias="defaultTimeWindow")
    max_evidence_per_query: int = Field(alias="maximumEvidencePerQuery")
    max_investigation_duration: int = Field(alias="maximumInvestigationDuration")
    enabled_evidence_types: list[str] = Field(alias="enabledEvidenceTypes")
    correlation_depth: int = Field(alias="correlationDepth")
    max_hypotheses: int = Field(alias="maximumHypotheses")
    evidence_budget: dict[str, int] | None = None
    parameters_schema: dict[str, Any] | None = Field(default=None, alias="parametersSchema")


class KnowledgeConfigView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reuse_scope: str
    retention_days: int
    min_confidence: float
    enable_cross_tenant_fingerprints: bool


class ProfileSourceView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    server_url: str
    spring_application: str
    spring_profile: str
    label: str | None = None
    fetched_at: str | None = None
    refresh_cadence: str | None = None


class DerivationReportView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fetched_at: str
    property_sources: list[str]
    property_count: int
    unmapped_keys: list[str]
    needs_review: list[str]
    dropped_sensitive_keys: list[str]
    notes: list[str]


class ReferenceDocsProfileView(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    config_version: str = Field(alias="configVersion")
    sources: list[dict[str, Any]]
    allowed_hosts: list[str] = Field(alias="allowedHosts")
    local_allowed_roots: list[str] = Field(alias="localAllowedRoots")
    embedding_model: str = Field(alias="embeddingModel")


class SloView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    target: float
    window: str
    objective: str | None


class RunbookView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    url: str | None
    path: str | None
    type: str
    ref: str | None


class ProfileView(BaseModel):
    """Sanitized ApplicationProfile — the ONLY profile shape that may cross the wire.

    `extra="forbid"` is deliberate: `from_domain` validates `sanitized()` output,
    so any future field added to the domain model without a matching view field
    fails loudly here rather than leaking silently.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    tenant_id: str
    name: str
    description: str
    environment: str
    environments: list[str]
    version: int
    status: str
    owner: str
    owners: list[str]
    domain: str | None
    planes: list[str]
    data_classification: str | None
    allowed_capabilities: list[str]
    tags: list[str]
    source: ProfileSourceView | None
    derivation: DerivationReportView | None
    services: list[ServiceView]
    datasources: list[DatasourceView]
    domain_hubs: list[DomainHubView] = Field(default_factory=list)
    mapping_source_id: str
    correlation: CorrelationConfigView
    investigation: InvestigationConfigView
    knowledge: KnowledgeConfigView
    expectations: ExpectationsConfigView = Field(default_factory=ExpectationsConfigView)
    reference_docs: ReferenceDocsProfileView | None = Field(alias="referenceDocs")
    slos: list[SloView]
    runbooks: list[RunbookView]
    oncall: dict[str, Any] | None

    @classmethod
    def from_domain(cls, profile: ApplicationProfile) -> ProfileView:
        """The single conversion point. Reuses the domain redaction verbatim."""
        return cls.model_validate(profile.sanitized())


class ProfileRevisionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: int
    status: str
    name: str
    environment: str


class ProfileDetail(BaseModel):
    """`{profile, version[, idempotent_replay]}` — GET/POST/PUT single-profile responses."""

    model_config = ConfigDict(extra="forbid")

    profile: ProfileView
    version: int
    idempotent_replay: bool | None = None


class ProfileDeleteAck(BaseModel):
    """DELETE result. `version` is the deleted revision, or null for whole-app deletes."""

    model_config = ConfigDict(extra="forbid")

    id: str
    version: int | None = None
    deleted: int


class GeneratedProfileView(BaseModel):
    """POST ...:generate. `version` absent when persist=false."""

    model_config = ConfigDict(extra="forbid")

    profile: ProfileView
    version: int | None = None
    derivation: dict[str, Any]
    persisted: bool
    warnings: list[str]


class GeneratedRefreshView(BaseModel):
    """POST ...:refresh. Detail keys present only when changed."""

    model_config = ConfigDict(extra="forbid")

    changed: bool
    version: int
    diff: list[str] | None = None
    profile: ProfileView | None = None
    warnings: list[str] | None = None


class SpringDerivationView(BaseModel):
    """POST ...:derive-from-spring. `version` absent when persist=false."""

    model_config = ConfigDict(extra="forbid")

    profile: ProfileView
    version: int | None = None
    derivation: DerivationReportView
    persisted: bool
    warnings: list[str]


class SpringRefreshView(BaseModel):
    """POST ...:refresh-from-spring. Detail keys present only when changed."""

    model_config = ConfigDict(extra="forbid")

    changed: bool
    version: int
    diff: list[str] | None = None
    profile: ProfileView | None = None
    warnings: list[str] | None = None


class SpringDriftView(BaseModel):
    """GET ...:spring-drift. Always fully populated, never writes."""

    model_config = ConfigDict(extra="forbid")

    changed: bool
    version: int
    diff: list[str]
    needs_review: list[str]
    warnings: list[str]
