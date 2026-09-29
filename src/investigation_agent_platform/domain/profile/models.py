# src/investigation_agent_platform/domain/profile/models.py
"""Application Profile domain specifications and configuration models."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from investigation_agent_platform.domain.observability.mapping import GENERIC_ECS_SOURCE_ID


class ProfileStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    ARCHIVED = "ARCHIVED"


class ObservabilityProfile(BaseModel):
    """Observability provider mapping configuration."""

    model_config = ConfigDict(frozen=True)

    provider: str = Field(..., max_length=128)
    indices: list[str] = Field(..., max_length=50)
    timestamp_field: str = Field(..., alias="timestampField", max_length=128)
    service_field: str = Field(..., alias="serviceField", max_length=128)
    environment_field: str = Field(..., alias="environmentField", max_length=128)
    session_field: str = Field(..., alias="sessionField", max_length=128)
    request_field: str = Field(..., alias="requestField", max_length=128)
    trace_field: str = Field(..., alias="traceField", max_length=128)
    log_level_field: str = Field(..., alias="logLevelField", max_length=128)
    # Part 10: links this profile to an ObservabilitySourceMapping
    # (config/observability-mappings/<source_id>.yaml). Defaults to the
    # built-in generic-ECS mapping, which reproduces pre-Part-10 behavior.
    # The legacy singular *Field attributes above are superseded for the
    # Elastic runtime path but left untouched for other consumers.
    mapping_source_id: str = Field(
        default=GENERIC_ECS_SOURCE_ID, alias="mappingSourceId", max_length=128
    )


class StateProfile(BaseModel):
    """State database query mapping and template repository."""

    model_config = ConfigDict(frozen=True)

    provider: str = Field(..., max_length=128)
    database: str = Field(..., max_length=128)
    schema_name: str = Field(..., alias="schema", max_length=128)
    tables: list[str] = Field(..., max_length=100)
    primary_identifiers: list[str] = Field(..., alias="primaryIdentifiers", max_length=50)
    state_fields: list[str] = Field(..., alias="stateFields", max_length=100)
    timestamp_fields: list[str] = Field(..., alias="timestampFields", max_length=50)
    query_templates: dict[str, str] = Field(..., alias="queryTemplates", max_length=50)

    @field_validator("query_templates")
    @classmethod
    def validate_query_templates_safety(cls, templates: dict[str, str]) -> dict[str, str]:
        forbidden_keywords = ["DROP", "DELETE", "TRUNCATE", "ALTER", "GRANT", "REVOKE"]
        for name, query in templates.items():
            query_upper = query.upper()
            for kw in forbidden_keywords:
                if f" {kw} " in f" {query_upper} ":
                    raise ValueError(f"Forbidden DDL/DML keyword '{kw}' in template '{name}'.")
        return templates


class CodeProfile(BaseModel):
    """Code repository and build configuration."""

    model_config = ConfigDict(frozen=True)

    provider: str = Field(..., max_length=128)
    repository: str = Field(..., max_length=256)
    default_branch: str = Field(..., alias="defaultBranch", max_length=128)
    language: str = Field(..., max_length=64)
    source_roots: list[str] = Field(..., alias="sourceRoots", max_length=50)
    build_system: str = Field(..., alias="buildSystem", max_length=128)
    module_structure: str = Field(..., alias="moduleStructure", max_length=128)
    # Layer 3 topology: canonical repository identity (distinct from the
    # access/build locator above). Optional so existing profiles remain valid
    # until a repository is registered in the topology registry.
    repository_id: str | None = Field(default=None, alias="repositoryId", max_length=128)
    # ISSUE-5: the runtime service/queue name this repository's deployed
    # workload is identified by in distributed traces (OTel `service.name`).
    # Optional — cross-repository trace hops are simply unavailable for a
    # profile without one, never guessed from `repository`/`application_id`.
    service_name: str | None = Field(default=None, alias="serviceName", max_length=256)

    @field_validator("repository", "source_roots")
    @classmethod
    def validate_paths(cls, v: Any) -> Any:
        if isinstance(v, str) and (".." in v or "\0" in v):
            raise ValueError("Path traversal sequence in CodeProfile string.")
        elif isinstance(v, list):
            for item in v:
                if ".." in item or "\0" in item:
                    raise ValueError("Path traversal sequence in CodeProfile source_roots.")
        return v


class CorrelationProfile(BaseModel):
    """Correlation priority field specifications."""

    model_config = ConfigDict(frozen=True)

    fields: list[str] = Field(..., max_length=50)


class InvestigationProfile(BaseModel):
    """Investigation operational constraints and bounds."""

    model_config = ConfigDict(frozen=True)

    default_time_window: int = Field(default=1800, alias="defaultTimeWindow", ge=60, le=86400)
    max_evidence_per_query: int = Field(default=25, alias="maximumEvidencePerQuery", ge=1, le=500)
    max_investigation_duration: int = Field(
        default=1800, alias="maximumInvestigationDuration", ge=60, le=86400
    )
    enabled_evidence_types: list[str] = Field(
        default_factory=lambda: ["LOG", "TRACE", "DATABASE_STATE"],
        alias="enabledEvidenceTypes",
        max_length=20,
    )
    correlation_depth: int = Field(default=2, alias="correlationDepth", ge=1, le=10)
    max_hypotheses: int = Field(default=5, alias="maximumHypotheses", ge=1, le=50)


class ApplicationProfile(BaseModel):
    """Aggregate domain configuration profile for onboarded targets."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(..., max_length=128)
    tenant_id: str = Field(..., max_length=128)
    name: str = Field(..., max_length=256)
    description: str = Field(..., max_length=2048)
    environment: str = Field(..., max_length=64)
    version: int = Field(default=1, ge=1)
    status: ProfileStatus = ProfileStatus.ACTIVE
    owner: str = Field(default="system", max_length=128)
    allowed_capabilities: list[str] = Field(default_factory=list, max_length=50)
    observability_configuration: ObservabilityProfile = Field(alias="observability")
    state_configuration: StateProfile = Field(alias="state")
    code_configuration: CodeProfile = Field(alias="code")
    correlation_configuration: CorrelationProfile = Field(alias="correlation")
    investigation_configuration: InvestigationProfile = Field(alias="investigation")
