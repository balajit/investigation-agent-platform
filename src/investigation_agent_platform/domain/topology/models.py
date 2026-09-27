# src/investigation_agent_platform/domain/topology/models.py
"""Domain models for Layer 3 organizational and static-code topology.

Every tenant-owned node carries ``tenant_id``. Every source-derived node
additionally carries ``repository_id`` and an immutable ``revision`` so
historical attribution is reproducible. See D6 in the design document for the
identity rules these models enforce.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Deterministic namespace for topology node IDs (fixed, arbitrary UUID —
# treat as a constant, never regenerate).
_TOPOLOGY_NAMESPACE = UUID("6f2b6f2e-6b7a-4b7e-9f3a-9d6a2b7c9e10")


class RepositoryType(StrEnum):
    """Canonical repository classification (D9 / F-004 in prompt1_v1.md).

    The dual-frame attribution policy depends on this value being exact and
    authoritative; do not add ad-hoc string classifications elsewhere.
    """

    SERVICE = "SERVICE"
    SHARED_LIBRARY = "SHARED_LIBRARY"
    APPLICATION = "APPLICATION"
    MONOREPO = "MONOREPO"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    TOOLING = "TOOLING"
    UNKNOWN = "UNKNOWN"


class TopologyNodeType(StrEnum):
    """Canonical AST node type. Extends the existing ``SymbolType`` vocabulary
    (see ``domain/code/models.py``) rather than introducing an unrelated
    fourth symbol taxonomy."""

    MODULE = "MODULE"
    CLASS = "CLASS"
    INTERFACE = "INTERFACE"
    METHOD = "METHOD"
    FUNCTION = "FUNCTION"
    CONSTRUCTOR = "CONSTRUCTOR"
    FIELD = "FIELD"
    ENUM = "ENUM"
    ROUTE = "ROUTE"
    MESSAGE_HANDLER = "MESSAGE_HANDLER"


#: Node types persisted to the graph (macro-topology). Everything else is
#: micro detail resolved on demand at query time (ISSUE-3). FIELD is the
#: only micro type today: intra-function variables can never own a domain,
#: so persisting them only inflates the graph. This mapping is the single
#: source of truth for the macro/micro split — adapters filter by it, never
#: by ad-hoc local lists.
MACRO_NODE_TYPES: frozenset[TopologyNodeType] = frozenset(
    {
        TopologyNodeType.MODULE,
        TopologyNodeType.CLASS,
        TopologyNodeType.INTERFACE,
        TopologyNodeType.METHOD,
        TopologyNodeType.FUNCTION,
        TopologyNodeType.CONSTRUCTOR,
        TopologyNodeType.ENUM,
        TopologyNodeType.ROUTE,
        TopologyNodeType.MESSAGE_HANDLER,
    }
)


class TopologySnapshotStatus(StrEnum):
    PENDING = "PENDING"
    INGESTING = "INGESTING"
    READY = "READY"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"
    #: ISSUE-4: the AST/SourceFile/Package subgraph has been deleted by the
    #: retention janitor; the ``TopologySnapshot`` audit node itself is kept
    #: (never deleted) so historical attribution evidence — which embeds its
    #: own ``snapshot_id``/``revision``/ownership path — stays interpretable.
    COLLECTED = "COLLECTED"


class CallResolutionStatus(StrEnum):
    RESOLVED = "RESOLVED"
    AMBIGUOUS = "AMBIGUOUS"
    UNRESOLVED = "UNRESOLVED"
    EXTERNAL = "EXTERNAL"


class AttributionClassification(StrEnum):
    LIBRARY_DEFECT = "LIBRARY_DEFECT"
    CALLER_MISUSE = "CALLER_MISUSE"
    NON_LIBRARY_DEFECT = "NON_LIBRARY_DEFECT"
    INCONCLUSIVE = "INCONCLUSIVE"


class AttributionFallbackLevel(StrEnum):
    AST_NODE = "AST_NODE"
    SOURCE_FILE = "SOURCE_FILE"
    REPOSITORY = "REPOSITORY"
    CODEOWNERS = "CODEOWNERS"
    UNATTRIBUTED = "UNATTRIBUTED"


# ---------------------------------------------------------------------------
# Organizational identity models
# ---------------------------------------------------------------------------


class DomainIdentity(BaseModel):
    """An organizational domain / team ownership boundary (tenant-owned)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(..., max_length=128)
    domain_id: str = Field(..., max_length=128)
    name: str = Field(..., max_length=256)
    owner_team_id: str = Field(..., max_length=128)
    contact_email: str | None = Field(default=None, max_length=256)
    ownership_source: str = Field(default="manual", max_length=64)
    effective_from: datetime = Field(default_factory=lambda: datetime.now(UTC))
    effective_to: datetime | None = None


