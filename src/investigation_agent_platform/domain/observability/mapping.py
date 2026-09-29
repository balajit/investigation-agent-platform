# src/investigation_agent_platform/domain/observability/mapping.py
"""Per-source Elastic field contracts (Part 10).

An `ObservabilitySourceMapping` is the complete, externally-authored field
contract for one index family. Every physical field name the runtime evidence
layer queries, sorts, filters, or projects is resolved through it — no
Python literal in `query.py`, `projection.py`, or `trace_telemetry.py` names
a log field. Operator-authored YAML, validated at startup, fail-closed on
malformed entries. Never agent input, never inferred from `_source` at
request time.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from investigation_agent_platform.domain.evidence.models import LogSeverity

# Built-in mapping id: resolves to generic_ecs_mapping() without any
# registry, in every resolver. Requests and profiles default here, so the
# entire pre-Part-10 path works with no configuration at all.
GENERIC_ECS_SOURCE_ID = "generic-ecs"


class FieldValueType(StrEnum):
    """Physical value shapes a resolver must handle per candidate path."""

    DATE_ISO = "date_iso"
    DATE_EPOCH_MILLIS = "date_epoch_millis"
    DATE_EPOCH_SECONDS = "date_epoch_seconds"
    KEYWORD = "keyword"
    TEXT = "text"
    BOOLEAN = "boolean"
    LONG = "long"
    FLOAT = "float"


class FieldMapping(BaseModel):
    """Ordered candidate physical paths for one logical concept.

    Value resolution takes the first present-and-non-empty candidate and
    never merges across candidates. Query construction over TEXT mappings
    (keyword search) may target *all* candidates as a field list — the
    display value still comes from first-present-wins.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidates: list[str] = Field(..., min_length=1, max_length=10)
    value_type: FieldValueType


class SeverityStrategy(StrEnum):
    """How a source supplies severity signal."""

    FIELD = "field"  # a real enum/keyword severity field exists
    ERROR_FLAG = "error_flag"  # derive from boolean + optional code field
    UNSUPPORTED = "unsupported"  # no signal; severity filters rejected, not ignored


class SeverityDerivation(BaseModel):
    """Declarative severity semantics for one source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: SeverityStrategy
    field: FieldMapping | None = None  # required if strategy=FIELD
    error_field: str | None = None  # required if strategy=ERROR_FLAG
    error_code_field: str | None = None  # optional detail for ERROR_FLAG
    true_severity: LogSeverity = LogSeverity.ERROR
    false_severity: LogSeverity = LogSeverity.INFO

    @model_validator(mode="after")
    def validate_strategy_fields(self) -> "SeverityDerivation":
        if self.strategy == SeverityStrategy.FIELD and self.field is None:
            raise ValueError("severity strategy 'field' requires a field mapping.")
        if self.strategy == SeverityStrategy.ERROR_FLAG and not self.error_field:
            raise ValueError("severity strategy 'error_flag' requires an error_field.")
        return self


class TenantScopeStrategy(StrEnum):
    """How tenant isolation is achieved for one source."""

    INDEX_PATTERN = "index_pattern"  # index selection alone provides isolation
    FIELD = "field"  # mandatory filter term required
    # Single-tenant source, explicit and audited — never a silent default.
    # Logged distinctly at startup (investigation-agent-platform#Part10).
    NONE_REQUIRED = "none_required"


class TenantScope(BaseModel):
    """Tenant-isolation contract for one source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: TenantScopeStrategy
    field: str | None = None  # physical field carrying tenant identity, if FIELD

    @model_validator(mode="after")
    def validate_scope_field(self) -> "TenantScope":
        if self.strategy == TenantScopeStrategy.FIELD and not self.field:
            raise ValueError("tenant_scope strategy 'field' requires a field.")
        return self


class SortOrder(StrEnum):
    """Pagination sort direction for one source."""

    ASC = "asc"
    DESC = "desc"


class SortSpec(BaseModel):
    """Deterministic pagination sort for one source (Part 10.9).

    The sort clause is always exactly two keys — the mapping sort field plus
    the `_id` tiebreak — so cursor arity is constant across mappings. The
    fingerprint binds the resolved spec, not just a version constant.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str = Field(..., min_length=1, max_length=256)
    order: SortOrder = SortOrder.DESC
    # ES date-format hint (e.g. "epoch_millis"); None for non-date fields.
    format: str | None = Field(default=None, max_length=64)
    # Unmapped-field behavior for indices missing the sort field.
    unmapped_type: str | None = Field(default=None, max_length=32)


class CodeLocationMapping(BaseModel):
    """Physical paths for code-location derivation (null = not supported)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    function: FieldMapping
    file_path: FieldMapping | None = None
    line: FieldMapping | None = None


