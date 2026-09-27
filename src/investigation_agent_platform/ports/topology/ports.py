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
    GitOrganizationIdentity,
    MicroSymbolMatch,
    RepositoryIdentity,
    SnapshotCollectionResult,
    SnapshotDescriptor,
    StaticOwnershipResult,
    TopologyIngestionResult,
    TopologySnapshotStatus,
    TraceHopTarget,
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

    async def resolve_for_service_name(
        self, tenant_id: str, service_name: str
    ) -> RepositoryIdentity | None:
        """Maps a runtime service/queue name (as it appears in distributed
        trace target-service metadata) to its repository identity (ISSUE-5).
        Returns ``None`` when no profile declares that service name — never
        guessed from name similarity to ``application_id``."""
        ...


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


@runtime_checkable
class OwnershipRegistryPort(Protocol):
    """Writes the organizational ownership chain a topology store needs to
    answer ``domain_id``/``git_org_id``/``ownership_path`` on
    ``DomainAttributionPort.resolve_source_location`` (ISSUE-7).

    Idempotent: registering the same repository/org twice must not
    duplicate nodes or edges. ``git_org`` and ``domain_id`` may each be
    ``None`` when unresolvable — the repository is still registered.
    """

    async def register_ownership(
        self,
        repository: RepositoryIdentity,
        git_org: GitOrganizationIdentity | None,
        domain_id: str | None,
    ) -> None: ...


@runtime_checkable
class SnapshotRetentionPort(Protocol):
    """Lists and collects (garbage-collects) topology snapshots (ISSUE-4).

    ``collect_snapshot`` must delete only the orphan AST/SourceFile/Package
    subgraph for one revision — the ``TopologySnapshot`` audit node itself
    is never deleted, so historical attribution evidence (which embeds its
    own snapshot_id/revision/ownership path) stays interpretable. Must be
    idempotent: collecting an already-``COLLECTED`` snapshot is a safe no-op.
    """

    async def list_snapshots(
        self, tenant_id: str, repository_id: str
    ) -> list[SnapshotDescriptor]: ...

    async def collect_snapshot(
        self, tenant_id: str, repository_id: str, revision: str
    ) -> SnapshotCollectionResult: ...


@runtime_checkable
class PinnedRevisionsPort(Protocol):
    """Resolves the set of revisions an *open* investigation still needs
    (ISSUE-4). Never guessed — derived only from an investigation's own
    recorded topology-attribution evidence, so a false negative (failing to
    pin) is impossible without an actual missing evidence record.
    """

    async def list_pinned_revisions(self, tenant_id: str, repository_id: str) -> set[str]: ...


@runtime_checkable
class TraceHopResolverPort(Protocol):
    """Resolves the cross-service target of one distributed-trace span
    (ISSUE-5), corroborated only by runtime trace evidence already
    retrievable through the tenant-scoped evidence gateway — never by
    static endpoint/name-similarity guessing.

    Returns ``None`` (never fabricates) when the trace has no other-service
    span, the target service is ambiguous, or the trace/tenant scope
    doesn't resolve.
    """

    async def resolve_target_service(
        self,
        tenant_id: str,
        investigation_id: UUID,
        application_id: str,
        environment: str,
        trace_id: str,
        source_span_id: str | None,
    ) -> TraceHopTarget | None: ...