class GitOrganizationIdentity(BaseModel):
    """A Git organization/group, owned by exactly one domain at a time.

    ``domain_id`` is optional: the org/domain relationship is derived from
    the repository's CODEOWNERS catch-all rule (ISSUE-7) at registration
    time and may be unresolvable (no CODEOWNERS file, no catch-all rule).
    An org without a resolvable domain is still registered — the git
    identity is not manufactured from ownership data, and vice versa.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(..., max_length=128)
    git_org_id: str = Field(..., max_length=128)
    name: str = Field(..., max_length=256)
    provider: str = Field(default="github", max_length=64)
    domain_id: str | None = Field(default=None, max_length=128)


class RepositoryIdentity(BaseModel):
    """Canonical, tenant-owned repository identity.

    This is distinct from ``CodeProfile.repository`` (an access/build
    locator). ``CodeProfile.repository_id`` links the two.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(..., max_length=128)
    repository_id: str = Field(..., max_length=128)
    name: str = Field(..., max_length=256)
    locator: str = Field(..., max_length=512, description="Clone URL or path locator")
    git_org_id: str = Field(..., max_length=128)
    repository_type: RepositoryType = RepositoryType.UNKNOWN
    default_branch: str = Field(default="main", max_length=128)
    is_active: bool = True


class PackageIdentity(BaseModel):
    """A language-aware package/module grouping within one repository revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(..., max_length=128)
    repository_id: str = Field(..., max_length=128)
    revision: str = Field(..., max_length=128)
    path: str = Field(..., max_length=1024)
    language: str = Field(default="unknown", max_length=64)


class SourceFileIdentity(BaseModel):
    """One source file within one immutable repository revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(..., max_length=128)
    repository_id: str = Field(..., max_length=128)
    revision: str = Field(..., max_length=128)
    path: str = Field(..., max_length=1024)
    language: str = Field(default="unknown", max_length=64)
    content_hash: str = Field(default="", max_length=128)
    package_path: str | None = Field(default=None, max_length=1024)


def normalize_file_path(file_path: str) -> str:
    """Normalize a source file path for stable identity generation.

    Collapses backslashes, strips leading ``./``, and rejects absolute paths
    and traversal sequences. Raises ``ValueError`` on unsafe input.
    """
    if not file_path or "\x00" in file_path:
        raise ValueError("file_path must be non-empty and must not contain null bytes")
    normalized = file_path.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("~"):
        raise ValueError("file_path must be repository-relative, not absolute")
    normalized = normalized.removeprefix("./")
    parts = normalized.split("/")
    if any(part in ("..", ".") for part in parts):
        raise ValueError("file_path must not contain traversal segments")
    return normalized


def compute_ast_node_id(
    tenant_id: str,
    repository_id: str,
    revision: str,
    file_path: str,
    qualified_name: str,
    node_type: TopologyNodeType,
    start_line: int,
    start_column: int,
    end_line: int,
    end_column: int,
) -> UUID:
    """Deterministic UUIDv5 identity for one AST node (D6).

    Same-name, nested, overloaded, and historical symbols never collide
    because tenant, repository, revision, and the full source range are all
    part of the identity.
    """
    normalized_path = normalize_file_path(file_path)
    key = "|".join(
        [
            tenant_id,
            repository_id,
            revision,
            normalized_path,
            qualified_name,
            node_type.value,
            str(start_line),
            str(start_column),
            str(end_line),
            str(end_column),
        ]
    )
    return uuid5(_TOPOLOGY_NAMESPACE, key)


