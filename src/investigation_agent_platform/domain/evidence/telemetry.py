# src/investigation_agent_platform/domain/evidence/telemetry.py
"""Trace telemetry domain models (Part 9).

Derived facts about a trace — code locations, SQL statements, table
references — each retaining the source evidence IDs it was derived from, so
every agent-facing conclusion stays traceable to observed evidence. Derived
facts are explicitly *referenced* tables/statements, never executed lineage:
parsing a log field does not establish that the database executed anything.
"""

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from investigation_agent_platform.domain.evidence.completeness import EvidenceCompleteness


class SqlParseStatus(StrEnum):
    """How far SQL extraction got for one statement chunk."""

    PARSED = "parsed"
    # Parsed only after heuristic statement splitting (whole-field parse
    # failed); statement boundaries are best-effort.
    PARTIAL = "partial"
    FAILED = "failed"
    NOT_ATTEMPTED = "not_attempted"


class CodeLocation(BaseModel):
    """One source location observed in trace evidence (deduplicated)."""

    model_config = ConfigDict(frozen=True)

    function: str | None = Field(default=None, max_length=500)
    file_path: str | None = Field(default=None, max_length=2000)
    line: int | None = Field(default=None, ge=1)
    evidence_ids: list[UUID] = Field(..., min_length=1, max_length=20)


class TableReference(BaseModel):
    """One table referenced by parsed SQL (deduplicated, qualification kept).

    Qualification is preserved as parsed — ``payments.transactions`` is never
    collapsed to ``transactions`` — and identifier case is left untouched
    (case rules are dialect-dependent; only the SQL dialect knows them).
    """

    model_config = ConfigDict(frozen=True)

    catalog: str | None = Field(default=None, max_length=500)
    schema_: str | None = Field(default=None, max_length=500, alias="schema")
    table: str = Field(..., min_length=1, max_length=500)
    evidence_ids: list[UUID] = Field(..., min_length=1, max_length=20)


class SqlStatement(BaseModel):
    """One SQL statement chunk observed in trace evidence.

    ``statement`` is literal-redacted and length-bounded — safe to display,
    never to execute. ``truncated`` marks input that exceeded the length bound
    before parsing (derived tables are best-effort in that case).
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    statement: str = Field(..., min_length=1, max_length=20000)
    evidence_id: UUID
    dialect: str | None = Field(default=None, max_length=100)
    parse_status: SqlParseStatus = SqlParseStatus.PARSED
    truncated: bool = False


class EvidenceRef(BaseModel):
    """Lightweight pointer to an examined evidence record."""

    model_config = ConfigDict(frozen=True)

    evidence_id: UUID
    evidence_type: str = Field(..., min_length=1, max_length=100)
    summary: str = Field(..., max_length=2000)


class TraceTelemetry(BaseModel):
    """Bounded, provenance-preserving investigation view of one trace."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    trace_id: str = Field(..., min_length=1, max_length=200)
    code_locations: list[CodeLocation] = Field(default_factory=list, max_length=100)
    tables: list[TableReference] = Field(default_factory=list, max_length=100)
    sql_statements: list[SqlStatement] = Field(default_factory=list, max_length=100)
    # Provider records examined (mapped items plus unmappable skips).
    evidence_count: int = Field(default=0, ge=0)
    # Provider records skipped as unmappable (see result completeness).
    skipped_record_count: int = Field(default=0, ge=0)
    # Mapped items with present-but-malformed code/db sections. Distinct from
    # "no metadata": absence is normal, malformed shape is data-quality signal.
    malformed_section_count: int = Field(default=0, ge=0)
    sql_observed: int = Field(default=0, ge=0)
    sql_parsed: int = Field(default=0, ge=0)
    sql_parse_failures: int = Field(default=0, ge=0)
    # Part 10: whether the resolved source mapping supports derivation at
    # all. False means "not supported by this source" — distinguishable from
    # "supported, found nothing" (empty lists with True).
    code_location_supported: bool = True
    sql_supported: bool = True
    completeness: EvidenceCompleteness
