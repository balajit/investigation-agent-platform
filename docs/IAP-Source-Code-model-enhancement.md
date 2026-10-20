# Design: Source Code Model Enhancement

## Context

The platform carries two code-model layers that grew up separately and now overlap without a contract between them. `src/investigation_agent_platform/domain/code/models.py` holds the investigation-facing shapes (`CodeSymbol`, `CallGraph`, `ExceptionPath`, `DatabaseOperation`, plus the newer `CodeFactResolution` from the Java field-fact work). `src/investigation_agent_platform/domain/topology/models.py` holds the Layer 3 authority (`ASTNodeIdentity` with deterministic UUIDv5 identity via `compute_ast_node_id`, `SourceFileIdentity`, `PackageIdentity`, edge inputs, `ASTTopologyPayload`, attribution results). A review against `tmp/GEMINI_THOUGHTS_COGNEE.md` confirmed the topology layer is the stronger design and the code layer has real structural gaps, listed below. This design closes those gaps with compatible, Cognee-independent hardening: no new dependency, no `DataPoint` inheritance in the domain, no persistence migration.

Confirmed gaps (all verified against current source, not inferred):

- `CodeSymbol.symbol_id` defaults to random `uuid4`. Two ingestions of the same symbol at the same revision produce different IDs, so replay, cross-run joins, and any future projection cannot correlate. Topology's `compute_ast_node_id` (tenant, repository, revision, normalized path, qualified name, node type, full range) already solves this.
- `CodeSymbol` carries line-only ranges (`line_start`, `line_end`) with a `HEAD` default revision. Topology carries start/end columns, requires an immutable revision, and normalizes paths against traversal. Overload disambiguation and exact range joins need columns; `HEAD` silently voids reproducibility.
- `CodeSymbol.parent_symbol` is a bare `str | None` (a name, not an identity). Topology uses `parent_node_id: UUID | None`. Name-based parenting collides on nested and overloaded symbols.
- `CodeSymbol` has no `qualified_name`, `signature_hash`, or `parser_version`. The signature is a free-text 2048-char field, so callers/callees and overloads cannot be keyed deterministically.
- Two symbol taxonomies coexist with no mapping: `SymbolType` (code) vs `TopologyNodeType` (topology). Adapters choose ad hoc, which is how a fourth incompatible shape gets born.
- `CallGraphNode` embeds a full `CodeSymbol` per node and `CallGraph` caps at 500 nodes, so a deep graph duplicates large symbol payloads. `invocations` is a free-text string list with no resolution status, confidence, or callee identity. There is no revision-consistency check between root and nodes.
- `ExceptionPath` and `DatabaseOperation` duplicate code-location information in their own shapes instead of referencing node identities, and neither records resolution confidence or revision pinning.
- `CodeFactResolution` (Java track) is field-shaped (`field_name`, `field_type`, `field_category`) and cannot express locals, call targets, or branch facts; its generalization is owned by `docs/IAP-Code-Java-lang-impl-design.md` and is a non-goal here except for the identity fields this design adds.

## References

