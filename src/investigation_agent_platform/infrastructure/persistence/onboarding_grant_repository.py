"""Tenant-scoped onboarding grant reads and platform-grant resolution."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from investigation_agent_platform.application.onboarding.authorization import (
    PLATFORM_SCOPE,
    AuthorizationScope,
    Capability,
    ScopedGrant,
)
from investigation_agent_platform.infrastructure.persistence.rls import rls_session


class SqlAlchemyOnboardingGrantReader:
    """Resolve exact tenant grants and platform grants without global table reads."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def find_grant(
        self,
        *,
        principal_id: str,
        capability: Capability,
        scope: AuthorizationScope,
    ) -> ScopedGrant | None:
        if scope.tenant_id == PLATFORM_SCOPE:
            return None
        async with rls_session(self._session_factory, scope.tenant_id) as session:
            result = await session.execute(
                text(
                    "SELECT principal_id, capability, tenant_id, application_id, environment, "
                    "operation, valid_from, expires_at, revoked_at, policy_version, issuer_id "
                    "FROM onboarding_grants WHERE principal_id = :principal_id "
                    "AND capability = :capability AND tenant_id = :tenant_id "
                    "AND application_id = :application_id AND environment = :environment "
                    "AND operation = :operation AND revoked_at IS NULL "
                    "AND valid_from <= :now AND expires_at > :now "
                    "ORDER BY expires_at DESC LIMIT 1"
                ),
                {
                    "principal_id": principal_id,
                    "capability": capability.value,
                    "tenant_id": scope.tenant_id,
                    "application_id": scope.application_id or "",
                    "environment": scope.environment,
                    "operation": scope.operation,
                    "now": datetime.now(UTC),
                },
            )
            row = result.mappings().first()
            if row is None:
                return None
            return ScopedGrant(
                principal_id=row["principal_id"],
                capability=Capability(row["capability"]),
                tenant_id=row["tenant_id"],
                application_id=row["application_id"],
                environment=row["environment"],
                operation=row["operation"],
                valid_from=row["valid_from"],
                expires_at=row["expires_at"],
                revoked_at=row["revoked_at"],
                policy_version=row["policy_version"],
                issuer_id=row["issuer_id"],
            )

    async def has_platform_grant(
        self,
        *,
        principal_id: str,
        capability: Capability,
        scope: AuthorizationScope,
        policy_version: str,
        at: datetime,
    ) -> bool:
        # The function has only a boolean result and is executable only by the
        # application role. It returns no global grant row or grant detail.
        async with rls_session(self._session_factory, scope.tenant_id) as session:
            result = await session.scalar(
                text(
                    "SELECT onboarding_has_platform_grant(:principal_id, :capability, "
                    ":application_id, :environment, :operation, :policy_version, :at)"
                ),
                {
                    "principal_id": principal_id,
                    "capability": capability.value,
                    "application_id": scope.application_id or "",
                    "environment": scope.environment,
                    "operation": scope.operation,
                    "policy_version": policy_version,
                    "at": at,
                },
            )
            return result is True
