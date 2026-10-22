"""Capability vocabulary and pure authorization policy for tenant onboarding."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


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
            return AuthorizationDecision(False, principal_id, required, scope, "SEPARATION_OF_DUTIES")
    return AuthorizationDecision(True, principal_id, required, scope, "ALLOWED")
