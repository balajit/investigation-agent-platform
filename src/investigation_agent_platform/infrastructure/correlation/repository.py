# src/investigation_agent_platform/infrastructure/correlation/repository.py
"""PostgreSQL correlation relationship repository for evidence graph traversal."""

import logging
from typing import Any
from uuid import UUID

from opentelemetry import trace
from sqlalchemy import ARRAY, Text, bindparam, text

from investigation_agent_platform.domain.common.exceptions import ExecutionError
from investigation_agent_platform.ports.persistence.repositories import (
    EvidenceRelationshipRecord,
)

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
    ) -> list[EvidenceRelationshipRecord]:
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

            # Application scoping joins investigations: evidence_relationships
            # carries (tenant_id, investigation_id) but no application_id
            # column, so the app boundary resolves through the parent
            # investigation row. Node IDs are stored as text; bind roots as
            # text[] (uuid[] would fail the varchar comparison).
            sql = text(
                """
                WITH RECURSIVE relationship_tree AS (
                    SELECT
                        r.source_node_id,
                        r.target_node_id,
                        r.relationship_type,
                        r.confidence,
                        1 AS depth,
                        ARRAY[r.source_node_id, r.target_node_id]::text[] AS visited_path
                    FROM evidence_relationships r
                    JOIN investigations i ON i.id = r.investigation_id
                    WHERE r.tenant_id = :tenant_id
                      AND i.application_id = :application_id
                      AND (r.source_node_id = ANY(:root_ids) OR r.target_node_id = ANY(:root_ids))

                    UNION ALL

                    SELECT
                        r.source_node_id,
                        r.target_node_id,
                        r.relationship_type,
                        r.confidence,
                        rt.depth + 1,
                        rt.visited_path || r.target_node_id::text
                    FROM evidence_relationships r
                    JOIN investigations i ON i.id = r.investigation_id
                    INNER JOIN relationship_tree rt
                        ON (r.source_node_id = rt.target_node_id OR r.target_node_id = rt.source_node_id)
                    WHERE r.tenant_id = :tenant_id
                      AND i.application_id = :application_id
                      AND rt.depth < :max_depth
                      AND NOT (r.target_node_id = ANY(rt.visited_path))
                )
                SELECT DISTINCT source_node_id, target_node_id, relationship_type, confidence
                FROM relationship_tree
                LIMIT :node_limit;
                """
            ).bindparams(
                bindparam("root_ids", type_=ARRAY(Text)),
            )

            try:
                async with self._session_factory() as session:
                    result = await session.execute(
                        sql,
                        {
                            "tenant_id": tenant_id,
                            "application_id": application_id,
                            "root_ids": [str(u) for u in validated_root_uuids],
                            "max_depth": bounded_depth,
                            "node_limit": MAX_RESULT_NODES_LIMIT,
                        },
                    )
                    rows = result.mappings().all()
                    span.set_attribute("result_count", len(rows))
                    return [
                        EvidenceRelationshipRecord(
                            tenant_id=tenant_id,
                            application_id=application_id,
                            source_id=str(row["source_node_id"]),
                            target_id=str(row["target_node_id"]),
                            type=str(row["relationship_type"]),
                            confidence=float(row["confidence"]),
                        )
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

    async def record_edges(
        self,
        tenant_id: str,
        investigation_id: Any,
        application_id: str,
        edges: list[EvidenceRelationshipRecord],
    ) -> int:
        """Persist correlation edges idempotently (first producer for this table).

        Skips edges already present for (tenant, investigation, source,
        target, type) so replays and re-concludes never duplicate rows.
        Returns the inserted count.
        """
        from sqlalchemy import select

        from investigation_agent_platform.infrastructure.persistence.models import (
            EvidenceRelationshipORM,
        )

        pending = [
            e for e in edges if e.tenant_id == tenant_id and e.application_id == application_id
        ]
        if not pending:
            return 0
        async with self._session_factory() as session:
            existing = await session.scalars(
                select(
                    EvidenceRelationshipORM.source_node_id,
                    EvidenceRelationshipORM.target_node_id,
                    EvidenceRelationshipORM.relationship_type,
                ).where(
                    EvidenceRelationshipORM.tenant_id == tenant_id,
                    EvidenceRelationshipORM.investigation_id == investigation_id,
                )
            )
            seen = {(r[0], r[1], r[2]) for r in existing.all()}
            fresh = [e for e in pending if (e.source_id, e.target_id, e.type) not in seen]
            for edge in fresh:
                session.add(
                    EvidenceRelationshipORM(
                        tenant_id=tenant_id,
                        investigation_id=investigation_id,
                        source_node_id=edge.source_id,
                        target_node_id=edge.target_id,
                        relationship_type=edge.type,
                        confidence=edge.confidence,
                        reason=str((edge.metadata or {}).get("reason", "")),
                        provenance_evidence_id=str(
                            (edge.metadata or {}).get("provenance_evidence_id", "")
                        ),
                    )
                )
            await session.commit()
            return len(fresh)

    async def fetch_links_for_node(self, tenant_id: str, node_id: str) -> list[dict[str, Any]]:
        """§2.4: inbound + outbound investigation-level links for one node.

        Returns plain dicts (source, target, type, confidence, reason,
        investigation_id) for rows where the node appears on either side.
        Tenant-scoped; no application filter (families may span profiles).
        """
        from sqlalchemy import or_, select

        from investigation_agent_platform.infrastructure.persistence.models import (
            EvidenceRelationshipORM,
        )

        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(EvidenceRelationshipORM)
                    .where(
                        EvidenceRelationshipORM.tenant_id == tenant_id,
                        or_(
                            EvidenceRelationshipORM.source_node_id == node_id,
                            EvidenceRelationshipORM.target_node_id == node_id,
                        ),
                    )
                    .order_by(EvidenceRelationshipORM.created_at)
                )
            ).all()
            return [
                {
                    "source": r.source_node_id,
                    "target": r.target_node_id,
                    "type": r.relationship_type,
                    "confidence": float(r.confidence),
                    "reason": r.reason,
                    "investigation_id": str(r.investigation_id),
                }
                for r in rows
            ]
