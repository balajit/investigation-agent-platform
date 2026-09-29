# src/investigation_agent_platform/infrastructure/evidence/logs/trace_telemetry.py
"""Trace telemetry extraction from runtime evidence (Part 9).

Reusable investigation semantics over ``Evidence`` records: code locations,
SQL statements, and table references — each retaining source evidence IDs.
The extractor never executes SQL, never fabricates locations, and always
reports whether its bounded view is complete.

Layering: ``TraceTelemetryExtractor`` retrieves through ``AsyncElasticAdapter``
(reusing its limits and security policies), then delegates per-record
derivation to pure helpers plus ``SqlEvidenceExtractor``. ``Evidence`` input
is already projected and bounded upstream; this module additionally bounds
everything it derives.
"""

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import sqlglot
import sqlglot.expressions as exp

from investigation_agent_platform.domain.common.exceptions import PlatformConfigurationError
from investigation_agent_platform.domain.evidence.completeness import (
    CompletenessReason,
    EvidenceCompleteness,
)
from investigation_agent_platform.domain.evidence.models import Evidence
from investigation_agent_platform.domain.evidence.requests import (
    RuntimeEvidenceRequest,
    TraceTelemetryRequest,
)
from investigation_agent_platform.domain.evidence.telemetry import (
    CodeLocation,
    SqlParseStatus,
    SqlStatement,
    TableReference,
    TraceTelemetry,
)
from investigation_agent_platform.domain.observability.mapping import (
    GENERIC_ECS_SOURCE_ID,
    CodeLocationMapping,
    FieldMapping,
    ObservabilitySourceMapping,
    generic_ecs_mapping,
)
from investigation_agent_platform.domain.profile.models import ObservabilityProfile
from investigation_agent_platform.infrastructure.configuration.mapping_registry import (
    MappingProfileRegistry,
)
from investigation_agent_platform.infrastructure.evidence.runtime.field_resolver import (
    resolve_value,
)
from investigation_agent_platform.ports.evidence.runtime import RuntimeEvidenceProviderProtocol
from investigation_agent_platform.ports.evidence.trace import (
    TraceTelemetryDerivationPort,
    TraceTelemetryPort,
)

logger = logging.getLogger(__name__)

MAX_SQL_STATEMENT_CHARS = 20000
MAX_FUNCTION_CHARS = 500
MAX_FILE_PATH_CHARS = 2000
MAX_DIALECT_CHARS = 100
MAX_EVIDENCE_REFS = 20
MAX_RESULT_ITEMS = 100


@dataclass(frozen=True)
class ParsedTable:
    catalog: str | None
    schema: str | None
    table: str


@dataclass(frozen=True)
class SqlExtraction:
    """Result of extracting SQL from one log field (never executed)."""

    statements: tuple[SqlStatement, ...]
    tables: tuple[ParsedTable, ...]
    observed: int
    parsed: int
    parse_failures: int


def _redact_literals(expression: exp.Expr, dialect: str | None) -> str:
    """Render the statement with every literal replaced by a placeholder."""
    redacted = expression.transform(
        lambda node: exp.Placeholder() if isinstance(node, exp.Literal) else node,
        copy=True,
    )
    return redacted.sql(dialect=dialect) if dialect else redacted.sql()