class ASTNodeIdentity(BaseModel):
    """Canonical topology AST node identity.

    Reconciles the three pre-existing symbol shapes (domain ``CodeSymbol``,
    port ``CodeSymbol``, parser ``CodeSymbol``) into one topology-owned
    representation. Adapters translate from those existing DTOs into this
    model rather than the platform gaining a fourth incompatible shape.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(..., max_length=128)
    repository_id: str = Field(..., max_length=128)
    revision: str = Field(..., max_length=128)
    node_id: UUID
    name: str = Field(..., max_length=256)
    qualified_name: str = Field(..., max_length=1024)
    node_type: TopologyNodeType
    file_path: str = Field(..., max_length=1024)
    start_line: int = Field(..., ge=1)
    start_column: int = Field(default=0, ge=0)
    end_line: int = Field(..., ge=1)
    end_column: int = Field(default=0, ge=0)
    signature_hash: str = Field(default="", max_length=128)
    parent_node_id: UUID | None = None
    parser_version: str = Field(default="unknown", max_length=64)
    granularity: str = Field(
        default="macro",
        max_length=16,
        description="macro (persisted to the graph) or micro (resolved on demand). "
        "Derived from node_type via MACRO_NODE_TYPES; never set by callers directly.",
    )

    @model_validator(mode="after")
    def _validate_range(self) -> ASTNodeIdentity:
        if self.end_line < self.start_line:
            raise ValueError("end_line cannot be less than start_line")
        return self

    @model_validator(mode="after")
    def _derive_granularity(self) -> ASTNodeIdentity:
        # The mapping is the single source of truth: derive, don't trust
        # input. Mutated in place (via object.__setattr__ for the frozen
        # model) because Pydantic discards non-self returns from top-level
        # validators invoked via __init__.
        expected = "macro" if self.node_type in MACRO_NODE_TYPES else "micro"
        if self.granularity != expected:
            object.__setattr__(self, "granularity", expected)
        return self

    @classmethod
    def create(
        cls,
        tenant_id: str,
        repository_id: str,
        revision: str,
        name: str,
        qualified_name: str,
        node_type: TopologyNodeType,
        file_path: str,
        start_line: int,
        end_line: int,
        start_column: int = 0,
        end_column: int = 0,
        signature: str = "",
        parent_node_id: UUID | None = None,
        parser_version: str = "unknown",
    ) -> ASTNodeIdentity:
        """Factory that derives the deterministic ``node_id`` and normalizes
        the file path, mirroring ``InvestigationAction.create``'s pattern of
        server-derived deterministic identity (see F-057 precedent)."""
        normalized_path = normalize_file_path(file_path)
        node_id = compute_ast_node_id(
            tenant_id=tenant_id,
            repository_id=repository_id,
            revision=revision,
            file_path=normalized_path,
            qualified_name=qualified_name,
            node_type=node_type,
            start_line=start_line,
            start_column=start_column,
            end_line=end_line,
            end_column=end_column,
        )
        signature_hash = hashlib.sha256(signature.encode("utf-8")).hexdigest() if signature else ""
        return cls(
            tenant_id=tenant_id,
            repository_id=repository_id,
            revision=revision,
            node_id=node_id,
            name=name,
            qualified_name=qualified_name,
            node_type=node_type,
            file_path=normalized_path,
            start_line=start_line,
            start_column=start_column,
            end_line=end_line,
            end_column=end_column,
            signature_hash=signature_hash,
            parent_node_id=parent_node_id,
            parser_version=parser_version,
        )

    @property
    def source_range_size(self) -> int:
        return max(0, self.end_line - self.start_line)


# ---------------------------------------------------------------------------
# Edge input models
# ---------------------------------------------------------------------------


class CallEdgeInput(BaseModel):
    """A caller -> callee call-site edge (validated ingestion input)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    caller_node_id: UUID
    callee_node_id: UUID | None = None
    callee_qualified_name: str | None = Field(default=None, max_length=1024)
    call_site_file: str = Field(..., max_length=1024)
    call_site_line: int = Field(..., ge=1)
    resolution_status: CallResolutionStatus = CallResolutionStatus.UNRESOLVED
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_resolution(self) -> CallEdgeInput:
        if self.resolution_status == CallResolutionStatus.RESOLVED and self.callee_node_id is None:
            raise ValueError("resolution_status=RESOLVED requires callee_node_id")
        return self