class TraceHopMapping(BaseModel):
    """Physical paths for cross-service hop resolution (Part 10.11).

    Optional: sources without tracing-shaped data omit it and hop
    resolution degrades to None (documented, never fabricated). When
    present, the resolver uses these paths instead of the ECS defaults.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    service: FieldMapping
    span_id: FieldMapping
    parent_id: FieldMapping | None = None


class ObservabilitySourceMapping(BaseModel):
    """One index-family's complete field contract. Config-only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(..., max_length=128)
    index_patterns: list[str] = Field(default_factory=list, max_length=10)
    # True only for the built-in generic-ECS mapping, whose index patterns
    # are synthesized per-request by the legacy builder. Operator-authored
    # mappings must always declare explicit patterns (enforced at load).
    legacy_index_synthesis: bool = False
    tenant_scope: TenantScope
    timestamp: FieldMapping
    message: FieldMapping | None = None
    service: FieldMapping | None = None
    severity: SeverityDerivation
    identifiers: dict[str, FieldMapping] = Field(default_factory=dict, max_length=20)
    classification: FieldMapping | None = None
    code_location: CodeLocationMapping | None = None
    sql_statement: FieldMapping | None = None
    trace_hop: TraceHopMapping | None = None
    sort: SortSpec = Field(default_factory=lambda: SortSpec(field="@timestamp"))
    top_level_allowlist: frozenset[str] = Field(..., min_length=1, max_length=100)


def generic_ecs_mapping() -> ObservabilitySourceMapping:
    """Built-in mapping reproducing the pre-Part-10 hard-coded behavior exactly.

    Regression-safe fallback: every Phase 1–6 test exercises this path, and
    any request without an explicit `mapping_source_id` resolves here.
    """
    keyword = FieldValueType.KEYWORD
    return ObservabilitySourceMapping(
        source_id=GENERIC_ECS_SOURCE_ID,
        index_patterns=[],
        legacy_index_synthesis=True,
        tenant_scope=TenantScope(strategy=TenantScopeStrategy.INDEX_PATTERN),
        timestamp=FieldMapping(candidates=["@timestamp"], value_type=FieldValueType.DATE_ISO),
        message=FieldMapping(
            candidates=["message", "error.message"], value_type=FieldValueType.TEXT
        ),
        service=FieldMapping(candidates=["service.name"], value_type=keyword),
        severity=SeverityDerivation(
            strategy=SeverityStrategy.FIELD,
            field=FieldMapping(candidates=["log.level"], value_type=keyword),
        ),
        identifiers={
            key: FieldMapping(candidates=[f"labels.{key}"], value_type=keyword)
            for key in (
                "trace_id",
                "span_id",
                "service_name",
                "user_id",
                "session_id",
                "container_id",
            )
        },
        classification=FieldMapping(
            candidates=[
                "labels.data_classification",
                "labels.classification",
                "event.classification",
            ],
            value_type=keyword,
        ),
        code_location=CodeLocationMapping(
            # Ordered fallback chains reproduce the historical container
            # fallback (`code.*`, then `log.origin.*` as a whole container).
            # Per-field (not per-container) fallback finds strictly more than
            # the legacy lookup in mixed-container edge cases; deterministic
            # either way.
            function=FieldMapping(
                candidates=["code.function", "log.origin.function"], value_type=keyword
            ),
            file_path=FieldMapping(
                candidates=[
                    "code.filepath",
                    "code.file.name",
                    "log.origin.filepath",
                    "log.origin.file.name",
                ],
                value_type=keyword,
            ),
            line=FieldMapping(
                candidates=[
                    "code.lineno",
                    "code.file.line",
                    "log.origin.lineno",
                    "log.origin.file.line",
                ],
                value_type=FieldValueType.LONG,
            ),
        ),
        sql_statement=FieldMapping(candidates=["db.statement"], value_type=FieldValueType.TEXT),
        sort=SortSpec(
            field="@timestamp", order=SortOrder.DESC, format="epoch_millis", unmapped_type="long"
        ),
        top_level_allowlist=frozenset(
            {
                "@timestamp",
                "message",
                "error",
                "service",
                "log",
                "labels",
                "code",
                "db",
                "trace",
                "span",
                "transaction",
                "parent",
                "event",
                "host",
                "container",
                "process",
                "tags",
                "user",
            }
        ),
    )