@dataclass(frozen=True)
class SqlEvidenceExtractor:
    """Parse-only SQL extraction with literal redaction and hard bounds.

    Multi-statement fields are parsed statement-by-statement: a whole-field
    parse is attempted first, and only on failure are heuristic ``;``-split
    chunks parsed individually (those carry ``PARTIAL`` status — boundaries
    are best-effort). Unparseable chunks yield ``FAILED`` entries carrying a
    size placeholder instead of raw text, so secrets embedded in rejected
    SQL can never flow to agent output. Nothing here ever executes.
    """

    max_statement_chars: int = MAX_SQL_STATEMENT_CHARS

    def extract(self, *, evidence_id: UUID, sql: str, dialect: str | None = None) -> SqlExtraction:
        """Extract statements and tables from one non-empty SQL string."""
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("SqlEvidenceExtractor.extract requires a non-empty SQL string.")
        if dialect is not None and (not isinstance(dialect, str) or not dialect.strip()):
            raise ValueError("Dialect must be a non-empty string or None.")
        clean_dialect = dialect.strip()[:MAX_DIALECT_CHARS] if dialect else None

        truncated = len(sql) > self.max_statement_chars
        bounded = sql[: self.max_statement_chars] if truncated else sql

        chunks = self._parse_chunks(bounded, clean_dialect)
        if not chunks:
            # Non-empty input that yields no statements (e.g. comment-only):
            # record the failure explicitly rather than vanishing.
            chunks = [(bounded.strip(), None, False)]

        statements: list[SqlStatement] = []
        tables: list[ParsedTable] = []
        parsed = 0
        failures = 0
        for raw_chunk, expression, heuristic in chunks:
            if expression is None:
                failures += 1
                statements.append(
                    SqlStatement(
                        statement=f"[UNPARSEABLE SQL STATEMENT ({len(raw_chunk)} CHARS)]",
                        evidence_id=evidence_id,
                        dialect=clean_dialect,
                        parse_status=SqlParseStatus.FAILED,
                        truncated=truncated,
                    )
                )
                continue
            parsed += 1
            try:
                rendered = _redact_literals(expression, clean_dialect)
            except Exception:
                failures += 1
                parsed -= 1
                statements.append(
                    SqlStatement(
                        statement=f"[UNPARSEABLE SQL STATEMENT ({len(raw_chunk)} CHARS)]",
                        evidence_id=evidence_id,
                        dialect=clean_dialect,
                        parse_status=SqlParseStatus.FAILED,
                        truncated=truncated,
                    )
                )
                continue
            if len(rendered) > self.max_statement_chars:
                rendered = rendered[: self.max_statement_chars]
                truncated = True
            statements.append(
                SqlStatement(
                    statement=rendered,
                    evidence_id=evidence_id,
                    dialect=clean_dialect,
                    parse_status=SqlParseStatus.PARTIAL if heuristic else SqlParseStatus.PARSED,
                    truncated=truncated,
                )
            )
            for table in expression.find_all(exp.Table):
                name = table.name or ""
                if not name:
                    continue
                tables.append(
                    ParsedTable(
                        catalog=table.catalog or None,
                        # `db` is the schema/database qualifier; case and
                        # qualification preserved exactly as parsed.
                        schema=table.db or None,
                        table=name,
                    )
                )
        return SqlExtraction(
            statements=tuple(statements),
            tables=tuple(tables),
            observed=1,
            parsed=parsed,
            parse_failures=failures,
        )

    @staticmethod
    def _parse_chunks(sql: str, dialect: str | None) -> list[tuple[str, exp.Expr | None, bool]]:
        """Parse whole-field first; fall back to heuristic chunk splitting."""
        try:
            expressions = sqlglot.parse(sql, read=dialect)
        except Exception:
            expressions = None
        if expressions is not None:
            return [("", expression, False) for expression in expressions if expression is not None]
        chunks: list[tuple[str, exp.Expr | None, bool]] = []
        for raw_chunk in sql.split(";"):
            chunk = raw_chunk.strip()
            if not chunk:
                continue
            try:
                expression = sqlglot.parse_one(chunk, read=dialect)
            except Exception:
                expression = None
            chunks.append((chunk, expression, True))
        return chunks


def _path_shape_broken(attrs: dict[str, Any], path: str) -> bool:
    """True if a present intermediate container on a dotted path is not a dict.

    Missing segments are absence (try the next candidate); present-but-wrong
    shapes are corruption. Leaf types are the caller's decision.
    """
    node: Any = attrs
    for segment in path.split("."):
        if not isinstance(node, dict):
            return True
        if segment not in node:
            return False
        node = node[segment]
    return False


