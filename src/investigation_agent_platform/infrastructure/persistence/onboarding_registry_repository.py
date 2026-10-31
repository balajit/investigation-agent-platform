# src/investigation_agent_platform/infrastructure/persistence/onboarding_registry_repository.py
"""Tenant-registry lifecycle transitions for onboarding (Phase 4, design D4).

Guarded status transitions with lifecycle-epoch fencing: every transition
bumps the epoch so stale workers and workflows cannot write or report
completion after the epoch changes. Activation additionally requires the
caller to have verified readiness; the repository enforces the status guard,
not the policy decision.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import select

from investigation_agent_platform.infrastructure.persistence.models import (
    ApplicationProfileORM,
    InvestigationORM,
    TenantRegistryORM,
)
from investigation_agent_platform.infrastructure.persistence.rls import (
    rls_session,
)


class RegistryTransitionError(ValueError):
    """The registry entry is not in a state this transition can leave."""


class SqlAlchemyOnboardingRegistryRepository:
    """CAS-guarded registry status transitions under tenant context."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def read_entry(self, tenant_id: str) -> TenantRegistryORM | None:
        """Return the registry entry for a canonical tenant, or None."""
        if not tenant_id or len(tenant_id) > 64:
            raise ValueError("tenant_id must be non-empty and at most 64 characters")
        async with rls_session(self._session_factory, tenant_id) as session:
            return cast(
                TenantRegistryORM | None,
                await session.scalar(
                    select(TenantRegistryORM).where(TenantRegistryORM.canonical_id == tenant_id)
                ),
            )

    async def list_known_tenant_ids(self) -> list[str]:
        """Return every tenant id the platform knows, cross-tenant (SELECT only).

        The union of ``tenant_registry`` ids and the tenant ids carried by
        investigations and application profiles. The registry alone is not the
        real sibling universe: tenants predate or bypass registration, and an
        empty registry would make the index-scope collision guard inert. A
        tenant that appears in any of the three can own indexes.

        Deliberately opens a session WITHOUT a tenant context: the caller is
        the platform itself (index-scope collision guard, onboarding id gate),
        not a tenant. All three tables have FORCE RLS keyed on the tenant id,
        so this sees every row only while the app role bypasses RLS (the
        current deployment). When least-privilege roles land (review R11) this
        read needs an explicit grant path (registry-admin principal or a narrow
        SECURITY DEFINER lookup), or it will silently see no sibling tenants.
        """
        async with self._session_factory() as session:
            ids: set[str] = set()
            for column in (
                TenantRegistryORM.canonical_id,
                InvestigationORM.tenant_id,
                ApplicationProfileORM.tenant_id,
            ):
                ids.update(str(row) for row in await session.scalars(select(column).distinct()))
            return sorted(ids)

    async def _transition(
        self, tenant_id: str, *, from_status: str, to_status: str
    ) -> TenantRegistryORM:
        async with rls_session(self._session_factory, tenant_id) as session:
            row = await session.scalar(
                select(TenantRegistryORM)
                .where(TenantRegistryORM.canonical_id == tenant_id)
                .with_for_update()
            )
            if row is None:
                raise RegistryTransitionError(f"no registry entry for tenant {tenant_id!r}")
            if row.status != from_status:
                raise RegistryTransitionError(
                    f"tenant {tenant_id!r} is {row.status}, not {from_status}"
                )
            row.status = to_status
            row.lifecycle_epoch = int(row.lifecycle_epoch) + 1
            await session.flush()
            return row

    async def activate(self, tenant_id: str) -> TenantRegistryORM:
        """Move RESERVED to ACTIVE. Readiness is verified by the caller."""
        return await self._transition(tenant_id, from_status="RESERVED", to_status="ACTIVE")

    async def suspend(self, tenant_id: str) -> TenantRegistryORM:
        """Move ACTIVE to SUSPENDED."""
        return await self._transition(tenant_id, from_status="ACTIVE", to_status="SUSPENDED")

    async def resume(self, tenant_id: str) -> TenantRegistryORM:
        """Move SUSPENDED back to ACTIVE."""
        return await self._transition(tenant_id, from_status="SUSPENDED", to_status="ACTIVE")
