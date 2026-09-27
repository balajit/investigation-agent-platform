# src/investigation_agent_platform/infrastructure/topology/profile_repository_registry.py
"""``RepositoryRegistryPort`` backed by ``ApplicationProfileRepository``.

Resolves a tenant/application profile's ``CodeProfile.repository_id`` to a
canonical ``RepositoryIdentity``. This is the tenant-safe bridge between the
existing profile/access configuration and the new topology bounded context —
it never invents a repository identity for a profile that has not been
explicitly linked (F-034/F-040 precedent: fail closed rather than guess).

ISSUE-7: on each resolution, this adapter also derives and write-throughs
the organizational ownership chain a Neo4j-backed topology store needs to
answer ``domain_id``/``git_org_id`` — two independent, narrow sources, per
the design decision:

- ``GitOrganization`` is parsed from the profile's repository locator
  (``infrastructure.topology.ownership.derive_git_organization``) instead
  of the previous hardcoded ``"unassigned"`` placeholder.
- ``Domain`` (team ownership) is read from the repository checkout's
  CODEOWNERS catch-all rule (``infrastructure.topology.ownership.
  derive_repository_domain``), reusing the same tenant-scoped,
  traversal-guarded checkout resolution the query-time ``CodeownersResolver``
  (ISSUE-6) already uses.

Both derivations fail closed to ``None`` — an unresolvable org or a missing
CODEOWNERS catch-all rule leaves the corresponding identity unset rather
than guessed. The profile stays the single source of truth; the graph is a
derived, rebuildable index (same pattern as the pgvector/Graphiti
projections in the knowledge-layer design).
"""

from __future__ import annotations

import logging
from pathlib import Path

from investigation_agent_platform.application.investigation.validator import _has_traversal
from investigation_agent_platform.domain.profile.models import ApplicationProfile
from investigation_agent_platform.domain.topology.models import (
    RepositoryIdentity,
    RepositoryType,
)
from investigation_agent_platform.infrastructure.topology.ownership import (
    derive_git_organization,
    derive_repository_domain,
)
from investigation_agent_platform.ports.persistence.repositories import (
    ApplicationProfileRepository,
)
from investigation_agent_platform.ports.topology.ports import OwnershipRegistryPort

logger = logging.getLogger(__name__)


class ProfileBackedRepositoryRegistry:
    """Derives repository identity — and its ownership chain — from the
    tenant's ``ApplicationProfile``.

    ``register_repository`` write-throughs to the injected
    ``ownership_registry`` (when configured); without one, resolution still
    returns a correct ``RepositoryIdentity`` but nothing is persisted.
    """

    def __init__(
        self,
        profile_repo: ApplicationProfileRepository,
        ownership_registry: OwnershipRegistryPort | None = None,
        repo_base_path: str = "",
    ) -> None:
        self._profile_repo = profile_repo
        self._ownership_registry = ownership_registry
        self._repo_base_path = Path(repo_base_path).resolve() if repo_base_path else None

    def _derive_domain(self, tenant_id: str, locator: str) -> str | None:
        """Read the repository's CODEOWNERS catch-all rule, tenant-scoped.

        Mirrors ``CodeownersResolver``'s traversal/symlink guards exactly;
        returns ``None`` (never raises) on any unavailable or unsafe path
        so a missing checkout degrades to "domain unknown", not a crash.
        """
        if self._repo_base_path is None or _has_traversal(locator) or Path(locator).is_absolute():
            return None
        try:
            repo_path = (self._repo_base_path / locator).resolve()
            if not repo_path.is_relative_to(self._repo_base_path):
                return None
            if repo_path.is_symlink() or not repo_path.is_dir():
                return None
        except OSError:
            return None
        return derive_repository_domain(repo_path)

    async def resolve_for_application(
        self, tenant_id: str, application_id: str
    ) -> RepositoryIdentity | None:
        profile = await self._profile_repo.get_by_application_id(tenant_id, application_id)
        if profile is None:
            return None
        return await self._build_repository_identity(tenant_id, application_id, profile)

    async def resolve_for_service_name(
        self, tenant_id: str, service_name: str
    ) -> RepositoryIdentity | None:
        """ISSUE-5: reverse-lookup a repository by the runtime service name
        its ``CodeProfile`` declares. Linear scan over the tenant's profiles
        — correct and tenant-scoped; optimize with an indexed lookup only if
        profile volume per tenant ever makes this a hot path."""
        profiles = await self._profile_repo.list(tenant_id)
        for profile in profiles:
            code = profile.code_configuration
            if getattr(code, "service_name", None) == service_name:
                return await self._build_repository_identity(tenant_id, profile.id, profile)
        return None

    async def _build_repository_identity(
        self, tenant_id: str, application_id: str, profile: ApplicationProfile
    ) -> RepositoryIdentity | None:
        code = profile.code_configuration
        repository_id = getattr(code, "repository_id", None)
        if not repository_id:
            logger.warning(
                "Application profile has no linked repository_id; topology "
                "attribution is unavailable until CodeProfile.repositoryId is set",
                extra={"tenant_id": tenant_id, "application_id": application_id},
            )
            return None
        git_org = derive_git_organization(tenant_id, code.repository)
        repository = RepositoryIdentity(
            tenant_id=tenant_id,
            repository_id=repository_id,
            name=code.repository,
            locator=code.repository,
            git_org_id=git_org.git_org_id if git_org is not None else f"{tenant_id}:unassigned",
            repository_type=RepositoryType.UNKNOWN,
            default_branch=code.default_branch,
            is_active=True,
        )
        domain_id = self._derive_domain(tenant_id, code.repository)
        await self._write_ownership(repository, git_org, domain_id)
        return repository

    async def _write_ownership(
        self, repository: RepositoryIdentity, git_org: object | None, domain_id: str | None
    ) -> None:
        if self._ownership_registry is None:
            logger.debug(
                "Ownership write-through skipped (no ownership_registry configured)",
                extra={
                    "tenant_id": repository.tenant_id,
                    "repository_id": repository.repository_id,
                },
            )
            return
        from investigation_agent_platform.domain.topology.models import (
            GitOrganizationIdentity,
        )

        resolved_git_org = git_org if isinstance(git_org, GitOrganizationIdentity) else None
        await self._ownership_registry.register_ownership(
            repository=repository, git_org=resolved_git_org, domain_id=domain_id
        )

    async def register_repository(self, repository: RepositoryIdentity) -> None:
        # RepositoryRegistryPort shape: no derived org/domain available here
        # (only a bare RepositoryIdentity), so this registers the repository
        # node alone. `resolve_for_application` is the path that derives and
        # writes the full ownership chain.
        await self._write_ownership(repository, git_org=None, domain_id=None)