def _extract_code_fields_mapped(
    attrs: dict[str, Any], code_mapping: CodeLocationMapping
) -> tuple[tuple[str | None, str | None, int | None] | None, bool]:
    """Mapping-driven code-location extraction with the same malformed parity
    as the ECS path: absent metadata is normal, corrupt containers are
    flagged, over-long strings degrade with a flag, invalid lines degrade
    silently to None."""
    malformed = False
    field_mappings = [code_mapping.function]
    if code_mapping.file_path is not None:
        field_mappings.append(code_mapping.file_path)
    if code_mapping.line is not None:
        field_mappings.append(code_mapping.line)
    for field_mapping in field_mappings:
        for path in field_mapping.candidates:
            if _path_shape_broken(attrs, path):
                malformed = True

    function: str | None = None
    function_raw = resolve_value(attrs, code_mapping.function)
    if isinstance(function_raw, str) and function_raw:
        if len(function_raw) <= MAX_FUNCTION_CHARS:
            function = function_raw
        else:
            malformed = True

    file_path: str | None = None
    line: int | None = None
    if code_mapping.file_path is not None or code_mapping.line is not None:
        if code_mapping.file_path is not None:
            path_raw = resolve_value(attrs, code_mapping.file_path)
            if isinstance(path_raw, str) and path_raw:
                if len(path_raw) <= MAX_FILE_PATH_CHARS:
                    file_path = path_raw
                else:
                    malformed = True
        if code_mapping.line is not None:
            line_raw = resolve_value(attrs, code_mapping.line)
            if isinstance(line_raw, int) and not isinstance(line_raw, bool) and line_raw >= 1:
                line = line_raw

    if function is None and file_path is None and line is None:
        return None, malformed
    return (function, file_path, line), malformed


def _extract_sql_field_mapped(
    attrs: dict[str, Any], sql_mapping: FieldMapping
) -> tuple[str | None, str | None, bool]:
    """Mapping-driven SQL text extraction; dialect keys stay generic."""
    statement_raw = resolve_value(attrs, sql_mapping)
    if statement_raw is None:
        # resolve_value skips absent AND empty values. Empty strings and
        # containers count as absent (matching legacy behavior); only
        # present, non-empty, wrong-typed values are malformed.
        present = any(_traverse_nonempty(attrs, path) for path in sql_mapping.candidates)
        return None, None, present
    if not isinstance(statement_raw, str):
        return None, None, True
    dialect: str | None = None
    for key in ("dialect", "system"):
        candidate = attrs.get("db") if isinstance(attrs.get("db"), dict) else None
        raw = candidate.get(key) if isinstance(candidate, dict) else attrs.get(key)
        if isinstance(raw, str) and raw.strip():
            dialect = raw.strip()[:MAX_DIALECT_CHARS]
            break
    return statement_raw, dialect, False


def _traverse_nonempty(attrs: dict[str, Any], path: str) -> bool:
    """True if a dotted path resolves to a present, non-empty value.

    Present-but-corrupt intermediates (a list where a dict belongs) count
    as present — the value exists, it is just malformed. Only genuinely
    missing keys and null/empty leaves count as absent, matching the legacy
    ECS extractor's absent-vs-malformed contract.
    """
    segments = path.split(".")
    node: Any = attrs
    for segment in segments[:-1]:
        if not isinstance(node, dict):
            return True
        if segment not in node:
            return False
        node = node[segment]
        if node is None:
            return False
    if not isinstance(node, dict):
        return True
    if segments[-1] not in node:
        return False
    value = node[segments[-1]]
    return value is not None and value != ""


def _extract_code_fields(
    attrs: dict[str, Any],
) -> tuple[tuple[str | None, str | None, int | None] | None, bool]:
    """Legacy ECS-only extraction, superseded by ``_extract_code_fields_mapped``.

    Kept for backward compatibility of direct unit-test imports; all
    production paths route through the mapping-driven variant (equivalent
    under the generic-ECS mapping, except that corrupt shapes anywhere in
    the code/log containers flag malformed even when a valid location was
    still extracted — the mapped variant checks all candidate paths).
    """
    from investigation_agent_platform.domain.observability.mapping import (
        CodeLocationMapping,
        FieldMapping,
        FieldValueType,
    )

    generic_code = CodeLocationMapping(
        function=FieldMapping(
            candidates=["code.function", "log.origin.function"],
            value_type=FieldValueType.KEYWORD,
        ),
        file_path=FieldMapping(
            candidates=["code.filepath", "code.file.name"],
            value_type=FieldValueType.KEYWORD,
        ),
        line=FieldMapping(
            candidates=["code.lineno", "code.file.line"],
            value_type=FieldValueType.LONG,
        ),
    )
    return _extract_code_fields_mapped(attrs, generic_code)


