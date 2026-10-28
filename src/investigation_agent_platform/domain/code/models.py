# src/investigation_agent_platform/domain/code/models.py
"""Domain models for code symbol AST analysis and structural representations."""

from __future__ import annotations

import hashlib
import warnings
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from investigation_agent_platform.domain.provenance.models import SourceLocation
from investigation_agent_platform.domain.topology.models import (
    CallResolutionStatus,
    TopologyNodeType,
    compute_ast_node_id,
    normalize_file_path,
)


class SymbolType(StrEnum):
    """Categories of code symbols for AST indexing and query dispatch."""

    CLASS = "CLASS"
    INTERFACE = "INTERFACE"
    METHOD = "METHOD"
    FUNCTION = "FUNCTION"
    FIELD = "FIELD"
    CONSTRUCTOR = "CONSTRUCTOR"
    ENUM = "ENUM"


SYMBOL_TO_TOPOLOGY_TYPE: dict[SymbolType, TopologyNodeType] = {
    SymbolType.CLASS: TopologyNodeType.CLASS,
    SymbolType.INTERFACE: TopologyNodeType.INTERFACE,
    SymbolType.METHOD: TopologyNodeType.METHOD,
    SymbolType.FUNCTION: TopologyNodeType.FUNCTION,
    SymbolType.FIELD: TopologyNodeType.FIELD,
    SymbolType.CONSTRUCTOR: TopologyNodeType.CONSTRUCTOR,
    SymbolType.ENUM: TopologyNodeType.ENUM,
}


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
    GUARDED_RETURN_LITERAL = "GUARDED_RETURN_LITERAL"
    # Python fact semantics (shared enum; no versioned envelope per pre-prod decision).
    PARAMETER_DEFAULT_EXPRESSION = "PARAMETER_DEFAULT_EXPRESSION"
    FRAMEWORK_DEFAULT = "FRAMEWORK_DEFAULT"


class JavaFactCategory(StrEnum):
    """Practical Java fact taxonomy (IAP-Code-Java D1).

    Independent from ClaimKind: ClaimKind selects the evidence-binding rule,
    fact_category names the Java semantic fact. Never infer one from the other.
    """

    DECLARATION_FACT = "DECLARATION_FACT"
    SOURCE_VALUE_FACT = "SOURCE_VALUE_FACT"
    LANGUAGE_SEMANTIC_FACT = "LANGUAGE_SEMANTIC_FACT"
    CONTROL_FLOW_FACT = "CONTROL_FLOW_FACT"
    DATA_FLOW_FACT = "DATA_FLOW_FACT"
    CALL_TARGET_FACT = "CALL_TARGET_FACT"
    POSSIBLE_EFFECT_FACT = "POSSIBLE_EFFECT_FACT"
    CONFIGURATION_FACT = "CONFIGURATION_FACT"
    RUNTIME_OBSERVATION_FACT = "RUNTIME_OBSERVATION_FACT"
    CAUSAL_CLAIM = "CAUSAL_CLAIM"


#: Language-neutral alias. The enum keeps its Java-prefixed class name for
#: backward compatibility; Python resolvers reuse the same members.
CodeFactCategory = JavaFactCategory


class SemanticStage(StrEnum):
    """Java initialization/evaluation stage for a resolved fact (D1)."""

    DECLARATION = "DECLARATION"
    CLASS_PREPARATION = "CLASS_PREPARATION"
    CLASS_INITIALIZATION = "CLASS_INITIALIZATION"
    INSTANCE_DEFAULTING = "INSTANCE_DEFAULTING"
    INSTANCE_INITIALIZATION = "INSTANCE_INITIALIZATION"
    CONSTRUCTOR_BODY = "CONSTRUCTOR_BODY"
    POST_CONSTRUCTION = "POST_CONSTRUCTION"
    LATER_MUTATION = "LATER_MUTATION"
    RUNTIME_EFFECTIVE = "RUNTIME_EFFECTIVE"


CODE_FACT_RESOLUTION_KEY = "code_fact_resolution"
CODE_FACT_RESOLUTION_V2_KEY = "code_fact_resolution_v2"
CODE_FACT_RESOLUTIONS_KEY = "code_fact_resolutions"
JAVA_FACT_RESOLUTION_PREFIX = "code_fact_java."

JAVA_FACT_SELECTORS = frozenset(
    {
        "call_target_name",
        "local_name",
        "init_name",
        "dataflow_symbol",
        "control_symbol",
        "effect_symbol",
        "type_symbol",
        "boundary_symbol",
    }
)


