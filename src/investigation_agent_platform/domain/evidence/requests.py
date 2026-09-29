# src/investigation_agent_platform/domain/evidence/requests.py
"""Evidence request models with input validation and security guardrails."""

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from investigation_agent_platform.application.investigation.validator import _has_traversal
from investigation_agent_platform.domain.code.models import SymbolType
from investigation_agent_platform.domain.evidence.models import LogSeverity

# Part 10: identifier keys are validated against the resolved mapping's
# logical keys at the provider boundary (`validate_identifier_items`), not
# here — no single static set is correct for all sources. The length checks
# below stay as name-agnostic defense in depth.
MAX_IDENTIFIER_VALUE_LENGTH = 500
MAX_KEYWORD_LENGTH = 200
MAX_SERVICE_LENGTH = 200
MAX_IDENTIFIER_KEY_LENGTH = 128
MAX_TRACE_ID_LENGTH = 200

ENVIRONMENT_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
TRACE_ID_PATTERN = r"^[A-Za-z0-9._:-]+$"


class TimeRange(BaseModel):
    """Time window specification for evidence filtering."""

    model_config = ConfigDict(frozen=True)

    start_time: datetime
    end_time: datetime

    @field_validator("end_time")
    @classmethod
    def validate_time_range_bounds(cls, v: datetime, info: ValidationInfo) -> datetime:
        start_time = info.data.get("start_time")
        if start_time and v < start_time:
            raise ValueError("end_time cannot precede start_time.")
        return v


class RuntimeEvidenceRequest(BaseModel):
    """Query parameters for retrieving runtime telemetry, logs, and traces."""

    model_config = ConfigDict(frozen=True)

    # Part 9: validated against the environment allowlist pattern before the
    # value is interpolated into any provider index expression.
    environment: str = Field(..., max_length=64, pattern=ENVIRONMENT_PATTERN)
    time_range: TimeRange | None = None
    identifiers: dict[str, str] = Field(default_factory=dict, max_length=20)
    services: list[Annotated[str, Field(min_length=1, max_length=MAX_SERVICE_LENGTH)]] = Field(
        default_factory=list, max_length=10
    )
    # Part 9: closed vocabulary. Unknown values are rejected — never filtered
    # out silently, which would broaden the query into an unfiltered one.
    severities: list[LogSeverity] = Field(default_factory=list, max_length=10)
    keywords: list[Annotated[str, Field(min_length=1, max_length=MAX_KEYWORD_LENGTH)]] = Field(
        default_factory=list, max_length=20
    )
    query_string: str | None = Field(default=None, max_length=1024)
    # Part 10, RESERVED legacy field: accepted and sanitized, but never
    # executed by the Elastic provider (build_query_body has no code path
    # that reads it — free-text search goes through keywords/multi_match).
    # The sole consumer is the *outbound* McpEvidenceAdapter, which forwards
    # it as the `query` argument of third-party `search_logs` tools under
    # their own contract. Do not add new readers; do not remove without
    # migrating that adapter.
    # Part 9: capped at the provider ceiling (200). Internal callers clamp to
    # this bound; agent-facing schemas enforce stricter maxima on top.
    limit: int = Field(default=100, ge=1, le=200)
    cursor: str | None = Field(default=None, max_length=4096)
    # Part 10, internal only: selects the ObservabilitySourceMapping used for
    # physical field resolution. Stamped server-side from the investigation's
    # profile (services) or bridge profile — never set from agent input, as
    # MCP handlers build requests field-by-field without this parameter.
    # None resolves to the built-in generic-ECS mapping.
    mapping_source_id: str | None = Field(default=None, max_length=128)

    @field_validator("identifiers")
    @classmethod
    def validate_identifiers(cls, v: dict[str, str]) -> dict[str, str]:
        for key, value in v.items():
            if len(key) > MAX_IDENTIFIER_KEY_LENGTH:
                raise ValueError(f"Identifier key exceeds {MAX_IDENTIFIER_KEY_LENGTH} characters.")
            # Key allowlisting is per-mapping at the provider boundary
            # (`validate_identifier_items`); the domain layer enforces only
            # shape bounds here so no source's key vocabulary is privileged.
            if not isinstance(value, str) or not (1 <= len(value) <= MAX_IDENTIFIER_VALUE_LENGTH):
                raise ValueError(
                    f"Identifier value for '{key}' must be 1..{MAX_IDENTIFIER_VALUE_LENGTH} characters."
                )
        return v

    @field_validator("query_string")
    @classmethod
    def sanitize_query_string(cls, v: str | None) -> str | None:
        if v is not None and (" DROP " in v.upper() or " DELETE " in v.upper() or "--" in v):
            raise ValueError("Forbidden query syntax detected in query_string.")
        return v