class ReferenceEdgeInput(BaseModel):
    """A generic symbol reference edge (import, type reference, etc.)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_node_id: UUID
    target_node_id: UUID
    reference_type: str = Field(default="REFERENCES", max_length=64)
    file_path: str = Field(..., max_length=1024)
    line_number: int = Field(..., ge=1)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class DatabaseAccessEdgeInput(BaseModel):
    """A sanitized code -> database-resource access edge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_node_id: UUID
    target_entity_or_table: str = Field(..., max_length=256)
    operation_type: str = Field(..., max_length=64)
    query_fingerprint: str = Field(default="", max_length=128)


# ---------------------------------------------------------------------------
# Ingestion payload
# ---------------------------------------------------------------------------


class ParseDiagnostic(BaseModel):
    """A bounded, non-sensitive parse warning/error surfaced during ingestion."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file_path: str = Field(..., max_length=1024)
    message: str = Field(..., max_length=1024)
    severity: str = Field(default="WARNING", max_length=32)


class ASTTopologyPayload(BaseModel):
    """Versioned, validated ingestion input for one immutable repository revision.

    The topology ingestion boundary never accepts a raw, unvalidated
    ``dict``; every field here is schema-checked before it reaches Neo4j or
    any other adapter.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default="v1", max_length=32)
    parser_version: str = Field(..., max_length=64)
    tenant_id: str = Field(..., max_length=128)
    application_id: str = Field(..., max_length=128)
    repository_id: str = Field(..., max_length=128)
    revision: str = Field(..., max_length=128)
    snapshot_id: UUID
    source_files: list[SourceFileIdentity] = Field(default_factory=list, max_length=20000)
    ast_nodes: list[ASTNodeIdentity] = Field(default_factory=list, max_length=200000)
    calls: list[CallEdgeInput] = Field(default_factory=list, max_length=200000)
    references: list[ReferenceEdgeInput] = Field(default_factory=list, max_length=200000)
    database_accesses: list[DatabaseAccessEdgeInput] = Field(default_factory=list, max_length=20000)
    diagnostics: list[ParseDiagnostic] = Field(default_factory=list, max_length=1000)
    payload_hash: str = Field(..., max_length=128)

    @model_validator(mode="after")
    def _validate_tenant_and_revision_consistency(self) -> ASTTopologyPayload:
        for node in self.ast_nodes:
            if node.tenant_id != self.tenant_id or node.repository_id != self.repository_id:
                raise ValueError(
                    f"AST node {node.node_id} tenant/repository does not match payload scope"
                )
            if node.revision != self.revision:
                raise ValueError(
                    f"AST node {node.node_id} revision does not match payload revision"
                )
        for source_file in self.source_files:
            if (
                source_file.tenant_id != self.tenant_id
                or source_file.repository_id != self.repository_id
            ):
                raise ValueError("source file tenant/repository does not match payload scope")
            if source_file.revision != self.revision:
                raise ValueError("source file revision does not match payload revision")
        node_ids = {n.node_id for n in self.ast_nodes}
        for call in self.calls:
            if call.caller_node_id not in node_ids:
                raise ValueError(f"call edge references unknown caller node {call.caller_node_id}")
            if call.callee_node_id is not None and call.callee_node_id not in node_ids:
                # Cross-repository/unresolved callees are allowed only when
                # explicitly marked EXTERNAL; otherwise the callee must be in
                # this same payload.
                if call.resolution_status != CallResolutionStatus.EXTERNAL:
                    raise ValueError(
                        f"call edge references callee node {call.callee_node_id} not present in payload"
                    )
        return self