def java_fact_selector(value: Any) -> str:
    """Normalize one explicit Java GET_CODE fact selector."""
    text = str(value or "").strip()
    if not text or len(text) > 256 or "\x00" in text:
        return ""
    return text


class CodeFactResolution(BaseModel):
    """Ephemeral result of deterministic source-level field analysis.

    This model is carried with existing evidence; it is not a persistence
    aggregate. A resolved value describes the named semantics only, never an
    arbitrary later/runtime-effective field value.

    Legacy strict reader: extra="forbid" is intentional. Generalized payloads
    carrying fact_category/subject_identity/spans must never validate here;
    they live under CODE_FACT_RESOLUTION_V2_KEY (CodeFactResolutionV2).
    Model step 5 identity alignment adds only optional node_id/parser_version.
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
    node_id: UUID | None = Field(default=None)
    parser_version: str = Field(default="unknown", max_length=64)

    @model_validator(mode="after")
    def validate_resolution(self) -> CodeFactResolution:
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


class CodeFactResolutionV2(BaseModel):
    """Versioned generalized resolution (Phase 0 dual-emit decision).

    Emitted under CODE_FACT_RESOLUTION_V2_KEY alongside the legacy
    CODE_FACT_RESOLUTION_KEY shape. Strict legacy readers reject this payload
    (extra fact_category/subject_identity fields under extra="forbid"); new
    readers accept both keys and reject conflicting duplicates via
    detect_dual_resolution_conflict. CAUSAL_CLAIM can never be analyzer-resolved.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: CodeFactStatus
    repository: str | None = Field(default=None, max_length=256)
    revision: str | None = Field(default=None, max_length=128)
    fact_category: JavaFactCategory
    semantic_stage: SemanticStage = SemanticStage.DECLARATION
    declaring_type: str | None = Field(default=None, max_length=512)
    subject_identity: str | None = Field(default=None, max_length=1024)
    field_name: str | None = Field(default=None, min_length=1, max_length=256)
    field_type: str | None = Field(default=None, min_length=1, max_length=256)
    field_category: Literal["INSTANCE_FIELD", "STATIC_FIELD"] | None = Field(default=None)
    value_semantics: CodeValueSemantics
    analysis_method: str = Field(default="tree-sitter-range-v1", max_length=128)
    language_version: str | None = Field(default=None, max_length=32)
    raw_initializer: str | None = Field(default=None, max_length=2048)
    resolved_value: str | None = Field(default=None, max_length=2048)
    structured_fact: dict[str, Any] | None = None
    node_id: UUID | None = Field(default=None)
    target_identity: str | None = Field(default=None, max_length=1024)
    call_target: str | None = Field(default=None, max_length=1024)
    control_flow_refs: tuple[str, ...] = ()
    data_flow_refs: tuple[str, ...] = ()
    source_evidence_ids: tuple[UUID, ...] = ()
    conflicting_evidence_ids: tuple[UUID, ...] = ()
    declaration_location: SourceLocation | None = None
    runtime_source_identity: str | None = Field(default=None, max_length=1024)
    runtime_observed_at: datetime | None = None
    limitation_codes: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    parser_version: str = Field(default="unknown", max_length=64)

    @model_validator(mode="after")
    def validate_v2_resolution(self) -> CodeFactResolutionV2:
        if (
            self.fact_category == JavaFactCategory.CAUSAL_CLAIM
            and self.status == CodeFactStatus.RESOLVED
        ):
            raise ValueError("CAUSAL_CLAIM is a verifier conclusion, never an analyzer resolution")
        if self.status == CodeFactStatus.RESOLVED:
            if self.fact_category == JavaFactCategory.RUNTIME_OBSERVATION_FACT:
                if not self.runtime_source_identity or self.runtime_observed_at is None:
                    raise ValueError(
                        "resolved runtime observations require runtime source identity and time"
                    )
            else:
                if (
                    not self.revision
                    or self.revision in {"HEAD", "UNPINNED"}
                    or self.declaration_location is None
                    or not self.declaration_location.file_path
                    or not self.declaration_location.revision
                ):
                    raise ValueError(
                        "resolved source facts require immutable revision/path provenance"
                    )
                if (
                    self.resolved_value is None
                    and self.structured_fact is None
                    and self.call_target is None
                ):
                    raise ValueError("resolved source facts require a value payload")
        else:
            if not self.limitation_codes:
                raise ValueError("PARTIAL/UNRESOLVED resolutions require limitation_codes")
        return self


