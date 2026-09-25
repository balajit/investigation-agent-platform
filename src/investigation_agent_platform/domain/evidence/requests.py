# src/investigation_agent_platform/domain/evidence/requests.py
"""Evidence request models with input validation and security guardrails."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from investigation_agent_platform.application.investigation.validator import _has_traversal
from investigation_agent_platform.domain.code.models import SymbolType


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

    environment: str = Field(..., max_length=64)
    time_range: TimeRange | None = None
    identifiers: dict[str, str] = Field(default_factory=dict, max_length=20)
    services: list[str] = Field(default_factory=list, max_length=50)
    severities: list[str] = Field(default_factory=list, max_length=20)
    keywords: list[str] = Field(default_factory=list, max_length=50)
    query_string: str | None = Field(default=None, max_length=1024)
    limit: int = Field(default=100, ge=1, le=500)
    cursor: str | None = Field(default=None, max_length=256)

    @field_validator("query_string")
    @classmethod
    def sanitize_query_string(cls, v: str | None) -> str | None:
        if v is not None and (" DROP " in v.upper() or " DELETE " in v.upper() or "--" in v):
            raise ValueError("Forbidden query syntax detected in query_string.")
        return v


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