class TopologyIngestionResult(BaseModel):
    """Outcome of one ingestion attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(..., max_length=128)
    repository_id: str = Field(..., max_length=128)
    revision: str = Field(..., max_length=128)
    snapshot_id: UUID
    status: TopologySnapshotStatus
    node_count: int = Field(default=0, ge=0)
    edge_count: int = Field(default=0, ge=0)
    micro_skipped_count: int = Field(
        default=0,
        ge=0,
        description="Micro-tier nodes filtered out of the persisted graph (ISSUE-3)",
    )
    error_summary: str | None = Field(default=None, max_length=2000)


# ---------------------------------------------------------------------------
# Snapshot retention / garbage collection (ISSUE-4)
# ---------------------------------------------------------------------------


class SnapshotDescriptor(BaseModel):
    """One repository-revision snapshot's retention-relevant metadata.

    Produced by ``SnapshotRetentionPort.list_snapshots``; consumed by the
    pure ``classify_snapshots`` policy function. Deliberately does not carry
    node/edge counts — only what the *policy* needs to decide pinned vs.
    collectible.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(..., max_length=128)
    repository_id: str = Field(..., max_length=128)
    revision: str = Field(..., max_length=128)
    status: TopologySnapshotStatus
    ingested_at: datetime = Field(
        description="When this snapshot last reached READY; drives the age-based window."
    )


class SnapshotCollectionResult(BaseModel):
    """Outcome of one ``collect_snapshot`` call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(..., max_length=128)
    repository_id: str = Field(..., max_length=128)
    revision: str = Field(..., max_length=128)
    already_collected: bool = Field(
        default=False, description="True when this call was a no-op retry (idempotent)."
    )
    deleted_node_count: int = Field(default=0, ge=0)
    deleted_edge_count: int = Field(default=0, ge=0)


class StaticOwnershipResult(BaseModel):
    """Result of resolving one source location to its static ownership path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(..., max_length=128)
    repository_id: str = Field(..., max_length=128)
    revision: str = Field(..., max_length=128)
    snapshot_id: UUID
    matched_node_id: UUID | None = None
    matched_node_qualified_name: str | None = Field(default=None, max_length=1024)
    matched_file_path: str = Field(..., max_length=1024)
    matched_start_line: int | None = None
    matched_end_line: int | None = None
    repository_type: RepositoryType = RepositoryType.UNKNOWN
    domain_id: str | None = Field(default=None, max_length=128)
    git_org_id: str | None = Field(default=None, max_length=128)
    ownership_path: list[str] = Field(default_factory=list, max_length=10)
    fallback_level: AttributionFallbackLevel = AttributionFallbackLevel.AST_NODE
    node_type: TopologyNodeType | None = Field(
        default=None,
        description="Matched AST node's type when an AST match was found "
        "(ISSUE-5: lets the attribution service detect ROUTE/MESSAGE_HANDLER "
        "boundaries where a cross-repository trace hop may be attempted).",
    )


class MicroSymbolMatch(BaseModel):
    """On-demand micro-tier symbol resolution (ISSUE-3).

    Produced by parsing the working-tree file at query time instead of
    reading the persisted macro graph. The symbol identity is deterministic
    (same ``ASTNodeIdentity.create`` path); only the *source* differs, which
    is why the resolution tier is recorded separately on the attribution
    result rather than here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: ASTNodeIdentity
    revision_verified: bool = Field(
        description="True when the checked-out working tree revision matches "
        "the requested revision; False means the match is approximate."
    )


class StackFrameLocation(BaseModel):
    """One resolved stack frame used as input to dual-frame attribution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository_id: str = Field(..., max_length=128)
    revision: str = Field(..., max_length=128)
    file_path: str = Field(..., max_length=1024)
    line_number: int = Field(..., ge=1)