def legacy_to_v2_resolution(legacy: CodeFactResolution) -> CodeFactResolutionV2:
    """Derive the dual-emit v2 view of a legacy field resolution."""
    subject = (
        f"{legacy.declaring_type}.{legacy.field_name}"
        if legacy.declaring_type and legacy.field_name
        else legacy.field_name
    )
    return CodeFactResolutionV2(
        status=legacy.status,
        repository=legacy.repository,
        revision=legacy.revision,
        fact_category=JavaFactCategory.SOURCE_VALUE_FACT,
        semantic_stage=SemanticStage.DECLARATION,
        declaring_type=legacy.declaring_type,
        subject_identity=subject,
        field_name=legacy.field_name,
        field_type=legacy.field_type,
        field_category=legacy.field_category,
        value_semantics=legacy.value_semantics,
        raw_initializer=legacy.raw_initializer,
        resolved_value=legacy.resolved_value,
        node_id=legacy.node_id,
        source_evidence_ids=legacy.source_evidence_ids,
        declaration_location=legacy.declaration_location,
        limitation_codes=legacy.limitation_codes
        if legacy.status != CodeFactStatus.RESOLVED or legacy.limitation_codes
        else (),
        parser_version=legacy.parser_version,
    )


def emit_dual_resolution_attributes(legacy: CodeFactResolution) -> dict[str, Any]:
    """Dual-emit payload: legacy key plus versioned v2 key (Phase 0 decision)."""
    v2 = legacy_to_v2_resolution(legacy)
    return {
        CODE_FACT_RESOLUTION_KEY: legacy.model_dump(mode="json"),
        CODE_FACT_RESOLUTION_V2_KEY: v2.model_dump(mode="json"),
    }


def detect_dual_resolution_conflict(legacy_raw: Any, v2_raw: Any) -> str | None:
    """Return a conflict reason when dual keys disagree, else None.

    New readers reject conflicting duplicates; old readers ignore the v2 key.
    Overlap checked: status, resolved_value, revision, repository, field identity.
    """
    if legacy_raw is None or v2_raw is None:
        return None
    if not isinstance(legacy_raw, dict) or not isinstance(v2_raw, dict):
        return None
    try:
        legacy = CodeFactResolution.model_validate(legacy_raw)
    except Exception:
        return "legacy payload invalid"
    try:
        v2 = CodeFactResolutionV2.model_validate(v2_raw)
    except Exception:
        return "v2 payload invalid"
    if legacy.status != v2.status:
        return f"status conflict: {legacy.status.value} vs {v2.status.value}"
    if (legacy.resolved_value or "") != (v2.resolved_value or ""):
        return f"resolved value conflict: {legacy.resolved_value!r} vs {v2.resolved_value!r}"
    if (legacy.revision or "") != (v2.revision or ""):
        return f"revision conflict: {legacy.revision!r} vs {v2.revision!r}"
    if legacy.repository != (v2.repository or legacy.repository):
        return "repository conflict"
    legacy_subject = f"{legacy.declaring_type}.{legacy.field_name}"
    v2_subject = v2.subject_identity or (
        f"{v2.declaring_type}.{v2.field_name}" if v2.declaring_type and v2.field_name else None
    )
    if v2_subject is not None and legacy_subject != v2_subject:
        return f"subject conflict: {legacy_subject!r} vs {v2_subject!r}"
    return None


def strip_v2_for_downgrade(attributes: dict[str, Any]) -> dict[str, Any]:
    """Downgrade: drop the versioned key; legacy readers see the original row."""
    if not isinstance(attributes, dict):
        return {}
    return {k: v for k, v in attributes.items() if k != CODE_FACT_RESOLUTION_V2_KEY}


