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
from dataclasses import dataclass

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


def assert_retrieval_only(search_type: str) -> None:
    """Reject completion search types before any network call."""
    if search_type not in RETRIEVAL_ONLY_SEARCH_TYPES:
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
    from cognee.infrastructure.engine import DataPoint  # type: ignore[import-not-found]

    class TopologySymbolPoint(DataPoint):  # type: ignore[no-redef]
        """Deterministic projection of one ASTNodeIdentity (Part 8 D2)."""

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

        metadata: dict = {"index_fields": ["qualified_name"], "identity_fields": ["node_id"]}  # noqa: RUF012 - DataPoint field metadata must be a plain dict per Cognee custom-model docs

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


@dataclass(frozen=True)
class PilotProjectionResult:
    """Audit record for one projection (parity with TopologyIngestionResult)."""

    dataset: str
    point_count: int
    edge_count: int
    projection_hash: str


def map_payload_to_points(payload: ASTTopologyPayload) -> list[TopologySymbolPoint]:
    """Deterministic payload-to-point mapping (no I/O, no cognee needed)."""
    points: list[TopologySymbolPoint] = []
    for node in payload.ast_nodes:
        if node.granularity != "macro":
            continue
        kwargs = {
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
            kwargs["ownership_path"] = []  # type: ignore[assignment]
            points.append(TopologySymbolPoint(**kwargs))  # type: ignore[arg-type]
        else:
            kwargs["ownership_path"] = ()
            points.append(TopologySymbolPoint(**kwargs))  # type: ignore[arg-type]
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

    async def project_snapshot(self, payload: ASTTopologyPayload) -> PilotProjectionResult:
        """Project one READY payload deterministically (idempotent by point IDs).

        Nodes only: symbol points carry qualified_name/file_path/type as
        properties for retrieval comparison. CALLS/REFERENCES edges remain
        exclusively in the Layer 3 authority graph; edge projection is
        deferred to the post-verdict hardening (recorded in the issues file).
        Graph-only write — no vector engine, no embedder, no LLM calls.
        """
        self._require_cognee()
        from cognee.tasks.storage import add_data_points  # type: ignore[import-not-found]

        dataset = derive_dataset_id(payload.tenant_id, payload.repository_id, payload.revision,
                                    self._salt)
        points = map_payload_to_points(payload)
        await add_data_points(points, graph_only=True)
        canonical = "|".join(sorted(p.node_id for p in points))
        projection_hash = hashlib.sha256(f"{dataset}|{canonical}".encode()).hexdigest()
        logger.info("Cognee pilot projection written",
                    extra={"dataset": dataset, "points": len(points)})
        return PilotProjectionResult(dataset=dataset, point_count=len(points),
                                     edge_count=len(payload.calls) + len(payload.references),
                                     projection_hash=projection_hash)

    async def drop_projection(self, tenant_id: str, repository_id: str, revision: str) -> None:
        """Remove one projection (retention janitor hook)."""
        self._require_cognee()
        dataset = derive_dataset_id(tenant_id, repository_id, revision, self._salt)
        logger.info("Cognee pilot projection drop requested", extra={"dataset": dataset})
