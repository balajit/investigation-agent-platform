# src/investigation_agent_platform/infrastructure/topology/in_memory.py
"""In-memory topology adapter (development/test profile only).

Implements ``TopologyIngestionPort``, ``CodeTopologyRepository``,
``DomainAttributionPort``, and ``RepositoryRegistryPort`` using the same
tenant/repository/revision-qualified keys the Neo4j adapter uses, so the
application-layer contracts are proven before a Neo4j deployment is required
(prompt1_v1.md "In-Memory Adapter").
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from investigation_agent_platform.domain.common.exceptions import (
    SecurityPolicyViolationException,
    TopologyAmbiguousMatchError,
    TopologyNotConfiguredError,
    TopologySnapshotNotReadyError,
)
from investigation_agent_platform.domain.topology.models import (
    ASTNodeIdentity,
    ASTTopologyPayload,
    AttributionFallbackLevel,
    GitOrganizationIdentity,
    RepositoryIdentity,
    RepositoryType,
    SnapshotCollectionResult,
    SnapshotDescriptor,
    StaticOwnershipResult,
    TopologyIngestionResult,
    TopologySnapshotStatus,
)

logger = logging.getLogger(__name__)


class InMemoryTopologyAdapter:
    """Process-local adapter implementing every Layer 3 topology port.

    Not safe across replicas or process restarts. Must only be constructed
    in explicit development/test profiles (see D2 / prompt1_v1.md).
    """

    def __init__(self) -> None:
        # (tenant_id, repository_id, revision) -> snapshot metadata
        self._snapshots: dict[tuple[str, str, str], dict[str, Any]] = {}
        # (tenant_id, repository_id, revision) -> list[ASTNodeIdentity]
        self._nodes: dict[tuple[str, str, str], list[ASTNodeIdentity]] = {}
        # (tenant_id, application_id) -> RepositoryIdentity
        self._repositories: dict[tuple[str, str], RepositoryIdentity] = {}
        # (tenant_id, repository_id) -> RepositoryIdentity (secondary index)
        self._repositories_by_id: dict[tuple[str, str], RepositoryIdentity] = {}
        # (tenant_id, service_name) -> RepositoryIdentity (ISSUE-5)
        self._repositories_by_service: dict[tuple[str, str], RepositoryIdentity] = {}
        # tenant_id -> domain_id -> ownership path (git_org_id, domain_id)
        self._ownership: dict[tuple[str, str], list[str]] = {}

    # -- TopologyIngestionPort ------------------------------------------------

    async def ingest(self, payload: ASTTopologyPayload) -> TopologyIngestionResult:
        key = (payload.tenant_id, payload.repository_id, payload.revision)
        existing = self._snapshots.get(key)
        if (
            existing is not None
            and existing.get("payload_hash") == payload.payload_hash
            and existing.get("status") == TopologySnapshotStatus.READY
        ):
            # Idempotent replay: identical payload already ingested.
            logger.info(
                "Topology ingestion idempotent replay",
                extra={
                    "tenant_id": payload.tenant_id,
                    "repository_id": payload.repository_id,
                    "revision": payload.revision,
                },
            )
            return TopologyIngestionResult(
                tenant_id=payload.tenant_id,
                repository_id=payload.repository_id,
                revision=payload.revision,
                snapshot_id=existing["snapshot_id"],
                status=TopologySnapshotStatus.READY,
                node_count=len(self._nodes.get(key, [])),
                edge_count=existing.get("edge_count", 0),
            )

        # Snapshot lifecycle: PENDING -> INGESTING -> READY|FAILED. A failed
        # or partial ingestion never becomes queryable.
        self._snapshots[key] = {
            "snapshot_id": payload.snapshot_id,
            "status": TopologySnapshotStatus.INGESTING,
            "payload_hash": payload.payload_hash,
            "edge_count": len(payload.calls) + len(payload.references),
        }
        try:
            # ISSUE-3: persist macro-tier nodes only; micro detail is
            # resolved on demand at query time and must never inflate the graph.
            macro_nodes = [n for n in payload.ast_nodes if n.granularity == "macro"]
            micro_skipped = len(payload.ast_nodes) - len(macro_nodes)
            macro_ids = {n.node_id for n in macro_nodes}
            full_ids = {n.node_id for n in payload.ast_nodes}
            kept_calls = []
            for call in payload.calls:
                if call.caller_node_id not in macro_ids:
                    if call.caller_node_id not in full_ids:
                        raise ValueError(f"dangling caller node {call.caller_node_id}")
                    # Caller is a micro node: skip the edge rather than
                    # failing ingestion (micro edges are not persisted).
                    micro_skipped += 1
                    continue
                kept_calls.append(call)
            self._nodes[key] = macro_nodes
            self._snapshots[key]["status"] = TopologySnapshotStatus.READY
            self._snapshots[key]["edge_count"] = len(kept_calls) + len(payload.references)
            self._snapshots[key]["ingested_at"] = datetime.now(UTC)
        except Exception as exc:
            self._snapshots[key]["status"] = TopologySnapshotStatus.FAILED
            self._snapshots[key]["error_summary"] = str(exc)[:2000]
            logger.exception("Topology ingestion failed", extra={"tenant_id": payload.tenant_id})
            return TopologyIngestionResult(
                tenant_id=payload.tenant_id,
                repository_id=payload.repository_id,
                revision=payload.revision,
                snapshot_id=payload.snapshot_id,
                status=TopologySnapshotStatus.FAILED,
                error_summary=str(exc)[:2000],
            )

        return TopologyIngestionResult(
            tenant_id=payload.tenant_id,
            repository_id=payload.repository_id,
            revision=payload.revision,
            snapshot_id=payload.snapshot_id,
            status=TopologySnapshotStatus.READY,
            node_count=len(macro_nodes),
            edge_count=len(kept_calls) + len(payload.references),
            micro_skipped_count=micro_skipped,
        )

    # -- CodeTopologyRepository -------------------------------------------------

    async def get_snapshot_status(
        self, tenant_id: str, repository_id: str, revision: str
    ) -> TopologySnapshotStatus | None:
        entry = self._snapshots.get((tenant_id, repository_id, revision))
        return entry["status"] if entry else None

    async def supersede_snapshot(self, tenant_id: str, repository_id: str, revision: str) -> None:
        entry = self._snapshots.get((tenant_id, repository_id, revision))
        if entry is not None:
            entry["status"] = TopologySnapshotStatus.SUPERSEDED

    # -- SnapshotRetentionPort (ISSUE-4) --------------------------------------

    async def list_snapshots(self, tenant_id: str, repository_id: str) -> list[SnapshotDescriptor]:
        descriptors = []
        for (t, r, revision), entry in self._snapshots.items():
            if t != tenant_id or r != repository_id:
                continue
            descriptors.append(
                SnapshotDescriptor(
                    tenant_id=tenant_id,
                    repository_id=repository_id,
                    revision=revision,
                    status=entry["status"],
                    ingested_at=entry.get("ingested_at", datetime.now(UTC)),
                )
            )
        return descriptors

    async def collect_snapshot(
        self, tenant_id: str, repository_id: str, revision: str
    ) -> SnapshotCollectionResult:
        key = (tenant_id, repository_id, revision)
        entry = self._snapshots.get(key)
        if entry is None:
            raise TopologyNotConfiguredError(
                f"No topology snapshot for repository={repository_id} revision={revision}",
                details={"tenant_id": tenant_id, "repository_id": repository_id},
            )
        if entry["status"] == TopologySnapshotStatus.COLLECTED:
            return SnapshotCollectionResult(
                tenant_id=tenant_id,
                repository_id=repository_id,
                revision=revision,
                already_collected=True,
            )
        deleted_nodes = self._nodes.pop(key, [])
        entry["status"] = TopologySnapshotStatus.COLLECTED
        return SnapshotCollectionResult(
            tenant_id=tenant_id,
            repository_id=repository_id,
            revision=revision,
            deleted_node_count=len(deleted_nodes),
            deleted_edge_count=entry.get("edge_count", 0),
        )

    # -- DomainAttributionPort --------------------------------------------------

    async def resolve_source_location(
        self,
        tenant_id: str,
        application_id: str,
        investigation_id: UUID,
        repository_id: str,
        revision: str,
        file_path: str,
        line_number: int,
    ) -> StaticOwnershipResult:
        key = (tenant_id, repository_id, revision)
        status = self._snapshots.get(key, {}).get("status")
        if status is None:
            raise TopologyNotConfiguredError(
                f"No topology snapshot for repository={repository_id} revision={revision}",
                details={"tenant_id": tenant_id, "repository_id": repository_id},
            )
        if status != TopologySnapshotStatus.READY:
            raise TopologySnapshotNotReadyError(
                f"Snapshot status is {status}, not READY",
                retryable=status == TopologySnapshotStatus.INGESTING,
                details={"status": str(status)},
            )

        candidates = [
            n
            for n in self._nodes.get(key, [])
            if n.tenant_id == tenant_id
            and n.file_path == file_path
            and n.start_line <= line_number <= n.end_line
        ]
        if not candidates:
            repo = self._repositories_by_id.get((tenant_id, repository_id))
            ownership_path = self._ownership.get((tenant_id, repository_id), [])
            return StaticOwnershipResult(
                tenant_id=tenant_id,
                repository_id=repository_id,
                revision=revision,
                snapshot_id=self._snapshots[key]["snapshot_id"],
                matched_file_path=file_path,
                repository_type=repo.repository_type if repo else RepositoryType.UNKNOWN,
                domain_id=ownership_path[1] if len(ownership_path) > 1 else None,
                git_org_id=ownership_path[0] if ownership_path else None,
                ownership_path=ownership_path,
                fallback_level=AttributionFallbackLevel.REPOSITORY,
            )

        # Most specific enclosing symbol: smallest source range ascending,
        # nesting depth (approximated by range size again) descending, then
        # stable node_id ascending for a fully deterministic tie-break.
        candidates.sort(key=lambda n: (n.source_range_size, str(n.node_id)))
        smallest = candidates[0].source_range_size
        tied = [c for c in candidates if c.source_range_size == smallest]
        if len(tied) > 1 and len({c.node_id for c in tied}) > 1:
            raise TopologyAmbiguousMatchError(
                f"{len(tied)} equally specific nodes match {file_path}:{line_number}",
                details={"candidates": [str(c.node_id) for c in tied]},
            )
        matched = tied[0]

        repo = self._repositories_by_id.get((tenant_id, repository_id))
        ownership_path = self._ownership.get((tenant_id, repository_id), [])
        return StaticOwnershipResult(
            tenant_id=tenant_id,
            repository_id=repository_id,
            revision=revision,
            snapshot_id=self._snapshots[key]["snapshot_id"],
            matched_node_id=matched.node_id,
            matched_node_qualified_name=matched.qualified_name,
            matched_file_path=matched.file_path,
            matched_start_line=matched.start_line,
            matched_end_line=matched.end_line,
            repository_type=repo.repository_type if repo else RepositoryType.UNKNOWN,
            domain_id=ownership_path[1] if len(ownership_path) > 1 else None,
            git_org_id=ownership_path[0] if ownership_path else None,
            ownership_path=ownership_path,
            fallback_level=AttributionFallbackLevel.AST_NODE,
            node_type=matched.node_type,
        )

    # -- RepositoryRegistryPort ---------------------------------------------

    async def resolve_for_application(
        self, tenant_id: str, application_id: str
    ) -> RepositoryIdentity | None:
        return self._repositories.get((tenant_id, application_id))

    async def register_repository(self, repository: RepositoryIdentity) -> None:
        # Dev/test helper: registration is not tenant-scoped by application_id
        # here since RepositoryIdentity itself carries no application_id;
        # callers register the application mapping separately via
        # ``register_application_repository``.
        self._repositories_by_id[(repository.tenant_id, repository.repository_id)] = repository

    async def resolve_for_service_name(
        self, tenant_id: str, service_name: str
    ) -> RepositoryIdentity | None:
        return self._repositories_by_service.get((tenant_id, service_name))

    # -- OwnershipRegistryPort (ISSUE-7) --------------------------------------

    async def register_ownership(
        self,
        repository: RepositoryIdentity,
        git_org: GitOrganizationIdentity | None,
        domain_id: str | None,
    ) -> None:
        """Mirrors ``Neo4jTopologyAdapter.register_ownership`` for parity:
        registers the repository and, when resolvable, records the
        git-org/domain ownership path."""
        self._repositories_by_id[(repository.tenant_id, repository.repository_id)] = repository
        if git_org is None:
            return
        path = [git_org.git_org_id]
        if domain_id:
            path.append(domain_id)
        self._ownership[(repository.tenant_id, repository.repository_id)] = path

    # -- Test/dev helpers (not part of any port) -----------------------------

    def register_application_repository(
        self, tenant_id: str, application_id: str, repository: RepositoryIdentity
    ) -> None:
        if repository.tenant_id != tenant_id:
            raise SecurityPolicyViolationException(
                "Repository tenant does not match requested tenant",
                details={"tenant_id": tenant_id},
            )
        self._repositories[(tenant_id, application_id)] = repository
        self._repositories_by_id[(tenant_id, repository.repository_id)] = repository

    def register_service_repository(
        self, tenant_id: str, service_name: str, repository: RepositoryIdentity
    ) -> None:
        """ISSUE-5 test/dev helper: seeds the service-name -> repository
        mapping ``resolve_for_service_name`` reads."""
        if repository.tenant_id != tenant_id:
            raise SecurityPolicyViolationException(
                "Repository tenant does not match requested tenant",
                details={"tenant_id": tenant_id},
            )
        self._repositories_by_service[(tenant_id, service_name)] = repository
        self._repositories_by_id[(tenant_id, repository.repository_id)] = repository

    def set_ownership_path(self, tenant_id: str, repository_id: str, path: list[str]) -> None:
        self._ownership[(tenant_id, repository_id)] = path