class CodeSymbol(BaseModel):
    """Structural definition of a source code symbol extracted via AST parsing."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    node_id: UUID = Field(default_factory=uuid4, alias="symbol_id")
    tenant_id: str = Field(default="", max_length=128)
    name: str = Field(..., max_length=256)
    qualified_name: str = Field(default="", max_length=1024)
    symbol_type: SymbolType
    repository: str = Field(..., max_length=256)
    file_path: str = Field(..., max_length=1024)
    line_start: int = Field(..., ge=1)
    line_end: int = Field(..., ge=1)
    start_column: int = Field(default=0, ge=0)
    end_column: int = Field(default=0, ge=0)
    signature: str = Field(..., max_length=2048)
    signature_hash: str = Field(default="", max_length=128)
    revision: str = Field(default="HEAD", max_length=128)
    documentation: str | None = Field(default=None, max_length=4096)
    parent_node_id: UUID | None = Field(default=None, alias="parent_symbol")
    parser_version: str = Field(default="unknown", max_length=64)

    @property
    def symbol_id(self) -> UUID:
        return self.node_id

    @property
    def parent_symbol(self) -> UUID | None:
        return self.parent_node_id

    @model_validator(mode="before")
    @classmethod
    def _warn_legacy_alias_spelling(cls, data: Any) -> Any:
        """Deprecation notice for pre-D1 alias spellings (Model step 6).

        Warns only; validation still accepts both spellings. Alias removal
        waits for a release with zero warning sightings.
        """
        if isinstance(data, dict) and ("symbol_id" in data or "parent_symbol" in data):
            warnings.warn(
                "CodeSymbol alias spellings 'symbol_id'/'parent_symbol' are deprecated; "
                "use 'node_id'/'parent_node_id'.",
                DeprecationWarning,
                stacklevel=4,
            )
        return data

    @field_validator("node_id", mode="before")
    @classmethod
    def _coerce_node_id(cls, value: Any) -> Any:
        return value

    @field_validator("parent_node_id", mode="before")
    @classmethod
    def _coerce_parent_node_id(cls, value: Any) -> Any:
        """Accept UUIDs; drop legacy name-only parents instead of failing.

        The pre-D1 ``parent_symbol`` was a bare name string, not an
        identity. A non-UUID string cannot become a ``UUID``, so it coerces
        to ``None`` (unparented) to keep legacy payloads validating. New
        code must pass a real ``UUID`` via ``CodeSymbol.create``.
        """
        if value is None or isinstance(value, UUID):
            return value
        if isinstance(value, str):
            try:
                return UUID(value)
            except (ValueError, AttributeError, TypeError):
                return None
        return value

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

    @model_validator(mode="after")
    def _validate_columns(self) -> CodeSymbol:
        if self.line_start == self.line_end and self.end_column < self.start_column:
            raise ValueError("end_column cannot be less than start_column on a single line.")
        return self

    @classmethod
    def create(
        cls,
        tenant_id: str,
        repository_id: str,
        name: str,
        qualified_name: str,
        symbol_type: SymbolType,
        repository: str,
        file_path: str,
        line_start: int,
        line_end: int,
        signature: str,
        revision: str,
        start_column: int = 0,
        end_column: int = 0,
        documentation: str | None = None,
        parent_node_id: UUID | None = None,
        parser_version: str = "unknown",
    ) -> CodeSymbol:
        """Factory deriving deterministic identity; the only production path."""
        normalized_path = normalize_file_path(file_path)
        node_id = compute_ast_node_id(
            tenant_id=tenant_id,
            repository_id=repository_id,
            revision=revision,
            file_path=normalized_path,
            qualified_name=qualified_name or name,
            node_type=SYMBOL_TO_TOPOLOGY_TYPE[symbol_type],
            start_line=line_start,
            start_column=start_column,
            end_line=line_end,
            end_column=end_column,
        )
        signature_hash = hashlib.sha256(signature.encode("utf-8")).hexdigest() if signature else ""
        return cls(
            node_id=node_id,
            tenant_id=tenant_id,
            name=name,
            qualified_name=qualified_name or name,
            symbol_type=symbol_type,
            repository=repository,
            file_path=normalized_path,
            line_start=line_start,
            line_end=line_end,
            start_column=start_column,
            end_column=end_column,
            signature=signature,
            signature_hash=signature_hash,
            revision=revision,
            documentation=documentation,
            parent_node_id=parent_node_id,
            parser_version=parser_version,
        )


class CallGraphNode(BaseModel):
    """Legacy node embedding a full ``CodeSymbol`` per node (D3 predecessor).

    Retained for one release so lagging callers' payloads still validate via
    :meth:`CallGraph.from_legacy_nodes` and the ``CallGraph`` before-validator.
    New code must emit :class:`CallGraphEdge` entries instead. Do not extend.
    """

    model_config = ConfigDict(frozen=True)

    symbol: CodeSymbol
    depth: int = Field(..., ge=0)
    invocations: list[str] = Field(default_factory=list, max_length=100)


#: Confidence assigned to edges fabricated by the one-release legacy
#: translator (D3 risk mitigation: invocation strings are not real call
#: sites, so they must never look like parser-attested edges).
LEGACY_TRANSLATED_EDGE_CONFIDENCE = 0.3


class CallGraphEdge(BaseModel):
    """One caller -> callee call-site edge with resolution state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    caller_node_id: UUID
    callee_node_id: UUID | None = None
    callee_qualified_name: str | None = Field(default=None, max_length=1024)
    call_site_file: str = Field(..., max_length=1024)
    call_site_line: int = Field(..., ge=1)
    resolution_status: CallResolutionStatus = CallResolutionStatus.UNRESOLVED
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_resolution(self) -> CallGraphEdge:
        if self.resolution_status == CallResolutionStatus.RESOLVED and self.callee_node_id is None:
            raise ValueError("resolution_status=RESOLVED requires callee_node_id")
        return self


