"""Capability vocabulary and pure authorization policy for tenant onboarding."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol


class Capability(StrEnum):
    """Actions that may be granted for a tenant-scoped onboarding operation."""

    READ = "tenant:read"
    ONBOARD = "tenant:onboard"
    CONFIGURE = "tenant:configure"
    APPROVE = "tenant:approve"
    CLEANUP = "tenant:cleanup"
    SUSPEND = "tenant:suspend"
    RESUME = "tenant:resume"
    SUPPORT = "tenant:support"


@dataclass(frozen=True)
class AuthorizationScope:
    """Tenant/application/environment scope of one requested operation."""

    tenant_id: str
    application_id: str | None
    environment: str
    operation: str


@dataclass(frozen=True)
class AuthorizationDecision:
    """Auditable outcome of the pure capability policy check."""

    allowed: bool
    principal_id: str
    capability: Capability
    scope: AuthorizationScope
    reason_code: str


PLATFORM_SCOPE = "__platform__"


@dataclass(frozen=True)
class ScopedGrant:
    principal_id: str
    capability: Capability
    tenant_id: str
    application_id: str
    environment: str
    operation: str
    valid_from: datetime
    expires_at: datetime
    revoked_at: datetime | None
    policy_version: str
    issuer_id: str


class GrantReader(Protocol):
    async def find_grant(
        self, *, principal_id: str, capability: Capability, scope: AuthorizationScope
    ) -> ScopedGrant | None: ...

    async def has_platform_grant(
        self,
        *,
        principal_id: str,
        capability: Capability,
        scope: AuthorizationScope,
        policy_version: str,
        at: datetime,
    ) -> bool: ...


async def authorize_scoped(
    *,
    principal_id: str,
    identity_tenant_id: str | None,
    required: Capability,
    scope: AuthorizationScope,
    policy_version: str,
    grant_reader: GrantReader,
    requester_id: str | None = None,
    now: datetime | None = None,
) -> AuthorizationDecision:
    """Resolve and validate a persisted, exact-scope capability grant.

    The tenant claim is identity context only. Platform authority is represented
    by an explicit ``__platform__`` grant rather than a Boolean operator flag.
    """
    if not principal_id.strip():
        return AuthorizationDecision(False, principal_id, required, scope, "PRINCIPAL_MISSING")
    instant = now or datetime.now(UTC)
    grant = await grant_reader.find_grant(
        principal_id=principal_id, capability=required, scope=scope
    )
    if grant is None:
        if scope.tenant_id != PLATFORM_SCOPE and await grant_reader.has_platform_grant(
            principal_id=principal_id,
            capability=required,
            scope=scope,
            policy_version=policy_version,
            at=instant,
        ):
            if required in _SEPARATION_OF_DUTIES and (
                requester_id is None or principal_id == requester_id
            ):
                return AuthorizationDecision(
                    False, principal_id, required, scope, "SEPARATION_OF_DUTIES"
                )
            return AuthorizationDecision(True, principal_id, required, scope, "ALLOWED")
        else:
            return AuthorizationDecision(False, principal_id, required, scope, "GRANT_MISSING")
    if grant.tenant_id == PLATFORM_SCOPE:
        if grant.revoked_at is not None or not (grant.valid_from <= instant < grant.expires_at):
            return AuthorizationDecision(False, principal_id, required, scope, "GRANT_INACTIVE")
        if grant.policy_version != policy_version:
            return AuthorizationDecision(
                False, principal_id, required, scope, "POLICY_VERSION_STALE"
            )
        if grant.application_id != (scope.application_id or ""):
            return AuthorizationDecision(
                False, principal_id, required, scope, "APPLICATION_MISMATCH"
            )
        if grant.environment != scope.environment:
            return AuthorizationDecision(
                False, principal_id, required, scope, "ENVIRONMENT_MISMATCH"
            )
        if grant.operation != scope.operation:
            return AuthorizationDecision(False, principal_id, required, scope, "OPERATION_MISMATCH")
        if required in _SEPARATION_OF_DUTIES and (
            requester_id is None or principal_id == requester_id
        ):
            return AuthorizationDecision(
                False, principal_id, required, scope, "SEPARATION_OF_DUTIES"
            )
        return AuthorizationDecision(True, principal_id, required, scope, "ALLOWED")
    if grant.principal_id != principal_id or grant.capability is not required:
        return AuthorizationDecision(False, principal_id, required, scope, "GRANT_SCOPE_MISMATCH")
    if grant.revoked_at is not None or not (grant.valid_from <= instant < grant.expires_at):
        return AuthorizationDecision(False, principal_id, required, scope, "GRANT_INACTIVE")
    if grant.policy_version != policy_version:
        return AuthorizationDecision(False, principal_id, required, scope, "POLICY_VERSION_STALE")
    platform = grant.tenant_id == PLATFORM_SCOPE
    if required in _OPERATOR_ONLY and not platform:
        return AuthorizationDecision(False, principal_id, required, scope, "OPERATOR_ROLE_REQUIRED")
    if not platform and (
        grant.tenant_id != scope.tenant_id or identity_tenant_id != scope.tenant_id
    ):
        return AuthorizationDecision(False, principal_id, required, scope, "TENANT_MISMATCH")
    if grant.application_id != (scope.application_id or ""):
        return AuthorizationDecision(False, principal_id, required, scope, "APPLICATION_MISMATCH")
    if grant.environment != scope.environment:
        return AuthorizationDecision(False, principal_id, required, scope, "ENVIRONMENT_MISMATCH")
    if grant.operation != scope.operation:
        return AuthorizationDecision(False, principal_id, required, scope, "OPERATION_MISMATCH")
    if required in _SEPARATION_OF_DUTIES and (requester_id is None or principal_id == requester_id):
        return AuthorizationDecision(False, principal_id, required, scope, "SEPARATION_OF_DUTIES")
    return AuthorizationDecision(True, principal_id, required, scope, "ALLOWED")


_OPERATOR_ONLY = frozenset({Capability.ONBOARD, Capability.CLEANUP, Capability.APPROVE})
_SEPARATION_OF_DUTIES = frozenset({Capability.APPROVE, Capability.CLEANUP})


def authorize(
    *,
    principal_id: str,
    identity_tenant_id: str | None,
    granted: frozenset[Capability],
    required: Capability,
    scope: AuthorizationScope,
    platform_operator: bool,
    requester_id: str | None = None,
) -> AuthorizationDecision:
    """Authorize one operation; tenant identity alone never grants capability.

    The caller must derive ``platform_operator`` and ``granted`` from a
    verified identity and authoritative policy. Approval and cleanup must
    identify the original requester and require a different authenticated
    principal; missing requester identity fails closed.
    """
    if not platform_operator and identity_tenant_id != scope.tenant_id:
        return AuthorizationDecision(False, principal_id, required, scope, "TENANT_MISMATCH")
    if required in _OPERATOR_ONLY and not platform_operator:
        return AuthorizationDecision(False, principal_id, required, scope, "OPERATOR_ROLE_REQUIRED")
    if required not in granted:
        return AuthorizationDecision(False, principal_id, required, scope, "CAPABILITY_MISSING")
    if required in _SEPARATION_OF_DUTIES:
        if requester_id is None or principal_id == requester_id:
            return AuthorizationDecision(
                False, principal_id, required, scope, "SEPARATION_OF_DUTIES"
            )
    return AuthorizationDecision(True, principal_id, required, scope, "ALLOWED")
