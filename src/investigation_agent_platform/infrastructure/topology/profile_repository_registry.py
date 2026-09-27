# src/investigation_agent_platform/infrastructure/topology/profile_repository_registry.py
"""``RepositoryRegistryPort`` backed by ``ApplicationProfileRepository``.

Resolves a tenant/application profile's ``CodeProfile.repository_id`` to a
canonical ``RepositoryIdentity``. This is the tenant-safe bridge between the
existing profile/access configuration and the new topology bounded context —
it never invents a repository identity for a profile that has not been
explicitly linked (F-034/F-040 precedent: fail closed rather than guess).
"""

from __future__ import annotations

import logging

from investigation_agent_platform.domain.topology.models import (
    RepositoryIdentity,
    RepositoryType,
)
from investigation_agent_platform.ports.persistence.repositories import (
    ApplicationProfileRepository,
)

logger = logging.getLogger(__name__)


class ProfileBackedRepositoryRegistry:
    """Derives repository identity from the tenant's ``ApplicationProfile``.

    ``register_repository`` is a no-op placeholder here: this adapter treats
    the profile as the source of truth and does not maintain a separate
    write-side registry. A future explicit ``RepositoryRegistry`` table can
    replace this adapter without changing the port contract.
    """

    def __init__(self, profile_repo: ApplicationProfileRepository) -> None:
        self._profile_repo = profile_repo

    async def resolve_for_application(
        self, tenant_id: str, application_id: str
    ) -> RepositoryIdentity | None:
        profile = await self._profile_repo.get_by_application_id(tenant_id, application_id)
        if profile is None:
            return None
        code = profile.code_configuration
        repository_id = getattr(code, "repository_id", None)
        if not repository_id:
            logger.warning(
                "Application profile has no linked repository_id; topology "
                "attribution is unavailable until CodeProfile.repositoryId is set",
                extra={"tenant_id": tenant_id, "application_id": application_id},
            )
            return None
        return RepositoryIdentity(
            tenant_id=tenant_id,
            repository_id=repository_id,
            name=code.repository,
            locator=code.repository,
            git_org_id=f"{tenant_id}:unassigned",
            repository_type=RepositoryType.UNKNOWN,
            default_branch=code.default_branch,
            is_active=True,
        )

    async def register_repository(self, repository: RepositoryIdentity) -> None:
        # Intentionally a no-op: registration in this adapter happens through
        # profile configuration (`CodeProfile.repositoryId`), not a separate
        # write path. Explicit no-op per "Structural placeholder" convention.
        logger.debug(
            "register_repository is a no-op for ProfileBackedRepositoryRegistry",
            extra={"tenant_id": repository.tenant_id, "repository_id": repository.repository_id},
        )
