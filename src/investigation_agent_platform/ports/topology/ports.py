# src/investigation_agent_platform/ports/topology/ports.py
"""Layer 3 topology ports.

Neo4j and Cypher are infrastructure details. The application layer (and
Temporal activities) must depend only on these protocols, never construct a
graph driver directly (D7 in the design document).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from investigation_agent_platform.domain.topology.models import (
    ASTTopologyPayload,
    MicroSymbolMatch,
    RepositoryIdentity,
    StaticOwnershipResult,
    TopologyIngestionResult,
    TopologySnapshotStatus,
)


@runtime_checkable
class TopologyIngestionPort(Protocol):
    """Ingests one immutable repository-revision AST topology snapshot."""

    async def ingest(self, payload: ASTTopologyPayload) -> TopologyIngestionResult: ...


@runtime_checkable
class CodeTopologyRepository(Protocol):
    """Read access to ingested topology snapshots."""

    async def get_snapshot_status(
        self, tenant_id: str, repository_id: str, revision: str
    ) -> TopologySnapshotStatus | None: ...

    async def supersede_snapshot(
        self, tenant_id: str, repository_id: str, revision: str
    ) -> None: ...


@runtime_checkable
class DomainAttributionPort(Protocol):
    """Resolves one source location to its static ownership path.

    Implementations must:
    - filter every query by ``tenant_id`` and ``repository_id``/``revision``
      from the first predicate (never a post-filter);
    - only resolve against ``READY`` snapshots;
    - select the most specific enclosing node deterministically or raise an
      ambiguity error rather than choosing arbitrarily.
    """

    async def resolve_source_location(
        self,
        tenant_id: str,
        application_id: str,
        investigation_id: UUID,
        repository_id: str,
        revision: str,
        file_path: str,
        line_number: int,
    ) -> StaticOwnershipResult: ...


@runtime_checkable
class RepositoryRegistryPort(Protocol):
    """Maps a tenant/application profile to its canonical repository identity."""

    async def resolve_for_application(
        self, tenant_id: str, application_id: str
    ) -> RepositoryIdentity | None: ...

    async def register_repository(self, repository: RepositoryIdentity) -> None: ...


@runtime_checkable
class MicroResolverPort(Protocol):
    """On-demand micro-tier symbol resolution (ISSUE-3).

    Parses the working-tree file at query time for symbols too fine-grained
    to persist (see ``MACRO_NODE_TYPES``). Bounded and tenant-scoped by
    contract: implementations must enforce file-size caps, timeouts, and
    repository allowlists, and must report whether the working-tree revision
    matches the requested revision.
    """

    async def resolve_micro_symbol(
        self,
        tenant_id: str,
        repository_id: str,
        locator: str,
        revision: str,
        file_path: str,
        line_number: int,
    ) -> MicroSymbolMatch | None: ...


@runtime_checkable
class CodeownersResolverPort(Protocol):
    """CODEOWNERS-based team ownership lookup (ISSUE-6).

    Lowest-confidence ownership tier, consulted only when static ownership
    yields no domain owner. Returns the owning team identifier or ``None``.
    """

    async def resolve_owner(self, tenant_id: str, locator: str, file_path: str) -> str | None: ...