- [tmp/GEMINI_THOUGHTS_COGNEE.md](../tmp/GEMINI_THOUGHTS_COGNEE.md) — External review whose gap table (BaseModel vs DataPoint, UUID keys vs typed edges, missing index metadata, UUIDv5 vs UUID4) motivated this hardening; its Cognee-specific remedies are dispositioned in ADR-COGNEE-001, not here.
- [docs/architecture/ADR-COGNEE-001.md](./architecture/ADR-COGNEE-001.md) — Projection-only Cognee verdict and the P1–P8 proposal record; constrains this design to stay Cognee-independent.
- [docs/IAP-Code-Java-lang-impl-design.md](./IAP-Code-Java-lang-impl-design.md) — Owns the `CodeFactResolution` generalization; this design only aligns its identity fields with the node identity below.
- [Cognee custom data models](https://docs.cognee.ai/guides/custom-data-models) — Verified that `DataPoint` subclasses embed `index_fields` into vector storage and expand `DataPoint`-valued fields into edges; informs why the domain stays plain Pydantic and the adapter owns all `DataPoint` mapping.

## Goals / Non-Goals

**Goals:**

- Deterministic, revision-pinned identity for every code symbol, keyed exactly like topology nodes.
- Column-anchored ranges, qualified names, signature hashes, and parser versions on the investigation-facing symbol.
- UUID-based parent linkage replacing the name string.
- A single canonical taxonomy mapping between `SymbolType` and `TopologyNodeType`.
- An edge-based call graph with resolution status and confidence instead of embedded-symbol nodes with free-text invocations.
- Node-referencing exception and database-access shapes with confidence and revision checks.
- Full backward compatibility: old payloads still validate; migration order documented; no database migration.

**Non-Goals:**

- Any Cognee dependency, `DataPoint` inheritance, vector index, or embedding configuration in the domain layer.
- Generalizing `CodeFactResolution` beyond identity alignment (owned by the Java language design).
- New REST endpoints, CLI commands, or persistence tables.
- Whole-program analysis, points-to analysis, or cross-repository static resolution.
- Changing the macro/micro split (`MACRO_NODE_TYPES`) or the Neo4j label layout.

## Decisions

### D1: Deterministic symbol identity mirroring topology

**Decision:** `CodeSymbol` gains `node_id`, `qualified_name`, `start_column`, `end_column`, `signature_hash`, `parser_version`, and `parent_node_id`, with a `create` factory that derives `node_id` through the existing topology `compute_ast_node_id`. The legacy `symbol_id` field stays as an alias of `node_id` so existing readers keep working. Random-ID generation is removed from the factory path; direct construction with an explicit `node_id` remains valid for tests.

```python
# src/investigation_agent_platform/domain/code/models.py
from __future__ import annotations

import hashlib
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from investigation_agent_platform.domain.provenance.models import SourceLocation
from investigation_agent_platform.domain.topology.models import (
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


class CodeSymbol(BaseModel):
    """Structural definition of a source code symbol extracted via AST parsing."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    node_id: UUID = Field(..., alias="symbol_id")
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
```

Pydantic aliases keep the wire shape (`symbol_id`, `parent_symbol`) readable by old code while the canonical fields are UUID identities. Direct construction still accepts legacy payloads without `tenant_id` or `qualified_name` (they default); only the factory path enforces full identity.

**Alternative considered:** Deleting `CodeSymbol` and forcing all readers onto `ASTNodeIdentity`. Rejected — the investigation, verification, and worker paths reference `CodeSymbol` pervasively; a flag-day rename breaks every consumer at once instead of converging through aliases.

---

### D2: Single taxonomy mapping, no third vocabulary

**Decision:** `SYMBOL_TO_TOPOLOGY_TYPE` above is the single source of truth for taxonomy translation, living in `domain/code/models.py` next to `SymbolType`. `ROUTE` and `MESSAGE_HANDLER` have no `SymbolType` counterpart by design (they are topology-only boundary markers); any adapter encountering them maps the enclosing `METHOD`/`FUNCTION` symbol and records the boundary separately on the attribution result, which already carries `node_type` for that purpose. No new enum values are added to either taxonomy in this design.

**Alternative considered:** Merging the enums into one. Rejected — `TopologyNodeType` carries persistence and macro/micro semantics owned by Layer 3; `SymbolType` is a query-dispatch vocabulary. A mapping table converges them without forcing either owner to absorb the other's lifecycle.

---

### D3: Edge-based call graph replacing embedded symbols

**Decision:** `CallGraph` becomes a node-ID-indexed graph: a `nodes` dict of `node_id` to `CodeSymbol` plus a `CallGraphEdge` list carrying caller, optional callee, call-site file and line, `CallResolutionStatus`, and confidence. The 500-node cap is preserved. A model validator enforces single-revision consistency and referential integrity (every edge endpoint known, `RESOLVED` edges require a callee). The old `CallGraphNode.invocations` free-text list is removed; unresolved call sites are explicit edges with `UNRESOLVED` status instead of strings.

```python
# src/investigation_agent_platform/domain/code/models.py
from investigation_agent_platform.domain.topology.models import CallResolutionStatus


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
                    raise ValueError(
                        f"call edge references unknown callee {edge.callee_node_id}"
                    )
        return self
```

This mirrors `ASTTopologyPayload`'s own edge validation (unknown endpoints fail closed, `EXTERNAL` callees exempted) so the two layers finally agree on what a call edge means. Builders that currently emit `CallGraphNode` translate one-to-one: each node becomes a dict entry, each invocation string becomes an `UNRESOLVED` edge at the node's location until the parser supplies real call sites.

**Alternative considered:** Keeping embedded nodes and adding edge fields alongside. Rejected — dual representation doubles payload size and lets the two halves disagree; one representation with a mechanical translation is reviewable.

---

### D4: Node-referencing exception and database shapes

**Decision:** `ExceptionPath` and `DatabaseOperation` reference `node_id`s instead of embedding `CodeSymbol`s, gain `revision` and `confidence`, and validate endpoint membership against a supplied node index at construction sites (validators stay local to avoid circular imports; builders pass `known_node_ids`).

```python
# src/investigation_agent_platform/domain/code/models.py
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

    @field_validator("raw_query_snippet")
    @classmethod
    def validate_sensitive_snippet(cls, value: str | None, info: ValidationInfo) -> str | None:
        is_sensitive = info.data.get("is_classified_sensitive", False)
        if is_sensitive and value is not None:
            raise ValueError(
                "Raw query snippet classified as sensitive must be sanitized/redacted before assignment."
            )
        return value
```

Readers that need the full symbol resolve `node_id` against the graph's node index or the topology payload — one lookup instead of trusting a duplicated, possibly stale embedded copy. The sensitive-snippet guard is unchanged.

**Alternative considered:** Leaving the embedded symbols and adding IDs beside them. Rejected for the same dual-representation reason as D3; the node index is the single copy.

---

### D5: `CodeFactResolution` identity alignment only

**Decision:** `CodeFactResolution` gains `node_id: UUID | None` (the resolved symbol's deterministic identity when the resolver can state it) and `parser_version: str`. No other change in this design; the category generalization lives in `docs/IAP-Code-Java-lang-impl-design.md`. The verifier gains one additional optional check (when both sides carry `node_id`, they must match) without altering any existing proof path.

**Alternative considered:** Doing the full generalization here. Rejected — it belongs to the Java track with its own phases and gates; mixing the two makes both unreviewable.

---

## Data Storage

No new persistence structures and no migration. The changes are Pydantic-shape changes in `domain/code/models.py` only. Neo4j impact is additive and backward compatible: parsers may now write `start_column`, `end_column`, `qualified_name`, and `signature_hash` properties on existing `ASTNode` labels; no new labels, constraints, or indexes are required by this design. A future index on `(qualified_name, revision)` is noted as a possible follow-up once query telemetry shows the need, not created here. This section records that explicitly so no phase invents storage ad hoc.

## Data Structures

Input and output shapes are the revised `CodeSymbol`, `CallGraphEdge`, `CallGraph`, `ExceptionPath`, and `DatabaseOperation` in the Decisions above, plus the unchanged topology models they now align with (`ASTNodeIdentity`, `CallEdgeInput`, `ReferenceEdgeInput`, `DatabaseAccessEdgeInput`, `ASTTopologyPayload`). Creation inputs flow through `CodeSymbol.create` (full identity) while read outputs are the frozen models themselves; the two are never the same object by construction. The taxonomy map `SYMBOL_TO_TOPOLOGY_TYPE` is data, not code, and lives next to the enums so adapters import it instead of branching on names.

## Interfaces

No new REST endpoints, CLI commands, MCP tools, or persistence APIs. Internal interfaces changed:

| Interface | Change | Description |
| --------- | ------ | ----------- |
| `CodeSymbol.create` | New factory | Deterministic-identity construction; only production path for new symbols. |
| `SYMBOL_TO_TOPOLOGY_TYPE` | New mapping | Single taxonomy translation table. |
| `CallGraph` nodes/edges | Shape change | Dict of symbols plus typed edges; builders translate from `CallGraphNode`. |
| `ExceptionPath` / `DatabaseOperation` | Shape change | Node-ID references plus revision and confidence. |
| Parser adapters | Extended | Emit columns, qualified names, signature hashes, parser versions. |

## Implementation Detail

Land in this order; each step is independently green:

1. Add new optional fields and aliases to `CodeSymbol` (`node_id`/`symbol_id`, `parent_node_id`/`parent_symbol`, columns, `qualified_name`, `signature_hash`, `parser_version`, `tenant_id`) with the `create` factory and `SYMBOL_TO_TOPOLOGY_TYPE`. No caller changes yet; full suite green.
2. Migrate parser adapters (`infrastructure/evidence/code/parser.py`, `graphify_adapter.py`, `micro.py`) to `CodeSymbol.create` so new symbols carry deterministic identity. Legacy tests constructing `CodeSymbol(symbol_id=...)` keep passing through the alias.
3. Replace `CallGraphNode` construction with the nodes-plus-edges shape; add a one-release translator accepting the old node list for any lagging caller, then delete it.
4. Convert `ExceptionPath` and `DatabaseOperation` construction sites; resolve embedded symbols to `node_id` at build time.
5. Add the `CodeFactResolution.node_id` alignment and the optional verifier match.
6. Delete deprecated aliases only after a full-suite green with deprecation warnings enabled and no remaining direct `symbol_id=` or embedded-node construction outside tests — a later cleanup, not this design.

### `docs/dev/architecture.md` changes

No `docs/dev/architecture.md` exists in this project; the architecture source of truth is `docs/IAP-architecture-v1.0.md`. This design requires no edits to that file to land. When the cleanup step (item 6) lands, append a short note to the code-topology chapter naming the alias removal and the single taxonomy table with a pointer to this design.

## Migrations

None. Pydantic aliases (`symbol_id`, `parent_symbol`) preserve wire compatibility for banked rows and tests. Neo4j changes are additive node properties with no constraint or label migration. Rollback is safe at every step before item 6: new fields default, old construction validates, and the graph validator treats `HEAD`-revision graphs with the legacy leniency already encoded.

## Testing Philosophy

### Identity determinism

Rebuild the same symbol twice from identical inputs and assert identical `node_id`s; vary each identity input (tenant, repository, revision, path, qualified name, node type, range) independently and assert divergence. Assert `CodeSymbol.create` and `ASTNodeIdentity.create` agree on `node_id` for the same inputs, proving the two layers finally share one identity function.

### Backward compatibility

Construct every revised model from legacy-shaped payloads (random `symbol_id`, name-only parent, line-only ranges, embedded-symbol graphs) and assert validation succeeds with the documented defaults. Fail the suite if any previously green fixture requires editing for reasons other than the intended `invocations` removal, which gets its own explicit fixture updates.

### Graph integrity

Feed the `CallGraph` validator unknown endpoints, `RESOLVED`-without-callee edges, and mixed-revision node sets, and assert each fails closed with a naming error. Mirror the `ASTTopologyPayload` edge tests so the two layers cannot drift apart again.

### Parser adapter parity

For each supported language in the tree-sitter parser, assert emitted symbols carry non-empty qualified names, sane column ranges, and stable parser versions across re-parses of the same revision. Micro-tier on-demand resolution (`MicroSymbolResolver`) must produce IDs identical to persisted macro IDs for the same source range.

### No-Cognee isolation

Assert no module under `domain/code` or `domain/topology` imports `cognee` (an import-lint test), and that all revised models instantiate and validate with the dependency absent. The adapter remains the only Cognee-touching module.

## Documentation Plan

### `docs/IAP-Source-Code-model-enhancement.md`

**Audience:** Implementers and reviewers

This file. It carries the gap record, the revised shapes, the landing order, and the no-migration guarantee. It is updated when each implementation step lands: mark the step as-built, record deviations, and link the landing tests. It is the only model doc implementers need alongside the two model modules.

### `docs/IAP-architecture-v1.0.md`

**Audience:** Platform engineers and operators

No edits required to land this design. The code-topology chapter gains a short alias-removal note only when the final cleanup step lands, naming the single taxonomy table and pointing here.

### `docs/IAP_Arch_Eng_Recommendations.log`

**Audience:** Architecture and engineering stewards

The `OPEN` model-gaps entry flips to `IMPLEMENTED` step by step as gates pass, per the ledger rule, citing test evidence. Any deferred scope (e.g. a future `qualified_name` index) is recorded as a new `OPEN` item rather than silently carried.

## Risks / Trade-offs

### Alias debt

**Risk:** Keeping `symbol_id` and `parent_symbol` aliases alongside canonical `node_id` and `parent_node_id` leaves two names for one thing, inviting new code to use the legacy spelling indefinitely.

**Mitigation:** Factories and builders use canonical names only; a deprecation warning on alias construction starts at landing, and the final cleanup step deletes the aliases behind a full-suite green.

### Column data availability

**Risk:** Some parser paths may not currently emit columns, forcing zeros that weaken overload disambiguation and range joins while looking complete.

**Mitigation:** Columns default to zero but the parser parity tests require real columns per supported language; a `parser_version` of `unknown` plus zero columns is treated by reviewers as missing data, not as a passing shape.

### Graph translation skew

**Risk:** Translating old `CallGraphNode.invocations` strings into `UNRESOLVED` edges fabricates call sites at node locations that were never real call sites.

**Mitigation:** The translator marks these edges with a distinct low confidence and the node's own location, and builders are migrated to emit real call sites; the translator is deleted after one release rather than maintained.

### Over-alignment with topology

**Risk:** Mirroring topology identity too closely could drag Layer 3 lifecycle concerns (snapshots, macro/micro persistence) into investigation-local reasoning.

**Mitigation:** Only the identity function and taxonomy map are shared. Granularity, persistence, snapshots, and retention stay entirely in `domain/topology`; the code layer borrows identity, nothing else.
