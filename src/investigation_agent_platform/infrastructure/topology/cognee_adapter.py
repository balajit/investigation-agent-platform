# src/investigation_agent_platform/infrastructure/topology/cognee_adapter.py
"""Optional Cognee enrichment pilot (Part 8, Track B).

Status: pilot scaffolding behind ``IAP_COGNEE_ENABLED`` (default off).
Deterministic projection only — ``DataPoint`` pipeline, never
``add()``/``cognify()`` on AST data, retrieval-only search types, opaque
server-derived datasets. All ``cognee`` imports are isolated to this module;
when the dependency is absent every entrypoint raises
``TopologyNotConfiguredError`` instead of importing anything.

Reads stay on the Neo4j adapter until the pilot verdict (see
``docs/IAP-implementation-part8-knowledge-v1.md`` D7): this module projects
and drops projections; it does not serve attribution queries.
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from investigation_agent_platform.domain.common.exceptions import (
    TopologyNotConfiguredError,
)
from investigation_agent_platform.domain.topology.models import ASTTopologyPayload

logger = logging.getLogger(__name__)

COGNEE_AVAILABLE = importlib.util.find_spec("cognee") is not None

# Retrieval-only search types permitted for enrichment reads. Any
# ``*_COMPLETION`` variant is unreachable by construction (see
# ``assert_retrieval_only``) — LLM-written strings never become evidence.
RETRIEVAL_ONLY_SEARCH_TYPES = frozenset({"CHUNKS", "SUMMARIES", "CHUNKS_LEXICAL", "CYPHER", "CODE"})

# Non-overridable guardrail (Part 8 D5): completion search types can never be
# enabled through configuration. This is a code constant, not an env knob —
# there is deliberately no `IAP_COGNEE_ALLOW_COMPLETION=true` path.
ALLOW_COMPLETION: bool = False


def assert_retrieval_only(search_type: str) -> None:
    """Reject completion search types before any network call."""
    if not ALLOW_COMPLETION and search_type not in RETRIEVAL_ONLY_SEARCH_TYPES:
        raise TopologyNotConfiguredError(
            f"Cognee search type {search_type!r} is not retrieval-only; "
            "completion output must never become evidence"
        )


def derive_dataset_id(tenant_id: str, repository_id: str, revision: str, salt: str) -> str:
    """Opaque server-derived dataset identifier (hash, never interpolated names)."""
    if not salt:
        raise TopologyNotConfiguredError("IAP_COGNEE_DATASET_SALT is required for pilot datasets")
    digest = hashlib.sha256(f"{salt}|{tenant_id}|{repository_id}|{revision}".encode()).hexdigest()
    return f"cg_{digest[:32]}"


if COGNEE_AVAILABLE:
    from cognee.infrastructure.engine import (
        DataPoint,  # type: ignore[import-not-found, import-untyped]
    )
    from pydantic import Field

    class TopologySymbolPoint(DataPoint):  # type: ignore[no-redef, misc]
        """Deterministic projection of one ASTNodeIdentity (Part 8 D2).

        ``linked`` carries ``(Edge, target-point)`` tuples that the Cognee
        graph walk expands into CALLS/REFERENCES edges with exact labels —
        the same vocabulary as the Layer 3 authority, never invented labels.
        """

        node_id: str
        tenant_id: str
        repository_id: str
        revision: str
        qualified_name: str
        node_type: str
        file_path: str
        start_line: int
        end_line: int
        ownership_path: list[str]
        linked: list[Any] = Field(default_factory=list)

        metadata: dict[str, Any] = {"index_fields": ["qualified_name"], "identity_fields": ["node_id"]}  # noqa: RUF012 - DataPoint field metadata must be a plain dict per Cognee custom-model docs

else:

    @dataclass(frozen=True)
    class TopologySymbolPoint:  # type: ignore[no-redef]
        """Dependency-free mirror of the DataPoint shape for mapping tests.

        Used only to prove deterministic derivation without the ``cognee``
        package installed. Projection itself requires the dependency.
        """

        node_id: str
        tenant_id: str
        repository_id: str
        revision: str
        qualified_name: str
        node_type: str
        file_path: str
        start_line: int
        end_line: int
        ownership_path: tuple[str, ...] = ()
        linked: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class PilotProjectionResult:
    """Audit record for one projection (parity with TopologyIngestionResult)."""

    dataset: str
    point_count: int
    edge_count: int
    projection_hash: str


def map_payload_to_points(
    payload: ASTTopologyPayload,
    calls: Sequence[object] = (),
    references: Sequence[object] = (),
    ownership_path: Sequence[str] = (),
) -> list[TopologySymbolPoint]:
    """Deterministic payload-to-point mapping (no I/O, no cognee needed).

    ``calls``/``references`` are the payload's edge inputs; macro endpoints
    present in this payload become ``(Edge, target)`` tuples on the source
    point's ``linked`` field. Dangling or micro-tier endpoints are skipped,
    never guessed — mirroring the Neo4j adapter's filtering exactly.
    ``ownership_path`` (org + domain from registration, possibly empty) is
    stored as a property on every point; empty means unjoined, never absent.
    """
    from investigation_agent_platform.domain.topology.models import (
        CallEdgeInput,
        ReferenceEdgeInput,
    )

    points: list[TopologySymbolPoint] = []
    by_id: dict[str, Any] = {}
    for node in payload.ast_nodes:
        if node.granularity != "macro":
            continue
        kwargs: dict[str, Any] = {
            "node_id": str(node.node_id),
            "tenant_id": node.tenant_id,
            "repository_id": node.repository_id,
            "revision": node.revision,
            "qualified_name": node.qualified_name,
            "node_type": node.node_type.value,
            "file_path": node.file_path,
            "start_line": node.start_line,
            "end_line": node.end_line,
        }
        if COGNEE_AVAILABLE:
            kwargs["ownership_path"] = list(ownership_path)  # type: ignore[assignment]
            point = TopologySymbolPoint(**kwargs)  # type: ignore[arg-type]
            points.append(point)
            by_id[point.node_id] = point
        else:
            kwargs["ownership_path"] = tuple(ownership_path)
            point = TopologySymbolPoint(**kwargs)  # type: ignore[arg-type]
            points.append(point)
            by_id[point.node_id] = point
    edge_pairs: list[tuple[str, str, str]] = []
    for edge in list(calls) + list(references):
        if isinstance(edge, CallEdgeInput):
            if edge.callee_node_id is None:
                continue
            edge_pairs.append((str(edge.caller_node_id), str(edge.callee_node_id), "CALLS"))
        elif isinstance(edge, ReferenceEdgeInput):
            edge_pairs.append((str(edge.source_node_id), str(edge.target_node_id), "REFERENCES"))

    for source_id, target_id, rel in edge_pairs:
        source, target = by_id.get(source_id), by_id.get(target_id)
        if source is None or target is None:
            continue
        if COGNEE_AVAILABLE:
            from cognee.infrastructure.engine import (
                Edge as _Edge2,  # type: ignore[import-not-found, import-untyped]
            )

            source.linked.append((_Edge2(relationship_type=rel), target))
        else:
            object.__setattr__(source, "linked", source.linked + ((rel, target_id),))
    return points


class CogneeTopologyAdapter:
    """Pilot-only projection adapter (no read path until verdict).

    Constructed from two primitives (not the full ``TopologyConfig``) so the
    operator CLI can build it from ``IAP_COGNEE_*`` env without a database or
    LLM configuration. Platform wiring passes the same two values through
    from ``TopologyConfig`` when the live pipeline lands.
    """

    def __init__(self, *, enabled: bool, dataset_salt: str) -> None:
        if not enabled:
            raise TopologyNotConfiguredError("Cognee pilot is disabled (IAP_COGNEE_ENABLED=false)")
        if not dataset_salt:
            raise TopologyNotConfiguredError(
                "Cognee pilot requires IAP_COGNEE_DATASET_SALT (production must set it)"
            )
        self._salt = dataset_salt

    def _require_cognee(self) -> None:
        if not COGNEE_AVAILABLE:
            raise TopologyNotConfiguredError(
                "cognee package is not installed; pilot projection unavailable"
            )

    async def project_snapshot(
        self, payload: ASTTopologyPayload, ownership_path: list[str] | None = None
    ) -> PilotProjectionResult:
        """Project one READY payload deterministically (idempotent by point IDs).

        Symbol points plus CALLS/REFERENCES edges flow through
        ``add_data_points(graph_only=True)`` — the walk expands ``linked``
        tuples into exactly-labeled edges. ``ownership_path`` (org + domain
        from registration) is stored per point for later read joins; empty
        means unjoined, never manufactured. Graph-only write: no vector
        engine, no embedder, no LLM calls.
        """
        self._require_cognee()
        from cognee.tasks.storage import (
            add_data_points,  # type: ignore[import-not-found, import-untyped]
        )

        dataset = derive_dataset_id(payload.tenant_id, payload.repository_id, payload.revision,
                                    self._salt)
        points = map_payload_to_points(
            payload, payload.calls, payload.references, ownership_path or [])
        await add_data_points(points, graph_only=True)
        canonical = "|".join(sorted(p.node_id for p in points))
        projection_hash = hashlib.sha256(f"{dataset}|{canonical}".encode()).hexdigest()
        logger.info("Cognee pilot projection written",
                    extra={"dataset": dataset, "points": len(points)})
        return PilotProjectionResult(dataset=dataset, point_count=len(points),
                                     edge_count=len(payload.calls) + len(payload.references),
                                     projection_hash=projection_hash)

    async def drop_projection(self, tenant_id: str, repository_id: str, revision: str) -> None:
        """Delete one projection's points (retention janitor hook).

        Tenant+revision-scoped DETACH DELETE over ``TopologySymbolPoint``
        nodes only — Layer 3 and Graphiti data are never touched. Connection
        comes from the same ``GRAPH_DATABASE_*`` env the pilot script maps
        from ``IAP_TOPOLOGY_NEO4J_*``.
        """
        self._require_cognee()
        import os

        from neo4j import AsyncGraphDatabase  # type: ignore[import-not-found, import-untyped]

        dataset = derive_dataset_id(tenant_id, repository_id, revision, self._salt)
        driver = AsyncGraphDatabase.driver(
            os.environ.get("GRAPH_DATABASE_URL", "bolt://localhost:7687"),
            auth=(
                os.environ.get("GRAPH_DATABASE_USERNAME", "neo4j"),
                os.environ.get("GRAPH_DATABASE_PASSWORD", ""),
            ),
        )
        try:
            async with driver.session(
                database=os.environ.get("GRAPH_DATABASE_NAME", "neo4j")
            ) as session:
                result = await session.run(
                    "MATCH (n:TopologySymbolPoint {tenant_id: $tenant_id, "
                    "repository_id: $repository_id, revision: $revision}) "
                    "DETACH DELETE n RETURN count(n) AS deleted",
                    tenant_id=tenant_id,
                    repository_id=repository_id,
                    revision=revision,
                )
                record = await result.single()
                deleted = record["deleted"] if record else 0
        finally:
            await driver.close()
        logger.info("Cognee pilot projection dropped",
                    extra={"dataset": dataset, "deleted": deleted})

    async def search_symbols(
        self, tenant_id: str, query_text: str, search_type: str, dataset: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Retrieval-only enrichment read primitive (pilot).

        Only ``RETRIEVAL_ONLY_SEARCH_TYPES`` are reachable (guarded twice:
        ``assert_retrieval_only`` plus the ``ALLOW_COMPLETION`` constant), and
        results are filtered to the caller's tenant in this layer before
        return — Cognee namespaces are never trusted as the boundary. This is
        a retrieval primitive, not a port implementation: attribution reads
        stay on the Neo4j adapter until the pilot verdict wires the port.
        """
        self._require_cognee()
        assert_retrieval_only(search_type)
        from cognee import SearchType  # type: ignore[import-not-found, import-untyped]
        from cognee import search as cognee_search

        raw = await cognee_search(
            query_text=query_text,
            query_type=SearchType(search_type),
            datasets=[dataset],
        )
        items = raw if isinstance(raw, list) else [raw]
        out: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, dict):
                if item.get("tenant_id", tenant_id) != tenant_id:
                    continue
                out.append(item)
            else:
                out.append({"result": str(item), "tenant_id": tenant_id})
            if len(out) >= limit:
                break
        return out