class TraceHopTarget(BaseModel):
    """A corroborated cross-service hop target, derived from runtime trace
    evidence (ISSUE-5) — never guessed from static name similarity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_service: str = Field(..., max_length=256)
    target_span_id: str = Field(..., max_length=128)
    parent_span_id: str | None = Field(default=None, max_length=128)


class CrossRepositoryHop(BaseModel):
    """One traced hop from a ``ROUTE``/``MESSAGE_HANDLER`` boundary in the
    source repository to a domain resolved in a different repository
    (ISSUE-5). Always corroborated by trace evidence — a hop that cannot be
    corroborated is never recorded, and attribution falls back to the
    single-repository result instead.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_repository_id: str = Field(..., max_length=128)
    source_revision: str = Field(..., max_length=128)
    source_node_id: UUID | None = None
    trace_id: str = Field(..., max_length=128)
    span_id: str | None = Field(default=None, max_length=128)
    target_span_id: str = Field(..., max_length=128)
    target_service: str = Field(..., max_length=256)
    target_repository_id: str = Field(..., max_length=128)
    target_revision: str = Field(..., max_length=128)
    target_domain_id: str | None = Field(default=None, max_length=128)
    corroborated: bool = Field(
        default=True,
        description="Always True for a recorded hop — uncorroborated hops "
        "are never stored, per the fail-closed cross-repo policy.",
    )


class DomainAttributionResult(BaseModel):
    """Final, provenanced dual-frame attribution result (D8).

    This is converted to a standard ``Evidence`` record with
    ``EvidenceProvenance`` before it can influence reasoning or conclusions
    (see ``application/topology/attribution.py``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(..., max_length=128)
    application_id: str = Field(..., max_length=128)
    investigation_id: UUID
    repository_id: str = Field(..., max_length=128)
    revision: str = Field(..., max_length=128)
    snapshot_id: UUID | None = None
    matched_node_id: UUID | None = None
    matched_file_path: str = Field(default="", max_length=1024)
    matched_line_range: tuple[int, int] | None = None
    ownership_path: list[str] = Field(default_factory=list, max_length=10)
    repository_type: RepositoryType = RepositoryType.UNKNOWN
    failure_frame_domain_id: str | None = Field(default=None, max_length=128)
    caller_frame_domain_id: str | None = Field(default=None, max_length=128)
    proposed_culprit_domain_id: str | None = Field(default=None, max_length=128)
    proposed_victim_domain_id: str | None = Field(default=None, max_length=128)
    classification: AttributionClassification = AttributionClassification.INCONCLUSIVE
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    alternatives: list[str] = Field(default_factory=list, max_length=10)
    limitations: list[str] = Field(default_factory=list, max_length=20)
    supporting_evidence_ids: list[UUID] = Field(default_factory=list, max_length=50)
    contradicting_evidence_ids: list[UUID] = Field(default_factory=list, max_length=50)
    attribution_rule_version: str = Field(default="v1", max_length=32)
    fallback_level: AttributionFallbackLevel = AttributionFallbackLevel.AST_NODE
    ownership_source: str = Field(
        default="static",
        max_length=16,
        description="static (topology ownership mapping) or codeowners "
        "(CODEOWNERS-file macro attribution, confidence-capped).",
    )
    resolution_tier: str = Field(
        default="macro",
        max_length=16,
        description="macro (graph lookup) or micro (on-demand parse). Recorded in provenance (ISSUE-3).",
    )
    hops: list[CrossRepositoryHop] = Field(
        default_factory=list,
        max_length=10,
        description="Cross-repository trace hops taken during attribution "
        "(ISSUE-5). Empty when the failure resolved within a single "
        "repository, or when a boundary was reached but no corroborating "
        "trace evidence was found — the primary classification is never "
        "based on an uncorroborated hop.",
    )
