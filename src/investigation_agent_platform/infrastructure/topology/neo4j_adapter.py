# src/investigation_agent_platform/infrastructure/topology/neo4j_adapter.py
"""Neo4j-backed Layer 3 topology adapter.

Implements ``TopologyIngestionPort``, ``CodeTopologyRepository``, and
``DomainAttributionPort`` using the official async Neo4j driver. Every
``MATCH``/``MERGE`` predicate starts with ``tenant_id`` (and ``repository_id``
/``revision`` where applicable) so tenant isolation is enforced at the query
level, not only in application code (D6/D7 in the design document).

Schema constraints are installed by ``install_constraints`` — an explicit
administration step, never executed implicitly on a request path.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from investigation_agent_platform.domain.common.exceptions import (
    TopologyAmbiguousMatchError,
    TopologyNotConfiguredError,
    TopologyProviderUnavailableError,
    TopologySnapshotNotReadyError,
)
from investigation_agent_platform.domain.topology.models import (
    ASTTopologyPayload,
    AttributionFallbackLevel,
    GitOrganizationIdentity,
    RepositoryIdentity,
    RepositoryType,
    SnapshotCollectionResult,
    SnapshotDescriptor,
    StaticOwnershipResult,
    TopologyIngestionResult,
    TopologyNodeType,
    TopologySnapshotStatus,
)
from investigation_agent_platform.infrastructure.configuration.config import TopologyConfig

logger = logging.getLogger(__name__)

# Recognized transient Neo4j failure classes that are safe to retry (F-056
# retry-taxonomy precedent). Everything else is treated as non-retryable.
_TRANSIENT_ERROR_CODE_PREFIXES = ("Neo.TransientError",)


def _is_transient_neo4j_error(exc: BaseException) -> bool:
    code = getattr(exc, "code", "") or ""
    return any(code.startswith(prefix) for prefix in _TRANSIENT_ERROR_CODE_PREFIXES)


class Neo4jTopologyAdapter:
    """Production topology adapter backed by Neo4j.

    Construction does not open a connection; call ``connect()`` during
    startup so failures surface as explicit readiness/health errors (F-001
    precedent: production must fail hard, not degrade silently).
    """

    def __init__(self, config: TopologyConfig) -> None:
        self._config = config
        self._driver: Any | None = None

    async def connect(self) -> None:
        try:
            from neo4j import AsyncGraphDatabase  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - dependency always installed in prod
            raise TopologyProviderUnavailableError("neo4j driver package is not installed") from exc

        uri = self._config.uri.get_secret_value()
        if not uri:
            raise TopologyNotConfiguredError("IAP_TOPOLOGY_NEO4J_URI is not configured")
        self._driver = AsyncGraphDatabase.driver(
            uri,
            auth=(
                self._config.username.get_secret_value(),
                self._config.password.get_secret_value(),
            ),
            max_connection_pool_size=self._config.max_connection_pool_size,
            connection_timeout=self._config.connection_timeout_seconds,
            encrypted=self._config.encrypted,
        )
        # Fail fast if the server is unreachable/misconfigured.
        await self._driver.verify_connectivity()

    async def close(self) -> None:
        if self._driver is not None:
            await self._driver.close()
            self._driver = None

    async def health_check(self) -> bool:
        if self._driver is None:
            return False
        try:
            await self._driver.verify_connectivity()
            return True
        except Exception:
            return False

    def _require_driver(self) -> Any:
        if self._driver is None:
            raise TopologyProviderUnavailableError("Neo4j driver is not connected")
        return self._driver

    async def install_constraints(self) -> None:
        """Explicit schema-administration step (never called on a request path)."""
        driver = self._require_driver()
        statements = [
            (
                "CREATE CONSTRAINT topology_domain_key IF NOT EXISTS "
                "FOR (d:Domain) REQUIRE (d.tenant_id, d.domain_id) IS UNIQUE"
            ),
            (
                "CREATE CONSTRAINT topology_gitorg_key IF NOT EXISTS "
                "FOR (g:GitOrganization) REQUIRE (g.tenant_id, g.git_org_id) IS UNIQUE"
            ),
            (
                "CREATE CONSTRAINT topology_repository_key IF NOT EXISTS "
                "FOR (r:Repository) REQUIRE (r.tenant_id, r.repository_id) IS UNIQUE"
            ),
            (
                "CREATE CONSTRAINT topology_snapshot_key IF NOT EXISTS "
                "FOR (s:TopologySnapshot) REQUIRE "
                "(s.tenant_id, s.repository_id, s.revision) IS UNIQUE"
            ),
            (
                "CREATE CONSTRAINT topology_package_key IF NOT EXISTS "
                "FOR (p:Package) REQUIRE "
                "(p.tenant_id, p.repository_id, p.revision, p.path) IS UNIQUE"
            ),
            (
                "CREATE CONSTRAINT topology_sourcefile_key IF NOT EXISTS "
                "FOR (f:SourceFile) REQUIRE "
                "(f.tenant_id, f.repository_id, f.revision, f.path) IS UNIQUE"
            ),
            (
                "CREATE CONSTRAINT topology_astnode_key IF NOT EXISTS "
                "FOR (n:ASTNode) REQUIRE "
                "(n.tenant_id, n.repository_id, n.revision, n.node_id) IS UNIQUE"
            ),
            (
                "CREATE CONSTRAINT topology_dbtable_key IF NOT EXISTS "
                "FOR (t:DatabaseTable) REQUIRE (t.tenant_id, t.table_name) IS UNIQUE"
            ),
        ]
        async with driver.session(database=self._config.database) as session:
            for stmt in statements:
                await session.run(stmt)

    # -- OwnershipRegistryPort (ISSUE-7) --------------------------------------

    async def register_ownership(
        self,
        repository: RepositoryIdentity,
        git_org: GitOrganizationIdentity | None,
        domain_id: str | None,
    ) -> None:
        """Write the Repository -> GitOrganization -> Domain chain.

        Idempotent MERGE on tenant-qualified keys. ``git_org``/``domain_id``
        may be ``None`` when unresolvable (see ``infrastructure.topology.
        ownership``); the Repository node is still registered so the
        REPOSITORY fallback tier and repository_type lookups keep working.
        """
        driver = self._require_driver()
        async with driver.session(database=self._config.database) as session:
            await session.execute_write(self._write_ownership, repository, git_org, domain_id)

    @staticmethod
    async def _write_ownership(
        tx: Any,
        repository: RepositoryIdentity,
        git_org: GitOrganizationIdentity | None,
        domain_id: str | None,
    ) -> None:
        await tx.run(
            "MERGE (r:Repository {tenant_id: $tenant_id, repository_id: $repository_id}) "
            "SET r.name = $name, r.locator = $locator, r.repository_type = $repository_type, "
            "r.default_branch = $default_branch, r.is_active = $is_active",
            tenant_id=repository.tenant_id,
            repository_id=repository.repository_id,
            name=repository.name,
            locator=repository.locator,
            repository_type=repository.repository_type.value,
            default_branch=repository.default_branch,
            is_active=repository.is_active,
        )
        if git_org is None:
            return
        await tx.run(
            "MERGE (g:GitOrganization {tenant_id: $tenant_id, git_org_id: $git_org_id}) "
            "SET g.name = $name, g.provider = $provider "
            "WITH g "
            "MATCH (r:Repository {tenant_id: $tenant_id, repository_id: $repository_id}) "
            "MERGE (r)-[:BELONGS_TO]->(g)",
            tenant_id=git_org.tenant_id,
            git_org_id=git_org.git_org_id,
            name=git_org.name,
            provider=git_org.provider,
            repository_id=repository.repository_id,
        )
        if not domain_id:
            return
        await tx.run(
            "MERGE (d:Domain {tenant_id: $tenant_id, domain_id: $domain_id}) "
            "WITH d "
            "MATCH (g:GitOrganization {tenant_id: $tenant_id, git_org_id: $git_org_id}) "
            "MERGE (g)-[:PART_OF]->(d)",
            tenant_id=git_org.tenant_id,
            git_org_id=git_org.git_org_id,
            domain_id=domain_id,
        )

    async def ingest(self, payload: ASTTopologyPayload) -> TopologyIngestionResult:
        driver = self._require_driver()
        try:
            async with driver.session(database=self._config.database) as session:
                # 1. Reserve the snapshot as INGESTING (idempotent via MERGE
                #    on the tenant/repository/revision key). Payload-hash
                #    replay short-circuits to READY without re-writing.
                existing = await session.execute_read(
                    self._read_snapshot, payload.tenant_id, payload.repository_id, payload.revision
                )
                if (
                    existing is not None
                    and existing.get("payload_hash") == payload.payload_hash
                    and (existing.get("status") == TopologySnapshotStatus.READY.value)
                ):
                    return TopologyIngestionResult(
                        tenant_id=payload.tenant_id,
                        repository_id=payload.repository_id,
                        revision=payload.revision,
                        snapshot_id=UUID(existing["snapshot_id"]),
                        status=TopologySnapshotStatus.READY,
                    )

                await session.execute_write(
                    self._write_snapshot_status,
                    payload,
                    TopologySnapshotStatus.INGESTING.value,
                    None,
                )

                batch_size = self._config.ingestion_batch_size
                # ISSUE-3: persist macro-tier nodes only. Micro detail is
                # resolved on demand and must never inflate the graph.
                macro_nodes = [n for n in payload.ast_nodes if n.granularity == "macro"]
                micro_skipped = len(payload.ast_nodes) - len(macro_nodes)
                macro_ids = {n.node_id for n in macro_nodes}
                full_ids = {n.node_id for n in payload.ast_nodes}
                kept_calls = []
                for call in payload.calls:
                    if call.caller_node_id not in macro_ids:
                        if call.caller_node_id not in full_ids:
                            raise ValueError(f"dangling caller node {call.caller_node_id}")
                        micro_skipped += 1
                        continue
                    kept_calls.append(call)
                for i in range(0, len(macro_nodes), batch_size):
                    node_batch = macro_nodes[i : i + batch_size]
                    await session.execute_write(self._write_node_batch, payload, node_batch)
                for i in range(0, len(kept_calls), batch_size):
                    call_batch = kept_calls[i : i + batch_size]
                    await session.execute_write(self._write_call_batch, payload, call_batch)
                kept_references = [
                    r
                    for r in payload.references
                    if r.source_node_id in macro_ids and r.target_node_id in macro_ids
                ]
                for i in range(0, len(kept_references), batch_size):
                    ref_batch = kept_references[i : i + batch_size]
                    await session.execute_write(self._write_reference_batch, payload, ref_batch)
                kept_db_accesses = [
                    d for d in payload.database_accesses if d.source_node_id in macro_ids
                ]
                for i in range(0, len(kept_db_accesses), batch_size):
                    db_batch = kept_db_accesses[i : i + batch_size]
                    await session.execute_write(self._write_db_access_batch, payload, db_batch)

                await session.execute_write(
                    self._write_snapshot_status,
                    payload,
                    TopologySnapshotStatus.READY.value,
                    None,
                )
        except TopologyNotConfiguredError:
            raise
        except Exception as exc:
            if _is_transient_neo4j_error(exc):
                raise TopologyProviderUnavailableError(str(exc)) from exc
            logger.exception(
                "Topology ingestion failed; marking snapshot FAILED",
                extra={"tenant_id": payload.tenant_id, "repository_id": payload.repository_id},
            )
            try:
                async with driver.session(database=self._config.database) as session:
                    await session.execute_write(
                        self._write_snapshot_status,
                        payload,
                        TopologySnapshotStatus.FAILED.value,
                        str(exc)[:2000],
                    )
            except Exception:
                logger.exception("Failed to record FAILED snapshot status")
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
            edge_count=len(kept_calls) + len(kept_references) + len(kept_db_accesses),
            micro_skipped_count=micro_skipped,
        )

    @staticmethod
    async def _read_snapshot(
        tx: Any, tenant_id: str, repository_id: str, revision: str
    ) -> dict[str, Any] | None:
        result = await tx.run(
            "MATCH (s:TopologySnapshot {tenant_id: $tenant_id, repository_id: $repository_id, "
            "revision: $revision}) RETURN s.status AS status, s.payload_hash AS payload_hash, "
            "s.snapshot_id AS snapshot_id",
            tenant_id=tenant_id,
            repository_id=repository_id,
            revision=revision,
        )
        record = await result.single()
        return dict(record) if record is not None else None

    @staticmethod
    async def _write_snapshot_status(
        tx: Any, payload: ASTTopologyPayload, status: str, error_summary: str | None
    ) -> None:
        await tx.run(
            "MERGE (s:TopologySnapshot {tenant_id: $tenant_id, repository_id: $repository_id, "
            "revision: $revision}) "
            "SET s.snapshot_id = $snapshot_id, s.status = $status, "
            "s.payload_hash = $payload_hash, s.schema_version = $schema_version, "
            "s.parser_version = $parser_version, s.error_summary = $error_summary, "
            "s.ingested_at = CASE WHEN $status = 'READY' THEN $ingested_at ELSE s.ingested_at END "
            "MERGE (repo:Repository {tenant_id: $tenant_id, repository_id: $repository_id}) "
            "MERGE (s)-[:OF_REPOSITORY]->(repo)",
            tenant_id=payload.tenant_id,
            repository_id=payload.repository_id,
            revision=payload.revision,
            snapshot_id=str(payload.snapshot_id),
            status=status,
            payload_hash=payload.payload_hash,
            schema_version=payload.schema_version,
            parser_version=payload.parser_version,
            error_summary=error_summary,
            ingested_at=datetime.now(UTC).isoformat(),
        )

    @staticmethod
    async def _write_node_batch(tx: Any, payload: ASTTopologyPayload, batch: list[Any]) -> None:
        rows = [
            {
                "node_id": str(n.node_id),
                "name": n.name,
                "qualified_name": n.qualified_name,
                "node_type": n.node_type.value,
                "file_path": n.file_path,
                "start_line": n.start_line,
                "start_column": n.start_column,
                "end_line": n.end_line,
                "end_column": n.end_column,
                "signature_hash": n.signature_hash,
                "parent_node_id": str(n.parent_node_id) if n.parent_node_id else None,
                "granularity": n.granularity,
            }
            for n in batch
        ]
        await tx.run(
            "UNWIND $rows AS row "
            "MERGE (n:ASTNode {tenant_id: $tenant_id, repository_id: $repository_id, "
            "revision: $revision, node_id: row.node_id}) "
            "SET n.name = row.name, n.qualified_name = row.qualified_name, "
            "n.node_type = row.node_type, n.file_path = row.file_path, "
            "n.start_line = row.start_line, n.start_column = row.start_column, "
            "n.end_line = row.end_line, n.end_column = row.end_column, "
            "n.signature_hash = row.signature_hash, n.granularity = row.granularity "
            "MERGE (f:SourceFile {tenant_id: $tenant_id, repository_id: $repository_id, "
            "revision: $revision, path: row.file_path}) "
            "MERGE (n)-[:DECLARED_IN]->(f)",
            rows=rows,
            tenant_id=payload.tenant_id,
            repository_id=payload.repository_id,
            revision=payload.revision,
        )

    @staticmethod
    async def _write_call_batch(tx: Any, payload: ASTTopologyPayload, batch: list[Any]) -> None:
        rows = [
            {
                "caller_node_id": str(c.caller_node_id),
                "callee_node_id": str(c.callee_node_id) if c.callee_node_id else None,
                "call_site_file": c.call_site_file,
                "call_site_line": c.call_site_line,
                "resolution_status": c.resolution_status.value,
                "confidence": c.confidence,
            }
            for c in batch
            if c.callee_node_id is not None
        ]
        if not rows:
            return
        await tx.run(
            "UNWIND $rows AS row "
            "MATCH (caller:ASTNode {tenant_id: $tenant_id, repository_id: $repository_id, "
            "revision: $revision, node_id: row.caller_node_id}) "
            "MATCH (callee:ASTNode {tenant_id: $tenant_id, repository_id: $repository_id, "
            "revision: $revision, node_id: row.callee_node_id}) "
            "MERGE (caller)-[c:CALLS]->(callee) "
            "SET c.call_site_file = row.call_site_file, c.call_site_line = row.call_site_line, "
            "c.resolution_status = row.resolution_status, c.confidence = row.confidence",
            rows=rows,
            tenant_id=payload.tenant_id,
            repository_id=payload.repository_id,
            revision=payload.revision,
        )

    @staticmethod
    async def _write_reference_batch(tx: Any, payload: ASTTopologyPayload, batch: list[Any]) -> None:
        rows = [
            {
                "source_node_id": str(r.source_node_id),
                "target_node_id": str(r.target_node_id),
                "reference_type": r.reference_type,
                "file_path": r.file_path,
                "line_number": r.line_number,
                "confidence": r.confidence,
            }
            for r in batch
        ]
        if not rows:
            return
        await tx.run(
            "UNWIND $rows AS row "
            "MATCH (source:ASTNode {tenant_id: $tenant_id, repository_id: $repository_id, "
            "revision: $revision, node_id: row.source_node_id}) "
            "MATCH (target:ASTNode {tenant_id: $tenant_id, repository_id: $repository_id, "
            "revision: $revision, node_id: row.target_node_id}) "
            "MERGE (source)-[r:REFERENCES]->(target) "
            "SET r.reference_type = row.reference_type, r.file_path = row.file_path, "
            "r.line_number = row.line_number, r.confidence = row.confidence",
            rows=rows,
            tenant_id=payload.tenant_id,
            repository_id=payload.repository_id,
            revision=payload.revision,
        )

    @staticmethod
    async def _write_db_access_batch(tx: Any, payload: ASTTopologyPayload, batch: list[Any]) -> None:
        rows = [
            {
                "source_node_id": str(d.source_node_id),
                "table_name": d.target_entity_or_table,
                "operation_type": d.operation_type,
                "query_fingerprint": d.query_fingerprint,
            }
            for d in batch
        ]
        if not rows:
            return
        await tx.run(
            "UNWIND $rows AS row "
            "MATCH (source:ASTNode {tenant_id: $tenant_id, repository_id: $repository_id, "
            "revision: $revision, node_id: row.source_node_id}) "
            "MERGE (t:DatabaseTable {tenant_id: $tenant_id, table_name: row.table_name}) "
            "MERGE (source)-[e:ACCESSES_TABLE]->(t) "
            "SET e.operation_type = row.operation_type, "
            "e.query_fingerprint = row.query_fingerprint, e.revision = $revision",
            rows=rows,
            tenant_id=payload.tenant_id,
            repository_id=payload.repository_id,
            revision=payload.revision,
        )

    async def get_snapshot_status(
        self, tenant_id: str, repository_id: str, revision: str
    ) -> TopologySnapshotStatus | None:
        driver = self._require_driver()
        async with driver.session(database=self._config.database) as session:
            record = await session.execute_read(
                self._read_snapshot, tenant_id, repository_id, revision
            )
        if record is None:
            return None
        return TopologySnapshotStatus(record["status"])

    async def supersede_snapshot(self, tenant_id: str, repository_id: str, revision: str) -> None:
        driver = self._require_driver()
        async with driver.session(database=self._config.database) as session:
            await session.run(
                "MATCH (s:TopologySnapshot {tenant_id: $tenant_id, repository_id: $repository_id, "
                "revision: $revision}) SET s.status = $status",
                tenant_id=tenant_id,
                repository_id=repository_id,
                revision=revision,
                status=TopologySnapshotStatus.SUPERSEDED.value,
            )

    # -- SnapshotRetentionPort (ISSUE-4) ---------------------------------------

    async def list_snapshots(self, tenant_id: str, repository_id: str) -> list[SnapshotDescriptor]:
        driver = self._require_driver()
        async with driver.session(database=self._config.database) as session:
            result = await session.run(
                "MATCH (s:TopologySnapshot {tenant_id: $tenant_id, repository_id: $repository_id}) "
                "RETURN s.revision AS revision, s.status AS status, "
                "s.ingested_at AS ingested_at",
                tenant_id=tenant_id,
                repository_id=repository_id,
            )
            records = [dict(record) async for record in result]

        descriptors = []
        for record in records:
            ingested_at_raw = record.get("ingested_at")
            # Snapshots ingested before this field existed have no
            # timestamp; treat as "now" so they are never mistakenly aged
            # out by a null comparison (fail closed: keep, don't collect).
            ingested_at = (
                datetime.fromisoformat(ingested_at_raw) if ingested_at_raw else datetime.now(UTC)
            )
            descriptors.append(
                SnapshotDescriptor(
                    tenant_id=tenant_id,
                    repository_id=repository_id,
                    revision=record["revision"],
                    status=TopologySnapshotStatus(record["status"]),
                    ingested_at=ingested_at,
                )
            )
        return descriptors

    async def collect_snapshot(
        self, tenant_id: str, repository_id: str, revision: str
    ) -> SnapshotCollectionResult:
        driver = self._require_driver()
        async with driver.session(database=self._config.database) as session:
            status = await session.execute_read(
                self._read_snapshot, tenant_id, repository_id, revision
            )
            if status is None:
                raise TopologyNotConfiguredError(
                    f"No topology snapshot for repository={repository_id} revision={revision}"
                )
            if status.get("status") == TopologySnapshotStatus.COLLECTED.value:
                return SnapshotCollectionResult(
                    tenant_id=tenant_id,
                    repository_id=repository_id,
                    revision=revision,
                    already_collected=True,
                )
            counts = await session.execute_write(
                self._delete_snapshot_subgraph, tenant_id, repository_id, revision
            )
        return SnapshotCollectionResult(
            tenant_id=tenant_id,
            repository_id=repository_id,
            revision=revision,
            deleted_node_count=counts["deleted_node_count"],
            deleted_edge_count=counts["deleted_edge_count"],
        )

    @staticmethod
    async def _delete_snapshot_subgraph(
        tx: Any, tenant_id: str, repository_id: str, revision: str
    ) -> dict[str, int]:
        """Delete the orphan ASTNode/SourceFile/Package subgraph for one
        revision and mark the ``TopologySnapshot`` audit node ``COLLECTED``.

        The ``TopologySnapshot`` node itself is never deleted — only the
        subgraph beneath it — so historical attribution evidence (which
        already embeds its own snapshot_id/revision/ownership path) remains
        interpretable after collection.
        """
        result = await tx.run(
            "MATCH (n) WHERE (n:ASTNode OR n:SourceFile OR n:Package) "
            "AND n.tenant_id = $tenant_id AND n.repository_id = $repository_id "
            "AND n.revision = $revision "
            "WITH n, count{ (n)--() } AS deg "
            "DETACH DELETE n "
            "RETURN count(n) AS deleted_nodes, sum(deg) AS deleted_edges",
            tenant_id=tenant_id,
            repository_id=repository_id,
            revision=revision,
        )
        record = await result.single()
        deleted_nodes = (
            int(record["deleted_nodes"]) if record and record.get("deleted_nodes") else 0
        )
        deleted_edges = (
            int(record["deleted_edges"]) if record and record.get("deleted_edges") else 0
        )
        await tx.run(
            "MATCH (s:TopologySnapshot {tenant_id: $tenant_id, repository_id: $repository_id, "
            "revision: $revision}) SET s.status = $status",
            tenant_id=tenant_id,
            repository_id=repository_id,
            revision=revision,
            status=TopologySnapshotStatus.COLLECTED.value,
        )
        return {"deleted_node_count": deleted_nodes, "deleted_edge_count": deleted_edges}

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
        driver = self._require_driver()
        status = await self.get_snapshot_status(tenant_id, repository_id, revision)
        if status is None:
            raise TopologyNotConfiguredError(
                f"No topology snapshot for repository={repository_id} revision={revision}"
            )
        if status != TopologySnapshotStatus.READY:
            raise TopologySnapshotNotReadyError(
                f"Snapshot status is {status.value}, not READY",
                retryable=status == TopologySnapshotStatus.INGESTING,
            )

        async with driver.session(database=self._config.database) as session:
            result = await session.execute_read(
                self._match_source_location,
                tenant_id,
                repository_id,
                revision,
                file_path,
                line_number,
            )

        if not result:
            repo_type = await self._get_repository_type(tenant_id, repository_id)
            ownership_path, domain_id, git_org_id = await self._resolve_ownership_path(
                tenant_id, repository_id
            )
            snapshot_id = await self._get_snapshot_id(tenant_id, repository_id, revision)
            return StaticOwnershipResult(
                tenant_id=tenant_id,
                repository_id=repository_id,
                revision=revision,
                snapshot_id=snapshot_id,
                matched_file_path=file_path,
                repository_type=repo_type,
                domain_id=domain_id,
                git_org_id=git_org_id,
                ownership_path=ownership_path,
                fallback_level=AttributionFallbackLevel.REPOSITORY,
            )

        smallest = min(r["range_size"] for r in result)
        tied = [r for r in result if r["range_size"] == smallest]
        if len({r["node_id"] for r in tied}) > 1:
            raise TopologyAmbiguousMatchError(
                f"{len(tied)} equally specific nodes match {file_path}:{line_number}"
            )
        matched = tied[0]
        repo_type = await self._get_repository_type(tenant_id, repository_id)
        ownership_path, domain_id, git_org_id = await self._resolve_ownership_path(
            tenant_id, repository_id
        )
        snapshot_id = await self._get_snapshot_id(tenant_id, repository_id, revision)
        matched_node_type: TopologyNodeType | None = None
        try:
            matched_node_type = TopologyNodeType(matched["node_type"])
        except (KeyError, ValueError):
            matched_node_type = None
        return StaticOwnershipResult(
            tenant_id=tenant_id,
            repository_id=repository_id,
            revision=revision,
            snapshot_id=snapshot_id,
            matched_node_id=UUID(matched["node_id"]),
            matched_node_qualified_name=matched["qualified_name"],
            matched_file_path=matched["file_path"],
            matched_start_line=matched["start_line"],
            matched_end_line=matched["end_line"],
            repository_type=repo_type,
            domain_id=domain_id,
            git_org_id=git_org_id,
            ownership_path=ownership_path,
            fallback_level=AttributionFallbackLevel.AST_NODE,
            node_type=matched_node_type,
        )

    @staticmethod
    async def _match_source_location(
        tx: Any,
        tenant_id: str,
        repository_id: str,
        revision: str,
        file_path: str,
        line_number: int,
    ) -> list[dict[str, Any]]:
        result = await tx.run(
            "MATCH (n:ASTNode {tenant_id: $tenant_id, repository_id: $repository_id, "
            "revision: $revision, file_path: $file_path}) "
            "WHERE n.start_line <= $line_number AND $line_number <= n.end_line "
            "RETURN n.node_id AS node_id, n.qualified_name AS qualified_name, "
            "n.file_path AS file_path, n.start_line AS start_line, n.end_line AS end_line, "
            "n.node_type AS node_type, "
            "(n.end_line - n.start_line) AS range_size "
            "ORDER BY range_size ASC, n.node_id ASC "
            "LIMIT 50",
            tenant_id=tenant_id,
            repository_id=repository_id,
            revision=revision,
            file_path=file_path,
            line_number=line_number,
        )
        return [dict(record) async for record in result]

    async def _get_repository_type(self, tenant_id: str, repository_id: str) -> RepositoryType:
        driver = self._require_driver()
        async with driver.session(database=self._config.database) as session:
            result = await session.run(
                "MATCH (r:Repository {tenant_id: $tenant_id, repository_id: $repository_id}) "
                "RETURN r.repository_type AS repository_type",
                tenant_id=tenant_id,
                repository_id=repository_id,
            )
            record = await result.single()
        if record is None or not record.get("repository_type"):
            return RepositoryType.UNKNOWN
        try:
            return RepositoryType(record["repository_type"])
        except ValueError:
            return RepositoryType.UNKNOWN

    async def _resolve_ownership_path(
        self, tenant_id: str, repository_id: str
    ) -> tuple[list[str], str | None, str | None]:
        """Traverse Repository -[:BELONGS_TO]-> GitOrganization -[:PART_OF]-> Domain.

        Returns ``(ownership_path, domain_id, git_org_id)``; each element is
        absent (empty list / ``None``) rather than fabricated when the
        chain (or part of it) was never registered via
        ``register_ownership`` (ISSUE-7).
        """
        driver = self._require_driver()
        async with driver.session(database=self._config.database) as session:
            result = await session.run(
                "MATCH (r:Repository {tenant_id: $tenant_id, repository_id: $repository_id}) "
                "OPTIONAL MATCH (r)-[:BELONGS_TO]->(g:GitOrganization) "
                "OPTIONAL MATCH (g)-[:PART_OF]->(d:Domain) "
                "RETURN g.git_org_id AS git_org_id, d.domain_id AS domain_id",
                tenant_id=tenant_id,
                repository_id=repository_id,
            )
            record = await result.single()
        if record is None:
            return [], None, None
        git_org_id = record.get("git_org_id")
        domain_id = record.get("domain_id")
        ownership_path = [p for p in (git_org_id, domain_id) if p]
        return ownership_path, domain_id, git_org_id

    async def _get_snapshot_id(self, tenant_id: str, repository_id: str, revision: str) -> UUID:
        driver = self._require_driver()
        async with driver.session(database=self._config.database) as session:
            record = await session.execute_read(
                self._read_snapshot, tenant_id, repository_id, revision
            )
        if record is None or not record.get("snapshot_id"):
            return UUID(int=0)
        return UUID(record["snapshot_id"])