class CallGraph(BaseModel):
    """Call topology keyed by deterministic node identity."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., max_length=128)
    investigation_id: UUID
    root_symbol: str = Field(..., max_length=256)
    root_node_id: UUID | None = None
    revision: str = Field(default="HEAD", max_length=128)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    nodes: dict[UUID, CodeSymbol] = Field(default_factory=dict, max_length=500)
    edges: list[CallGraphEdge] = Field(default_factory=list, max_length=2000)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_node_list(cls, data: Any) -> Any:
        """One-release translator: accept the old ``nodes`` list shape.

        Legacy payloads carry ``nodes`` as a list of
        ``{symbol, depth, invocations}`` objects. Each node becomes a dict
        entry keyed by its symbol identity; each free-text invocation becomes
        an ``UNRESOLVED`` edge at the node's own location with
        :data:`LEGACY_TRANSLATED_EDGE_CONFIDENCE`, since the strings were
        never real call sites (D3 graph-translation-skew mitigation).
        """
        if not isinstance(data, dict):
            return data
        nodes = data.get("nodes")
        if not isinstance(nodes, list):
            return data
        warnings.warn(
            "CallGraph legacy node-list payloads are deprecated; emit nodes-dict plus edges.",
            DeprecationWarning,
            stacklevel=4,
        )
        translated_nodes: dict[str, Any] = {}
        translated_edges: list[dict[str, Any]] = list(data.get("edges") or [])
        for item in nodes:
            if not isinstance(item, dict) or not isinstance(item.get("symbol"), dict):
                continue
            symbol = dict(item["symbol"])
            node_key = str(symbol.get("node_id") or symbol.get("symbol_id") or uuid4())
            if "node_id" not in symbol and "symbol_id" not in symbol:
                symbol["symbol_id"] = node_key
            translated_nodes[node_key] = symbol
            caller_key = str(symbol.get("node_id") or symbol.get("symbol_id") or node_key)
            for invocation in item.get("invocations") or []:
                translated_edges.append(
                    {
                        "caller_node_id": caller_key,
                        "callee_node_id": None,
                        "callee_qualified_name": invocation,
                        "call_site_file": symbol.get("file_path", ""),
                        "call_site_line": symbol.get("line_start", 1),
                        "resolution_status": CallResolutionStatus.UNRESOLVED,
                        "confidence": LEGACY_TRANSLATED_EDGE_CONFIDENCE,
                    }
                )
        return {**data, "nodes": translated_nodes, "edges": translated_edges}

    @classmethod
    def from_legacy_nodes(
        cls,
        legacy_nodes: list[CallGraphNode],
        *,
        tenant_id: str,
        investigation_id: UUID,
        root_symbol: str,
        root_node_id: UUID | None = None,
        revision: str = "HEAD",
        confidence: float = 1.0,
    ) -> CallGraph:
        """Build a graph from pre-D3 ``CallGraphNode`` entries (one release)."""
        nodes: dict[UUID, CodeSymbol] = {}
        edges: list[CallGraphEdge] = []
        for item in legacy_nodes:
            nodes[item.symbol.node_id] = item.symbol
            for invocation in item.invocations:
                edges.append(
                    CallGraphEdge(
                        caller_node_id=item.symbol.node_id,
                        callee_node_id=None,
                        callee_qualified_name=invocation,
                        call_site_file=item.symbol.file_path,
                        call_site_line=item.symbol.line_start,
                        resolution_status=CallResolutionStatus.UNRESOLVED,
                        confidence=LEGACY_TRANSLATED_EDGE_CONFIDENCE,
                    )
                )
        return cls(
            tenant_id=tenant_id,
            investigation_id=investigation_id,
            root_symbol=root_symbol,
            root_node_id=root_node_id,
            revision=revision,
            confidence=confidence,
            nodes=nodes,
            edges=edges,
        )

    @model_validator(mode="after")
    def _validate_graph(self) -> CallGraph:
        for symbol in self.nodes.values():
            if symbol.revision != self.revision and self.revision != "HEAD":
                raise ValueError("call graph node revision does not match graph revision")
        for edge in self.edges:
            if edge.caller_node_id not in self.nodes:
                raise ValueError(f"call edge references unknown caller {edge.caller_node_id}")
            if edge.callee_node_id is not None and edge.callee_node_id not in self.nodes:
                if edge.resolution_status != CallResolutionStatus.EXTERNAL:
                    raise ValueError(f"call edge references unknown callee {edge.callee_node_id}")
        return self


def _extract_node_id(value: Any) -> UUID | None:
    """Resolve an embedded legacy symbol payload to its node identity."""
    if isinstance(value, CodeSymbol):
        return value.node_id
    if isinstance(value, dict):
        raw = value.get("node_id", value.get("symbol_id"))
        if raw is None and isinstance(value.get("symbol"), dict):
            return _extract_node_id(value["symbol"])
        if raw is None:
            return None
        try:
            return raw if isinstance(raw, UUID) else UUID(str(raw))
        except (ValueError, AttributeError, TypeError):
            return None
    return None


class ExceptionPath(BaseModel):
    """Traceability representation linking exception throwing locations to handlers."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., max_length=128)
    investigation_id: UUID
    exception_class: str = Field(..., max_length=256)
    revision: str = Field(default="HEAD", max_length=128)
    throw_node_id: UUID
    handler_node_ids: list[UUID] = Field(default_factory=list, max_length=50)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_embedded_symbols(cls, data: Any) -> Any:
        """Accept pre-D4 payloads embedding ``throw_location``/``handler_locations``."""
        if not isinstance(data, dict):
            return data
        if "throw_node_id" in data or "throw_location" not in data:
            return data
        warnings.warn(
            "ExceptionPath embedded throw/handler locations are deprecated; "
            "use throw_node_id/handler_node_ids.",
            DeprecationWarning,
            stacklevel=4,
        )
        normalized = dict(data)
        throw_id = _extract_node_id(data.get("throw_location"))
        if throw_id is not None:
            normalized["throw_node_id"] = str(throw_id)
        handlers = data.get("handler_locations") or []
        handler_ids = [hid for hid in (_extract_node_id(h) for h in handlers) if hid is not None]
        normalized["handler_node_ids"] = [str(hid) for hid in handler_ids]
        return normalized