class TraceTelemetryRequest(BaseModel):
    """Bounded request for a trace-centered telemetry investigation (Part 9)."""

    model_config = ConfigDict(frozen=True)

    trace_id: str = Field(
        ..., min_length=1, max_length=MAX_TRACE_ID_LENGTH, pattern=TRACE_ID_PATTERN
    )
    environment: str = Field(..., max_length=64, pattern=ENVIRONMENT_PATTERN)
    time_range: TimeRange | None = None
    max_evidence: int = Field(default=100, ge=1, le=100)
    # Part 10, internal only: see RuntimeEvidenceRequest.mapping_source_id.
    mapping_source_id: str | None = Field(default=None, max_length=128)


class ApplicationStateRequest(BaseModel):
    """Parameterized request for state evidence using pre-defined database templates."""

    model_config = ConfigDict(frozen=True)

    environment: str = Field(..., max_length=64)
    template_id: str = Field(..., max_length=128)
    parameters: dict[str, Any] = Field(default_factory=dict, max_length=50)
    limit: int = Field(default=50, ge=1, le=200)


class CodeSearchRequest(BaseModel):
    """Request specification for searching source code repositories."""

    model_config = ConfigDict(frozen=True)

    repository: str = Field(..., max_length=256)
    revision: str = Field(default="HEAD", max_length=128)
    query: str = Field(..., max_length=512)
    language: str | None = Field(default=None, max_length=64)
    path_prefix: str | None = Field(default=None, max_length=512)
    symbol_type: SymbolType | None = None
    limit: int = Field(default=20, ge=1, le=100)

    @field_validator("repository", "path_prefix")
    @classmethod
    def validate_safe_path(cls, v: str | None) -> str | None:
        if v is not None and _has_traversal(v):
            raise ValueError("Unsafe path traversal element in repository or path_prefix.")
        return v


class SymbolRequest(BaseModel):
    """Request to locate specific code symbols across codebase AST indexes."""

    model_config = ConfigDict(frozen=True)

    repository: str = Field(..., max_length=256)
    revision: str = Field(default="HEAD", max_length=128)
    symbol_name: str = Field(..., max_length=256)
    symbol_type: SymbolType | None = None


class SourceRequest(BaseModel):
    """Request to fetch specific source code file contents or bounded line ranges."""

    model_config = ConfigDict(frozen=True)

    repository: str = Field(..., max_length=256)
    revision: str = Field(default="HEAD", max_length=128)
    file_path: str = Field(..., max_length=1024)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, v: str) -> str:
        if _has_traversal(v):
            raise ValueError("Unsafe directory traversal sequence in file_path.")
        return v


class CallGraphRequest(BaseModel):
    """Request to generate call hierarchies for target symbols."""

    model_config = ConfigDict(frozen=True)

    repository: str = Field(..., max_length=256)
    revision: str = Field(default="HEAD", max_length=128)
    symbol: str = Field(..., max_length=256)
    direction: str = Field(default="BOTH", pattern="^(INBOUND|OUTBOUND|BOTH)$")
    depth: int = Field(default=2, ge=1, le=5)


class CodeHistoryRequest(BaseModel):
    """Request to retrieve commit history for specified file paths or symbols."""

    model_config = ConfigDict(frozen=True)

    repository: str = Field(..., max_length=256)
    file_path: str = Field(..., max_length=1024)
    symbol: str | None = Field(default=None, max_length=256)
    limit: int = Field(default=10, ge=1, le=50)

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, v: str) -> str:
        if _has_traversal(v):
            raise ValueError("Unsafe directory traversal sequence in file_path.")
        return v