class CogneeAttributionReader:
    """Port-level read path over Cognee projections (Part 8 addendum).

    Implements ``DomainAttributionPort.resolve_source_location`` with the
    same contract as the Neo4j adapter: tenant/repo/revision-qualified
    predicates first, most-specific enclosing point wins deterministically,
    ties raise instead of guessing. Reads direct CYPHER (retrieval-only, no
    LLM anywhere); the caller guarantees READY projections. ``snapshot_id``
    is a projection-scoped deterministic UUID (not the Layer 3 snapshot) —
    recorded as such, never confused with it.
    """

    _PROJECTION_NS = __import__("uuid").NAMESPACE_URL

    def __init__(self, *, enabled: bool, dataset_salt: str) -> None:
        if not enabled:
            raise TopologyNotConfiguredError("Cognee pilot is disabled (IAP_COGNEE_ENABLED=false)")
        if not dataset_salt:
            raise TopologyNotConfiguredError(
                "Cognee pilot requires IAP_COGNEE_DATASET_SALT (production must set it)"
            )
        self._salt = dataset_salt

    def _require_cognee(self) -> None:
        if not COGNEE_AVAILABLE:
            raise TopologyNotConfiguredError(
                "cognee package is not installed; pilot reads unavailable"
            )

    async def resolve_source_location(
        self,
        tenant_id: str,
        application_id: str,
        investigation_id: object,
        repository_id: str,
        revision: str,
        file_path: str,
        line_number: int,
    ) -> object:
        """Resolve one location to its stored ownership path."""
        import os
        import uuid

        from neo4j import AsyncGraphDatabase  # type: ignore[import-not-found, import-untyped]

        from investigation_agent_platform.domain.common.exceptions import (
            TopologyAmbiguousMatchError,
        )
        from investigation_agent_platform.domain.topology.models import (
            AttributionFallbackLevel,
            StaticOwnershipResult,
            TopologyNodeType,
        )

        self._require_cognee()
        _ = application_id, investigation_id  # port signature parity; unused by reads
        driver = AsyncGraphDatabase.driver(
            os.environ.get("GRAPH_DATABASE_URL", "bolt://localhost:7687"),
            auth=(
                os.environ.get("GRAPH_DATABASE_USERNAME", "neo4j"),
                os.environ.get("GRAPH_DATABASE_PASSWORD", ""),
            ),
        )
        try:
            async with driver.session(
                database=os.environ.get("GRAPH_DATABASE_NAME", "neo4j")
            ) as session:
                result = await session.run(
                    "MATCH (n:TopologySymbolPoint {tenant_id: $tenant_id, "
                    "repository_id: $repository_id, revision: $revision, "
                    "file_path: $file_path}) "
                    "WHERE n.start_line <= $line AND n.end_line >= $line "
                    "RETURN n.node_id AS node_id, n.qualified_name AS qualified_name, "
                    "n.node_type AS node_type, n.start_line AS start_line, "
                    "n.end_line AS end_line, n.ownership_path AS ownership_path",
                    tenant_id=tenant_id,
                    repository_id=repository_id,
                    revision=revision,
                    file_path=file_path,
                    line=line_number,
                )
                rows = [dict(r) async for r in result]
        finally:
            await driver.close()

        snapshot_id = uuid.uuid5(
            self._PROJECTION_NS, f"cognee:{tenant_id}:{repository_id}:{revision}"
        )
        if not rows:
            return StaticOwnershipResult(
                tenant_id=tenant_id,
                repository_id=repository_id,
                revision=revision,
                snapshot_id=snapshot_id,
                matched_file_path=file_path,
                ownership_path=[],
                fallback_level=AttributionFallbackLevel.REPOSITORY,
            )
        rows.sort(key=lambda r: (r["end_line"] - r["start_line"], str(r["node_id"])))
        smallest = rows[0]["end_line"] - rows[0]["start_line"]
        tied = [r for r in rows if r["end_line"] - r["start_line"] == smallest]
        if len({r["node_id"] for r in tied}) > 1:
            raise TopologyAmbiguousMatchError(
                f"{len(tied)} equally specific points match {file_path}:{line_number}"
            )
        matched = tied[0]
        try:
            node_type = TopologyNodeType(matched["node_type"])
        except ValueError:
            node_type = None
        path = list(matched.get("ownership_path") or [])
        return StaticOwnershipResult(
            tenant_id=tenant_id,
            repository_id=repository_id,
            revision=revision,
            snapshot_id=snapshot_id,
            matched_node_id=matched["node_id"],
            matched_node_qualified_name=matched["qualified_name"],
            matched_file_path=file_path,
            matched_start_line=matched["start_line"],
            matched_end_line=matched["end_line"],
            domain_id=path[1] if len(path) > 1 else None,
            git_org_id=path[0] if path else None,
            ownership_path=path,
            fallback_level=AttributionFallbackLevel.AST_NODE,
            node_type=node_type,
        )