class DatabaseOperation(BaseModel):
    """Source code mapping linking code locations to database queries or entities."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str = Field(..., max_length=128)
    investigation_id: UUID
    target_entity_or_table: str = Field(..., max_length=256)
    operation_type: str = Field(..., max_length=64)
    revision: str = Field(default="HEAD", max_length=128)
    source_node_id: UUID
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    raw_query_snippet: str | None = Field(
        default=None, max_length=2048, description="Sanitized query snippet"
    )
    is_classified_sensitive: bool = Field(default=False)

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_embedded_symbol(cls, data: Any) -> Any:
        """Accept pre-D4 payloads embedding ``source_symbol``."""
        if not isinstance(data, dict):
            return data
        if "source_node_id" in data or "source_symbol" not in data:
            return data
        warnings.warn(
            "DatabaseOperation embedded source_symbol is deprecated; use source_node_id.",
            DeprecationWarning,
            stacklevel=4,
        )
        normalized = dict(data)
        source_id = _extract_node_id(data.get("source_symbol"))
        if source_id is not None:
            normalized["source_node_id"] = str(source_id)
        return normalized

    @field_validator("raw_query_snippet")
    @classmethod
    def validate_sensitive_snippet(cls, value: str | None, info: ValidationInfo) -> str | None:
        is_sensitive = info.data.get("is_classified_sensitive", False)
        if is_sensitive and value is not None:
            raise ValueError(
                "Raw query snippet classified as sensitive must be sanitized/redacted before assignment."
            )
        return value
