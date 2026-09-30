# src/investigation_agent_platform/infrastructure/persistence/finding_cluster_repository.py
"""SQLAlchemy finding-cluster repository (Part 11.7)."""

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from investigation_agent_platform.domain.common.exceptions import ConcurrencyError
from investigation_agent_platform.domain.finding.clustering import (
    UNASSIGNED_CLUSTER_ID,
    FindingCluster,
    FindingClusterAssignment,
    FindingEmbedding,
)
from investigation_agent_platform.infrastructure.persistence.models import (
    FindingClusterAssignmentORM,
    FindingClusterORM,
    FindingEmbeddingORM,
)
from investigation_agent_platform.infrastructure.persistence.rls import rls_session


def _lexical_text(label: str, description: str) -> str:
    return f"{label} {description}".strip()


class SqlAlchemyFindingClusterRepository:
    """PostgreSQL-backed FindingClusterRepository with strict tenant scoping."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    # -- mapping ---------------------------------------------------------

    @staticmethod
    def _cluster_from_orm(row: FindingClusterORM) -> FindingCluster:
        return FindingCluster(
            id=row.id,
            tenant_id=row.tenant_id,
            cluster_key=row.cluster_key,
            label=row.label,
            description=row.description,
            taxonomy_revision=row.taxonomy_revision,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _assignment_from_orm(row: FindingClusterAssignmentORM) -> FindingClusterAssignment:
        return FindingClusterAssignment(
            id=row.id,
            tenant_id=row.tenant_id,
            finding_id=row.finding_id,
            cluster_id=row.cluster_id,
            method=row.method,
            confidence=row.confidence,
            taxonomy_revision=row.taxonomy_revision,
            valid_from=row.valid_from,
            valid_to=row.valid_to,
            provenance=None,
        )

    # -- taxonomy --------------------------------------------------------

    async def current_revision(self, tenant_id: str) -> int:
        async with rls_session(self._session_factory, tenant_id) as session:
            revision = await session.scalar(
                select(func.max(FindingClusterORM.taxonomy_revision)).where(
                    FindingClusterORM.tenant_id == tenant_id
                )
            )
            return int(revision or 0)

    async def load_taxonomy(self, tenant_id: str) -> list[FindingCluster]:
        revision = await self.current_revision(tenant_id)
        if revision == 0:
            return []
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(FindingClusterORM)
                .where(
                    FindingClusterORM.tenant_id == tenant_id,
                    FindingClusterORM.taxonomy_revision == revision,
                )
                .order_by(FindingClusterORM.cluster_key)
            )
            return [self._cluster_from_orm(row) for row in result.all()]

    async def create_cluster(self, tenant_id: str, cluster: FindingCluster) -> None:
        if cluster.tenant_id != tenant_id:
            raise ConcurrencyError("Cluster tenant mismatch")
        if cluster.cluster_key == UNASSIGNED_CLUSTER_ID:
            raise ConcurrencyError("UNASSIGNED is a bucket, not a taxonomy row")
        async with rls_session(self._session_factory, tenant_id) as session:
            stmt = (
                pg_insert(FindingClusterORM)
                .values(
                    id=cluster.id,
                    tenant_id=tenant_id,
                    cluster_key=cluster.cluster_key,
                    label=cluster.label,
                    description=cluster.description,
                    taxonomy_revision=cluster.taxonomy_revision,
                    created_at=cluster.created_at,
                    updated_at=cluster.updated_at,
                    lexical_tsv=func.to_tsvector(
                        "english", _lexical_text(cluster.label, cluster.description)
                    ),
                )
                .on_conflict_do_nothing(constraint="uq_cluster_tenant_key_revision")
            )
            await session.execute(stmt)

    # -- assignments -----------------------------------------------------

    async def assign_finding(self, tenant_id: str, assignment: FindingClusterAssignment) -> None:
        if assignment.tenant_id != tenant_id:
            raise ConcurrencyError("Assignment tenant mismatch")
        async with rls_session(self._session_factory, tenant_id) as session:
            open_rows = await session.scalars(
                select(FindingClusterAssignmentORM).where(
                    FindingClusterAssignmentORM.tenant_id == tenant_id,
                    FindingClusterAssignmentORM.finding_id == assignment.finding_id,
                    FindingClusterAssignmentORM.valid_to.is_(None),
                )
            )
            for row in open_rows.all():
                row.valid_to = assignment.valid_from
            session.add(
                FindingClusterAssignmentORM(
                    id=assignment.id,
                    tenant_id=tenant_id,
                    finding_id=assignment.finding_id,
                    cluster_id=assignment.cluster_id,
                    method=assignment.method,
                    confidence=assignment.confidence,
                    taxonomy_revision=assignment.taxonomy_revision,
                    valid_from=assignment.valid_from,
                    valid_to=assignment.valid_to,
                    provenance_json=(
                        assignment.provenance.model_dump(mode="json")
                        if assignment.provenance is not None
                        else None
                    ),
                )
            )

    async def assigned_finding_ids(self, tenant_id: str, finding_ids: list[UUID]) -> set[UUID]:
        if not finding_ids:
            return set()
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(FindingClusterAssignmentORM.finding_id).where(
                    FindingClusterAssignmentORM.tenant_id == tenant_id,
                    FindingClusterAssignmentORM.finding_id.in_(finding_ids),
                    FindingClusterAssignmentORM.valid_to.is_(None),
                )
            )
            return set(result.all())

    async def assignments_for_finding(
        self, tenant_id: str, finding_id: UUID
    ) -> list[FindingClusterAssignment]:
        async with rls_session(self._session_factory, tenant_id) as session:
            result = await session.scalars(
                select(FindingClusterAssignmentORM)
                .where(
                    FindingClusterAssignmentORM.tenant_id == tenant_id,
                    FindingClusterAssignmentORM.finding_id == finding_id,
                )
                .order_by(FindingClusterAssignmentORM.valid_from.desc())
            )
            return [self._assignment_from_orm(row) for row in result.all()]

    async def cluster_assignments(
        self,
        tenant_id: str,
        cluster_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[FindingClusterAssignment], int]:
        async with rls_session(self._session_factory, tenant_id) as session:
            filters = [
                FindingClusterAssignmentORM.tenant_id == tenant_id,
                FindingClusterAssignmentORM.cluster_id == cluster_id,
                FindingClusterAssignmentORM.valid_to.is_(None),
            ]
            total = await session.scalar(
                select(func.count()).select_from(FindingClusterAssignmentORM).where(*filters)
            )
            result = await session.scalars(
                select(FindingClusterAssignmentORM)
                .where(*filters)
                .order_by(FindingClusterAssignmentORM.valid_from.desc())
                .offset(offset)
                .limit(limit)
            )
            return [self._assignment_from_orm(row) for row in result.all()], int(total or 0)

    # -- candidates + embeddings ------------------------------------------

    async def find_candidate_clusters(
        self, tenant_id: str, query_text: str, limit: int = 12
    ) -> list[FindingCluster]:
        """Lexical candidates over the current taxonomy (ts_rank, bounded)."""
        revision = await self.current_revision(tenant_id)
        if revision == 0 or not query_text.strip():
            return []
        async with rls_session(self._session_factory, tenant_id) as session:
            query = func.plainto_tsquery("english", query_text[:500])
            result = await session.scalars(
                select(FindingClusterORM)
                .where(
                    FindingClusterORM.tenant_id == tenant_id,
                    FindingClusterORM.taxonomy_revision == revision,
                    FindingClusterORM.lexical_tsv.op("@@")(query),
                )
                .order_by(func.ts_rank(FindingClusterORM.lexical_tsv, query).desc())
                .limit(max(1, min(limit, 60)))
            )
            return [self._cluster_from_orm(row) for row in result.all()]

    async def upsert_finding_embedding(self, tenant_id: str, embedding: FindingEmbedding) -> None:
        """Idempotent write for one (finding, model, version, generation) space."""
        if embedding.tenant_id != tenant_id:
            raise ConcurrencyError("Embedding tenant mismatch")
        lexical = embedding.lexical_text[:4000]
        async with rls_session(self._session_factory, tenant_id) as session:
            stmt = (
                pg_insert(FindingEmbeddingORM)
                .values(
                    tenant_id=tenant_id,
                    finding_id=embedding.finding_id,
                    embedding_model=embedding.embedding_model,
                    embedding_version=embedding.embedding_version,
                    generation=embedding.generation,
                    embedding=embedding.vector or None,
                    lexical=lexical or None,
                    lexical_tsv=func.to_tsvector("english", lexical),
                    lifecycle=embedding.lifecycle,
                    provenance_json=(
                        embedding.provenance.model_dump(mode="json")
                        if embedding.provenance is not None
                        else None
                    ),
                )
                .on_conflict_do_update(
                    constraint="uq_finding_embedding_space",
                    set_={
                        "embedding": embedding.vector or None,
                        "lifecycle": embedding.lifecycle,
                    },
                )
            )
            await session.execute(stmt)

    async def active_generation(
        self, tenant_id: str, embedding_model: str, embedding_version: str
    ) -> int:
        """Newest generation with rows = the read generation (blue/green)."""
        async with rls_session(self._session_factory, tenant_id) as session:
            generation = await session.scalar(
                select(func.max(FindingEmbeddingORM.generation)).where(
                    FindingEmbeddingORM.tenant_id == tenant_id,
                    FindingEmbeddingORM.embedding_model == embedding_model,
                    FindingEmbeddingORM.embedding_version == embedding_version,
                    FindingEmbeddingORM.lifecycle == "ACTIVE",
                )
            )
            return int(generation or 0)

    async def find_similar_assigned_clusters(
        self, tenant_id: str, query_text: str, limit: int = 12
    ) -> list[UUID]:
        """Cluster ids whose ACTIVE-generation member findings lexically match.

        Collaborative signal: findings described like this one were placed in
        these clusters. Vector rerank over these candidates happens in the
        service when an embedder is configured.
        """
        if not query_text.strip():
            return []
        async with rls_session(self._session_factory, tenant_id) as session:
            query = func.plainto_tsquery("english", query_text[:500])
            result = await session.scalars(
                select(FindingClusterAssignmentORM.cluster_id)
                .join(
                    FindingEmbeddingORM,
                    FindingEmbeddingORM.finding_id == FindingClusterAssignmentORM.finding_id,
                )
                .where(
                    FindingClusterAssignmentORM.tenant_id == tenant_id,
                    FindingEmbeddingORM.tenant_id == tenant_id,
                    FindingClusterAssignmentORM.valid_to.is_(None),
                    FindingClusterAssignmentORM.cluster_id.is_not(None),
                    FindingEmbeddingORM.lifecycle == "ACTIVE",
                    FindingEmbeddingORM.lexical_tsv.op("@@")(query),
                )
                .order_by(func.ts_rank(FindingEmbeddingORM.lexical_tsv, query).desc())
                .limit(max(1, min(limit, 60)))
            )
            seen: list[UUID] = []
            for cluster_id in result.all():
                if cluster_id is not None and cluster_id not in seen:
                    seen.append(cluster_id)
            return seen
