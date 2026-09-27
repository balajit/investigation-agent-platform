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
        confidence_threshold: float = 0.20,
    ) -> None:
        self._relationship_repo = evidence_relationship_repo
        self._evidence_repo = evidence_repository
        self._max_node_limit = max_node_limit
        self.confidence_threshold = confidence_threshold

    async def expand_correlation(
        self,
        tenant_id: str,
        application_id: str,
        root_evidence_ids: list[UUID],
        max_depth: int,
        max_nodes: int | None = None,
        max_edges: int | None = None,
    ) -> CorrelationGraph:
        """Hydrates relationships from persistence and performs fast rustworkx graph traversal.

        F-037: every relationship record is validated for tenant/application
        ownership before it enters the graph — inconsistent records are
        quarantined (counted, logged, skipped), never traversed.
        F-038: per-call budget overrides (max_nodes/max_edges/max_depth) let
        callers propagate investigation-specific limits instead of relying on
        engine-level defaults.
        """
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
            # F-038: effective budget is the tightest of caller-supplied and engine defaults.
            node_cap = (
                min(max_nodes, self._max_node_limit)
                if max_nodes is not None
                else self._max_node_limit
            )
            edge_cap = max_edges if max_edges is not None else self._max_node_limit * 4
            depth_cap = max(0, max_depth)
            records = await self._relationship_repo.fetch_relationships_for_evidence(
                tenant_id=tenant_id,
                application_id=application_id,
                root_evidence_ids=root_str_ids,
                max_depth=depth_cap,
            )

            graph: Any = rx.PyDiGraph(multigraph=False)
            node_indices: dict[str, int] = {}
            index_to_id: dict[int, str] = {}
            quarantined = 0

            for root_id in root_str_ids:
                if root_id not in node_indices:
                    idx: int = graph.add_node(root_id)
                    node_indices[root_id] = idx
                    index_to_id[idx] = root_id

            edge_count = 0
            for rel in records:
                # F-037: provenance/ownership check before graph construction.
                rel_tenant = rel.get("tenant_id", tenant_id)
                rel_app = rel.get("application_id", application_id)
                if rel_tenant != tenant_id or rel_app != application_id:
                    quarantined += 1
                    continue
                try:
                    confidence = float(rel.get("confidence", 1.0))
                except (TypeError, ValueError):
                    quarantined += 1
                    continue
                if not (0.0 <= confidence <= 1.0):
                    quarantined += 1
                    continue
                # Probabilistic pruning: discard hypothesis links below threshold
                if rel.get("type") == "hypothesis" and confidence < self.confidence_threshold:
                    quarantined += 1
                    continue
                src = str(rel["source_id"])
                tgt = str(rel["target_id"])
                if (
                    len(node_indices) >= node_cap
                    and src not in node_indices
                    and tgt not in node_indices
                ):
                    break
                if edge_count >= edge_cap:
                    break
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
                    "weight": confidence,
                    "metadata": rel.get("metadata", {}),
                }
                graph.add_edge(node_indices[src], node_indices[tgt], edge_data)
                edge_count += 1

            if quarantined:
                logger.warning(
                    "Quarantined inconsistent relationship records",
                    extra={"quarantined": quarantined, "tenant_id": tenant_id},
                )

            visited_nodes: set[int] = set()
            frontier: set[int] = {node_indices[rid] for rid in root_str_ids if rid in node_indices}

            current_depth = 0
            while frontier and current_depth < depth_cap:
                next_frontier: set[int] = set()
                for parent in frontier:
                    neighbors: Any = graph.neighbors(parent)
                    for neighbor in neighbors:
                        if neighbor not in visited_nodes:
                            if len(visited_nodes) >= node_cap:
                                logger.warning(
                                    "Traversal node limit reached", extra={"limit": node_cap}
                                )
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


class ProbabilisticCorrelationEngine:
    def __init__(self, confidence_threshold: float = 0.20) -> None:
        self.graph: Any = rx.PyDiGraph(multigraph=False)
        self.confidence_threshold = confidence_threshold

    def prune_low_confidence_hypotheses(self, threshold: float | None = None) -> list[int]:
        """Removes nodes and downstream edges where hypothesis confidence drops below threshold."""
        effective_threshold = threshold if threshold is not None else self.confidence_threshold
        removed_nodes = []
        for node_idx in self.graph.node_indices():
            node_data = self.graph.get_node_data(node_idx)
            if isinstance(node_data, dict) and node_data.get("type") == "hypothesis":
                if float(node_data.get("confidence", 1.0)) < effective_threshold:
                    removed_nodes.append(node_idx)

        # PyDiGraph allows batch node removal
        for idx in removed_nodes:
            self.graph.remove_node(idx)

        return removed_nodes

    async def build_correlation_graph(
        self, events: list[dict[str, Any]], hypotheses: list[dict[str, Any]]
    ) -> Any:
        """Constructs correlation graph and prunes low-confidence hypothesis branches."""
        for event in events:
            self.graph.add_node(event)

        for hypothesis in hypotheses:
            self.graph.add_node(hypothesis)

        self.prune_low_confidence_hypotheses()
        return self.graph
