# src/investigation_agent_platform/application/correlation/engine.py
import logging
from typing import Any
from uuid import UUID

import rustworkx as rx
from opentelemetry import trace

from investigation_agent_platform.ports.correlation.engine import (
    CorrelationEdge,
    CorrelationExpander,
    CorrelationGraph,
    CorrelationNode,
)
from investigation_agent_platform.ports.persistence.repositories import (
    EvidenceRelationshipRepository,
    EvidenceRepository,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class EphemeralCorrelationEngine(CorrelationExpander):
    """In-memory Rust-backed correlation traversal engine hydrated from persistent state."""

    def __init__(
        self,
        evidence_relationship_repo: EvidenceRelationshipRepository,
        evidence_repository: EvidenceRepository,
        max_node_limit: int = 500,
    ) -> None:
        self._relationship_repo = evidence_relationship_repo
        self._evidence_repo = evidence_repository
        self._max_node_limit = max_node_limit

    async def expand_correlation(
        self,
        tenant_id: str,
        application_id: str,
        root_evidence_ids: list[UUID],
        max_depth: int,
    ) -> CorrelationGraph:
        """Hydrates relationships from persistence and performs fast rustworkx graph traversal."""
        with tracer.start_as_current_span("EphemeralCorrelationEngine.expand_correlation") as span:
            span.set_attribute("tenant_id", tenant_id)
            span.set_attribute("application_id", application_id)
            span.set_attribute("root_count", len(root_evidence_ids))

            logger.info(
                "Expanding correlation graph",
                extra={
                    "tenant_id": tenant_id,
                    "application_id": application_id,
                    "root_evidence_ids": [str(rid) for rid in root_evidence_ids],
                    "max_depth": max_depth,
                },
            )

            root_str_ids = [str(rid) for rid in root_evidence_ids]
            records = await self._relationship_repo.fetch_relationships_for_evidence(
                tenant_id=tenant_id,
                application_id=application_id,
                root_evidence_ids=root_str_ids,
                max_depth=max_depth,
            )

            graph: Any = rx.PyDiGraph(multigraph=False)
            node_indices: dict[str, int] = {}
            index_to_id: dict[int, str] = {}

            for root_id in root_str_ids:
                if root_id not in node_indices:
                    idx: int = graph.add_node(root_id)
                    node_indices[root_id] = idx
                    index_to_id[idx] = root_id

            for rel in records:
                src = str(rel["source_id"])
                tgt = str(rel["target_id"])
                if src not in node_indices:
                    idx = graph.add_node(src)
                    node_indices[src] = idx
                    index_to_id[idx] = src
                if tgt not in node_indices:
                    idx = graph.add_node(tgt)
                    node_indices[tgt] = idx
                    index_to_id[idx] = tgt

                edge_data: dict[str, Any] = {
                    "relationship_type": rel.get("type", "CORRELATES_WITH"),
                    "weight": float(rel.get("confidence", 1.0)),
                    "metadata": rel.get("metadata", {}),
                }
                graph.add_edge(node_indices[src], node_indices[tgt], edge_data)

            visited_nodes: set[int] = set()
            frontier: set[int] = {node_indices[rid] for rid in root_str_ids if rid in node_indices}

            current_depth = 0
            while frontier and current_depth < max_depth:
                next_frontier: set[int] = set()
                for parent in frontier:
                    neighbors: Any = graph.neighbors(parent)
                    for neighbor in neighbors:
                        if neighbor not in visited_nodes:
                            if len(visited_nodes) >= self._max_node_limit:
                                logger.warning("Traversal node limit reached", extra={"limit": self._max_node_limit})
                                break
                            visited_nodes.add(neighbor)
                            next_frontier.add(neighbor)
                frontier = next_frontier
                current_depth += 1

            result_nodes: list[CorrelationNode] = []
            for node_idx in visited_nodes:
                node_id_str = index_to_id[node_idx]
                result_nodes.append(
                    CorrelationNode(
                        id=UUID(node_id_str),
                        label=node_id_str,
                        node_type="EVIDENCE",
                        metadata={"traversed_depth": current_depth},
                    )
                )

            result_edges: list[CorrelationEdge] = []
            for edge_idx in graph.edge_indices():
                src_idx, tgt_idx = graph.get_edge_endpoints_by_index(edge_idx)
                if src_idx in visited_nodes and tgt_idx in visited_nodes:
                    edge_data = graph.get_edge_data_by_index(edge_idx)
                    result_edges.append(
                        CorrelationEdge(
                            source_id=UUID(index_to_id[src_idx]),
                            target_id=UUID(index_to_id[tgt_idx]),
                            relationship_type=edge_data["relationship_type"],
                            weight=edge_data["weight"],
                            metadata=edge_data["metadata"],
                        )
                    )

            span.set_attribute("total_traversed_nodes", len(result_nodes))
            return CorrelationGraph(
                nodes=result_nodes,
                edges=result_edges,
                root_node_ids=root_evidence_ids,
            )