@dataclass
class TraceTelemetryExtractor(TraceTelemetryPort, TraceTelemetryDerivationPort):
    """Bounded trace investigation over runtime evidence (Parts 9-10).

    Depends only on the runtime provider port: trace derivation works over
    any provider whose records carry the mapped shapes. The provider is
    optional so derivation-only consumers (application service, which
    retrieves through the gateway) never fabricate one; ``extract`` fails
    fast without it. Field names resolve through the request's mapping
    (``None`` = built-in generic-ECS); sources without code/SQL mappings
    report unsupported explicitly instead of empty success.
    """

    runtime_provider: RuntimeEvidenceProviderProtocol | None = None
    sql_extractor: SqlEvidenceExtractor = field(default_factory=SqlEvidenceExtractor)
    mapping_registry: MappingProfileRegistry | None = None

    def _resolve_derivation_mapping(self, source_id: str | None) -> ObservabilitySourceMapping:
        """Resolve a stamped mapping id; fail closed before any derivation."""
        if source_id is None or source_id == GENERIC_ECS_SOURCE_ID:
            return generic_ecs_mapping()
        if self.mapping_registry is None:
            raise PlatformConfigurationError(
                "Trace derivation names an observability mapping but this "
                "extractor was built without a mapping registry."
            )
        return self.mapping_registry.resolve(source_id)

    async def extract(
        self,
        *,
        tenant_id: str,
        investigation_id: UUID,
        request: TraceTelemetryRequest,
        profile: ObservabilityProfile,
        correlation_id: str | None = None,
    ) -> TraceTelemetry:
        """Retrieve, derive, and bound one trace view with completeness.

        Provider failures and cancellation propagate untouched; one malformed
        event never aborts the investigation (counted, not hidden).
        """
        if self.runtime_provider is None:
            raise PlatformConfigurationError(
                "TraceTelemetryExtractor.extract requires a runtime provider."
            )
        evidence_request = RuntimeEvidenceRequest(
            environment=request.environment,
            identifiers={"trace_id": request.trace_id},
            time_range=request.time_range,
            limit=request.max_evidence,
            mapping_source_id=request.mapping_source_id,
        )
        result = await self.runtime_provider.search_runtime_evidence(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            request=evidence_request,
            profile=profile,
            correlation_id=correlation_id,
        )
        execution_metadata = result.execution_metadata or {}
        record_errors = execution_metadata.get("errors", [])
        skipped = len(record_errors) if isinstance(record_errors, list) else 0
        return self.extract_from_items(
            trace_id=request.trace_id,
            items=list(result.items),
            skipped=skipped,
            has_more=result.has_more,
            max_evidence=request.max_evidence,
            mapping_source_id=request.mapping_source_id,
        )

    def extract_from_items(
        self,
        *,
        trace_id: str,
        items: list[Evidence],
        skipped: int,
        has_more: bool,
        max_evidence: int,
        mapping_source_id: str | None = None,
    ) -> TraceTelemetry:
        """Derive the bounded trace view from already-retrieved evidence.

        Pure derivation (no I/O): the application service feeds it gateway-
        sanitized items so agent-facing telemetry inherits the sanitization,
        authorization, and dedup of the single evidence path. Code/SQL
        derivation is gated on the resolved mapping declaring those concepts;
        unsupported sources report explicit flags, never silent emptiness.
        """
        mapping = self._resolve_derivation_mapping(mapping_source_id)
        code_supported = mapping.code_location is not None
        sql_supported = mapping.sql_statement is not None
        locations: dict[tuple[str | None, str | None, int | None], set[UUID]] = {}
        tables: dict[tuple[str | None, str | None, str], set[UUID]] = {}
        sql_statements: list[SqlStatement] = []
        malformed_sections = 0
        sql_observed = 0
        sql_parsed = 0
        sql_failures = 0

        for item in items:
            attrs = item.attributes or {}
            if not isinstance(attrs, dict):
                malformed_sections += 1
                continue
            item_malformed = False

            if code_supported and mapping.code_location is not None:
                code_location, code_malformed = _extract_code_fields_mapped(
                    attrs, mapping.code_location
                )
            else:
                code_location, code_malformed = None, False
            item_malformed = item_malformed or code_malformed
            if code_location is not None:
                locations.setdefault(code_location, set()).add(item.evidence_id)

            if sql_supported and mapping.sql_statement is not None:
                sql_text, dialect, sql_malformed = _extract_sql_field_mapped(
                    attrs, mapping.sql_statement
                )
            else:
                sql_text, dialect, sql_malformed = None, None, False
            item_malformed = item_malformed or sql_malformed
            if sql_text is not None:
                sql_observed += 1
                extraction = self.sql_extractor.extract(
                    evidence_id=item.evidence_id, sql=sql_text, dialect=dialect
                )
                sql_statements.extend(extraction.statements)
                sql_parsed += extraction.parsed
                sql_failures += extraction.parse_failures
                for table in extraction.tables:
                    tables.setdefault((table.catalog, table.schema, table.table), set()).add(
                        item.evidence_id
                    )

            if item_malformed:
                malformed_sections += 1

        code_locations = [
            CodeLocation(
                function=key[0],
                file_path=key[1],
                line=key[2],
                evidence_ids=sorted(evidence_ids)[:MAX_EVIDENCE_REFS],
            )
            for key, evidence_ids in sorted(
                locations.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "", kv[0][2] or 0)
            )[:MAX_RESULT_ITEMS]
        ]
        table_refs = [
            TableReference(
                catalog=key[0],
                schema=key[1],
                table=key[2],
                evidence_ids=sorted(evidence_ids)[:MAX_EVIDENCE_REFS],
            )
            for key, evidence_ids in sorted(
                tables.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "", kv[0][2])
            )[:MAX_RESULT_ITEMS]
        ]

        examined = len(items) + skipped
        if has_more:
            # The trace view hit the request budget. Below-request yields mean
            # a tighter provider ceiling applied; at-request yields mean our
            # own budget applied.
            reason = (
                CompletenessReason.PROVIDER_LIMIT
                if len(items) < max_evidence
                else CompletenessReason.APPLICATION_LIMIT
            )
            completeness = EvidenceCompleteness(
                complete=False,
                truncated=True,
                reason=reason,
                returned_count=len(items),
                examined_count=examined,
            )
        else:
            partial = skipped > 0
            completeness = EvidenceCompleteness(
                complete=not partial,
                truncated=False,
                reason=CompletenessReason.METADATA_INCOMPLETE if partial else None,
                returned_count=len(items),
                examined_count=examined,
            )

        logger.debug(
            "Trace telemetry extracted",
            extra={
                "context": {
                    "trace_id": trace_id,
                    "examined": examined,
                    "locations": len(code_locations),
                    "tables": len(table_refs),
                    "sql_observed": sql_observed,
                    "sql_parsed": sql_parsed,
                    "sql_failures": sql_failures,
                    "complete": completeness.complete,
                }
            },
        )
        return TraceTelemetry(
            trace_id=trace_id,
            code_locations=code_locations,
            tables=table_refs,
            sql_statements=sql_statements[:MAX_RESULT_ITEMS],
            evidence_count=examined,
            skipped_record_count=skipped,
            malformed_section_count=malformed_sections,
            sql_observed=sql_observed,
            sql_parsed=sql_parsed,
            sql_parse_failures=sql_failures,
            code_location_supported=code_supported,
            sql_supported=sql_supported,
            completeness=completeness,
        )
