# src/investigation_agent_platform/domain/code/models.py
"""Domain models for code symbol AST analysis and structural representations."""

from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from investigation_agent_platform.domain.provenance.models import SourceLocation


class SymbolType(StrEnum):
    """Categories of code symbols for AST indexing and query dispatch."""

    CLASS = "CLASS"
    INTERFACE = "INTERFACE"
    METHOD = "METHOD"
    FUNCTION = "FUNCTION"
    FIELD = "FIELD"
    CONSTRUCTOR = "CONSTRUCTOR"
    ENUM = "ENUM"


class CodeFactStatus(StrEnum):
    RESOLVED = "RESOLVED"
    PARTIAL = "PARTIAL"
    UNRESOLVED = "UNRESOLVED"


class CodeValueSemantics(StrEnum):
    EXPLICIT_INITIALIZER = "EXPLICIT_INITIALIZER"
    LANGUAGE_DEFAULT = "LANGUAGE_DEFAULT"
    POST_CONSTRUCTION = "POST_CONSTRUCTION"
    LATER_MUTABLE = "LATER_MUTABLE"
    RUNTIME_EFFECTIVE = "RUNTIME_EFFECTIVE"


class CodeFactResolution(BaseModel):
    """Ephemeral result of deterministic source-level field analysis.

    This model is carried with existing evidence; it is not a persistence
    aggregate. A resolved value describes the named semantics only, never an
    arbitrary later/runtime-effective field value.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: CodeFactStatus
    repository: str = Field(..., min_length=1, max_length=256)
    revision: str | None = Field(default=None, max_length=128)
    declaring_type: str = Field(..., min_length=1, max_length=512)
    field_name: str = Field(..., min_length=1, max_length=256)
    field_type: str = Field(..., min_length=1, max_length=256)
    field_category: Literal["INSTANCE_FIELD", "STATIC_FIELD"]
    value_semantics: CodeValueSemantics
    raw_initializer: str | None = Field(default=None, max_length=512)
    resolved_value: str | None = Field(default=None, max_length=512)
    source_evidence_ids: tuple[UUID, ...] = ()
    declaration_location: SourceLocation
    limitation_codes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_resolution(self) -> "CodeFactResolution":
        if self.status == CodeFactStatus.RESOLVED and self.resolved_value is None:
            raise ValueError("resolved code facts require a resolved_value")
        if self.status == CodeFactStatus.RESOLVED and (
            not self.revision
            or not self.declaration_location.file_path
            or not self.declaration_location.revision
        ):
            raise ValueError(
                "resolved code facts require source path and immutable revision provenance"
            )
        return self


class CodeSymbol(BaseModel):
    """Structural definition of a source code symbol extracted via AST parsing."""

    model_config = ConfigDict(frozen=True)

    symbol_id: UUID = Field(default_factory=uuid4)
    name: str = Field(..., max_length=256)
    symbol_type: SymbolType
    repository: str = Field(..., max_length=256)
    file_path: str = Field(..., max_length=1024)
    line_start: int = Field(..., ge=1)
    line_end: int = Field(..., ge=1)
    signature: str = Field(..., max_length=2048)
    revision: str = Field(default="HEAD", max_length=128)
    documentation: str | None = Field(default=None, max_length=4096)
    parent_symbol: str | None = Field(default=None, max_length=256)

    @field_validator("file_path")
    @classmethod
    def validate_file_path_safety(cls, value: str) -> str:
        if ".." in value or "\0" in value:
            raise ValueError(
                "File path contains unsafe directory traversal sequences or null bytes."
            )
        return value

    @field_validator("line_end")
    @classmethod
    def validate_line_range(cls, value: int, info: ValidationInfo) -> int:
        line_start = info.data.get("line_start")
        if line_start is not None and value < line_start:
            raise ValueError("line_end cannot be less than line_start.")
        return value


class CallGraphNode(BaseModel):
    """Node representing a function/method call in execution paths."""

    model_config = ConfigDict(frozen=True)

    symbol: CodeSymbol
    depth: int = Field(..., ge=0)
    invocations: list[str] = Field(default_factory=list, max_length=100)


class CallGraph(BaseModel):
    """Call tree topology representing static or dynamic invocation hierarchies."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., max_length=128)
    investigation_id: UUID
    root_symbol: str = Field(..., max_length=256)
    revision: str = Field(default="HEAD", max_length=128)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    nodes: list[CallGraphNode] = Field(default_factory=list, max_length=500)


class ExceptionPath(BaseModel):
    """Traceability representation linking exception throwing locations to handlers."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., max_length=128)
    investigation_id: UUID
    exception_class: str = Field(..., max_length=256)
    throw_location: CodeSymbol
    handler_locations: list[CodeSymbol] = Field(default_factory=list, max_length=50)


class DatabaseOperation(BaseModel):
    """Source code mapping linking code locations to database queries or entities."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., max_length=128)
    investigation_id: UUID
    target_entity_or_table: str = Field(..., max_length=256)
    operation_type: str = Field(..., max_length=64)
    source_symbol: CodeSymbol
    raw_query_snippet: str | None = Field(
        default=None, max_length=2048, description="Sanitized query snippet"
    )
    is_classified_sensitive: bool = Field(default=False)

    @field_validator("raw_query_snippet")
    @classmethod
    def validate_sensitive_snippet(cls, value: str | None, info: ValidationInfo) -> str | None:
        is_sensitive = info.data.get("is_classified_sensitive", False)
        if is_sensitive and value is not None:
            raise ValueError(
                "Raw query snippet classified as sensitive must be sanitized/redacted before assignment."
            )
        return value
