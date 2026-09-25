# src/investigation_agent_platform/infrastructure/correlation/repository.py
"""PostgreSQL correlation relationship repository for evidence graph traversal."""

import logging
from typing import Any
from uuid import UUID

from opentelemetry import trace
from sqlalchemy import ARRAY, bindparam, text
from sqlalchemy import UUID as PostgresUUID

from investigation_agent_platform.domain.common.exceptions import ExecutionError

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

MAX_ALLOWED_RECURSION_DEPTH = 5
MAX_RESULT_NODES_LIMIT = 1000


class PostgresRelationshipRepository:
    """PostgreSQL implementation for fetching relationship vectors to hydrate evidence graphs."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    async def fetch_relationships_for_evidence(
        self,
        tenant_id: str,
        application_id: str,
        root_evidence_ids: list[str],
        max_depth: int,
    ) -> list[dict[str, Any]]:
        """Fetch directed and bidirectional evidence relationship edges up to a bounded recursion depth."""
        with tracer.start_as_current_span(
            "PostgresRelationshipRepository.fetch_relationships_for_evidence"
        ) as span:
            span.set_attribute("tenant_id", tenant_id)
            span.set_attribute("application_id", application_id)
            span.set_attribute("root_count", len(root_evidence_ids))

            if not root_evidence_ids:
                return []

            bounded_depth = max(1, min(max_depth, MAX_ALLOWED_RECURSION_DEPTH))
            span.set_attribute("bounded_depth", bounded_depth)

            try:
                validated_root_uuids = [UUID(rid) for rid in root_evidence_ids]
            except ValueError as exc:
                logger.error(
                    "Invalid UUID string provided in root_evidence_ids",
                    extra={"context": {"tenant_id": tenant_id, "application_id": application_id}},
                    exc_info=exc,
                )
                raise ExecutionError(f"Invalid evidence UUID in root list: {exc}") from exc

            sql = text(
                """
                WITH RECURSIVE relationship_tree AS (
                    SELECT 
                        source_node_id, 
                        target_node_id, 
                        relationship_type, 
                        confidence, 
                        1 AS depth,
                        ARRAY[source_node_id, target_node_id] AS visited_path
                    FROM evidence_relationships
                    WHERE tenant_id = :tenant_id 
                      AND application_id = :application_id
                      AND (source_node_id = ANY(:root_ids) OR target_node_id = ANY(:root_ids))

                    UNION ALL

                    SELECT 
                        r.source_node_id, 
                        r.target_node_id, 
                        r.relationship_type, 
                        r.confidence, 
                        rt.depth + 1,
                        rt.visited_path || r.target_node_id
                    FROM evidence_relationships r
                    INNER JOIN relationship_tree rt 
                        ON (r.source_node_id = rt.target_node_id OR r.target_node_id = rt.source_node_id)
                    WHERE r.tenant_id = :tenant_id 
                      AND r.application_id = :application_id
                      AND rt.depth < :max_depth
                      AND NOT (r.target_node_id = ANY(rt.visited_path))
                )
                SELECT DISTINCT source_node_id, target_node_id, relationship_type, confidence 
                FROM relationship_tree
                LIMIT :node_limit;
                """
            ).bindparams(
                bindparam("root_ids", type_=ARRAY(PostgresUUID)),
            )

            try:
                async with self._session_factory() as session:
                    result = await session.execute(
                        sql,
                        {
                            "tenant_id": tenant_id,
                            "application_id": application_id,
                            "root_ids": validated_root_uuids,
                            "max_depth": bounded_depth,
                            "node_limit": MAX_RESULT_NODES_LIMIT,
                        },
                    )
                    rows = result.mappings().all()
                    span.set_attribute("result_count", len(rows))
                    return [
                        {
                            "source_id": str(row["source_node_id"]),
                            "target_id": str(row["target_node_id"]),
                            "type": str(row["relationship_type"]),
                            "confidence": float(row["confidence"]),
                        }
                        for row in rows
                    ]
            except Exception as exc:
                logger.exception(
                    "Failed to query relationships from PostgreSQL",
                    extra={
                        "context": {
                            "tenant_id": tenant_id,
                            "application_id": application_id,
                            "root_count": len(root_evidence_ids),
                        }
                    },
                )
                raise ExecutionError(
                    f"Relationship query failed for tenant {tenant_id}: {exc}"
                ) from exc
