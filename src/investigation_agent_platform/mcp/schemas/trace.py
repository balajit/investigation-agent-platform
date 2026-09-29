# src/investigation_agent_platform/mcp/schemas/trace.py
"""Trace investigation MCP output models (Part 9 Phase 6).

Every derived fact retains source evidence IDs. The wire format uses the
contract keys (``schema`` for table schema); Python attribute access uses
``schema_`` to avoid the ``BaseModel`` namespace.
"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.evidence.telemetry import (
    CodeLocation,
    SqlParseStatus,
    SqlStatement,
    TableReference,
    TraceTelemetry,
)
from investigation_agent_platform.mcp.schemas.common import McpCompleteness


class TraceCodeLocation(BaseModel):
    """One deduplicated source location with evidence references."""

    model_config = ConfigDict(frozen=True)

    function: str | None = Field(..., max_length=500)
    file_path: str | None = Field(..., max_length=2000)
    line: int | None = Field(..., ge=1)
    evidence_ids: list[UUID] = Field(..., min_length=1, max_length=20)

    @classmethod
    def from_domain(cls, location: CodeLocation) -> "TraceCodeLocation":
        return cls(
            function=location.function,
            file_path=location.file_path,
            line=location.line,
            evidence_ids=list(location.evidence_ids),
        )


class TraceTableReference(BaseModel):
    """One referenced table with evidence references (qualification kept)."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    catalog: str | None = Field(..., max_length=500)
    schema_: str | None = Field(..., max_length=500, alias="schema")
    table: str = Field(..., min_length=1, max_length=500)
    evidence_ids: list[UUID] = Field(..., min_length=1, max_length=20)

    @classmethod
    def from_domain(cls, reference: TableReference) -> "TraceTableReference":
        return cls(
            catalog=reference.catalog,
            schema=reference.schema_,
            table=reference.table,
            evidence_ids=list(reference.evidence_ids),
        )


class TraceSqlStatement(BaseModel):
    """One redacted SQL statement chunk (display only, never execute)."""

    model_config = ConfigDict(frozen=True)

    statement: str = Field(..., min_length=1, max_length=20000)
    evidence_id: UUID
    dialect: str | None = Field(..., max_length=100)
    parse_status: SqlParseStatus
    truncated: bool

    @classmethod
    def from_domain(cls, statement: SqlStatement) -> "TraceSqlStatement":
        return cls(
            statement=statement.statement,
            evidence_id=statement.evidence_id,
            dialect=statement.dialect,
            parse_status=statement.parse_status,
            truncated=statement.truncated,
        )


class TraceEvidenceRef(BaseModel):
    """Lightweight evidence pointer for trace-derived context."""

    model_config = ConfigDict(frozen=True)

    evidence_id: UUID
    evidence_type: str = Field(..., max_length=100)
    summary: str = Field(..., max_length=2000)


class InvestigateTraceOutput(BaseModel):
    """Bounded trace view with completeness state."""

    model_config = ConfigDict(frozen=True)

    trace_id: str = Field(..., min_length=1, max_length=200)
    code_locations: list[TraceCodeLocation] = Field(..., max_length=100)
    tables: list[TraceTableReference] = Field(..., max_length=100)
    sql_statements: list[TraceSqlStatement] = Field(..., max_length=100)
    evidence: list[TraceEvidenceRef] = Field(..., max_length=100)
    # Part 10: distinguishes "source has no such data" (False) from
    # "supported, found nothing" (True with empty lists).
    code_location_supported: bool
    sql_supported: bool
    completeness: McpCompleteness

    @classmethod
    def from_domain(
        cls, telemetry: TraceTelemetry, evidence_refs: list[TraceEvidenceRef]
    ) -> "InvestigateTraceOutput":
        return cls(
            trace_id=telemetry.trace_id,
            code_locations=[TraceCodeLocation.from_domain(loc) for loc in telemetry.code_locations],
            tables=[TraceTableReference.from_domain(table) for table in telemetry.tables],
            sql_statements=[
                TraceSqlStatement.from_domain(stmt) for stmt in telemetry.sql_statements
            ],
            evidence=evidence_refs,
            code_location_supported=telemetry.code_location_supported,
            sql_supported=telemetry.sql_supported,
            completeness=McpCompleteness.from_domain(telemetry.completeness),
        )
