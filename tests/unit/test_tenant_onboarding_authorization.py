"""D1 capability authorization matrix for tenant onboarding."""

from datetime import UTC, datetime, timedelta

import pytest

from investigation_agent_platform.application.onboarding.authorization import (
    AuthorizationScope,
    Capability,
    ScopedGrant,
    authorize,
    authorize_scoped,
)

SCOPE = AuthorizationScope(
    tenant_id="tenant-synthetic-a",
    application_id="application-synthetic-a",
    environment="test",
    operation="onboarding-test",
)


@pytest.mark.parametrize(
    (
        "principal",
        "identity_tenant",
        "granted",
        "required",
        "operator",
        "requester",
        "allowed",
        "reason",
    ),
    [
        (
            "principal-reader",
            "tenant-synthetic-a",
            frozenset({Capability.READ}),
            Capability.READ,
            False,
            None,
            True,
            "ALLOWED",
        ),
        (
            "principal-reader",
            "tenant-synthetic-a",
            frozenset({Capability.READ}),
            Capability.CONFIGURE,
            False,
            None,
            False,
            "CAPABILITY_MISSING",
        ),
        (
            "principal-tenant-admin",
            "tenant-synthetic-a",
            frozenset({Capability.ONBOARD}),
            Capability.ONBOARD,
            False,
            None,
            False,
            "OPERATOR_ROLE_REQUIRED",
        ),
        (
            "principal-operator",
            None,
            frozenset({Capability.ONBOARD}),
            Capability.ONBOARD,
            True,
            None,
            True,
            "ALLOWED",
        ),
        (
            "principal-requester",
            None,
            frozenset({Capability.APPROVE}),
            Capability.APPROVE,
            True,
            "principal-requester",
            False,
            "SEPARATION_OF_DUTIES",
        ),
        (
            "principal-approver",
            None,
            frozenset({Capability.APPROVE}),
            Capability.APPROVE,
            True,
            "principal-requester",
            True,
            "ALLOWED",
        ),
        (
            "principal-operator",
            None,
            frozenset({Capability.CLEANUP}),
            Capability.CLEANUP,
            True,
            None,
            False,
            "SEPARATION_OF_DUTIES",
        ),
        (
            "principal-cleanup-approver",
            None,
            frozenset({Capability.CLEANUP}),
            Capability.CLEANUP,
            True,
            "principal-cleanup-requester",
            True,
            "ALLOWED",
        ),
        (
            "principal-support",
            "tenant-synthetic-a",
            frozenset({Capability.SUPPORT}),
            Capability.SUPPORT,
            False,
            None,
            True,
            "ALLOWED",
        ),
        (
            "principal-other-tenant",
            "tenant-synthetic-b",
            frozenset({Capability.READ, Capability.CONFIGURE}),
            Capability.CONFIGURE,
            False,
            None,
            False,
            "TENANT_MISMATCH",
        ),
    ],
)
def test_authorization_matrix(
    principal: str,
    identity_tenant: str | None,
    granted: frozenset[Capability],
    required: Capability,
    operator: bool,
    requester: str | None,
    allowed: bool,
    reason: str,
) -> None:
    decision = authorize(
        principal_id=principal,
        identity_tenant_id=identity_tenant,
        granted=granted,
        required=required,
        scope=SCOPE,
        platform_operator=operator,
        requester_id=requester,
    )

    assert decision.allowed is allowed
    assert decision.reason_code == reason
    assert decision.principal_id == principal
    assert decision.capability is required
    assert decision.scope == SCOPE


class _GrantReader:
    def __init__(self, grant: ScopedGrant | None, platform: bool = False) -> None:
        self.grant = grant
        self.platform = platform

    async def find_grant(self, **_kwargs):  # type: ignore[no-untyped-def]
        return self.grant

    async def has_platform_grant(self, **_kwargs):  # type: ignore[no-untyped-def]
        return self.platform


def _grant(**overrides):  # type: ignore[no-untyped-def]
    now = datetime.now(UTC)
    values = {
        "principal_id": "principal-operator",
        "capability": Capability.CONFIGURE,
        "tenant_id": SCOPE.tenant_id,
        "application_id": SCOPE.application_id or "",
        "environment": SCOPE.environment,
        "operation": SCOPE.operation,
        "valid_from": now - timedelta(minutes=1),
        "expires_at": now + timedelta(minutes=1),
        "revoked_at": None,
        "policy_version": "policy-v1",
        "issuer_id": "principal-issuer",
    }
    values.update(overrides)
    return ScopedGrant(**values)


@pytest.mark.asyncio
async def test_scoped_grant_authorizes_matching_verified_principal_and_scope() -> None:
    decision = await authorize_scoped(
        principal_id="principal-operator",
        identity_tenant_id=SCOPE.tenant_id,
        required=Capability.CONFIGURE,
        scope=SCOPE,
        policy_version="policy-v1",
        grant_reader=_GrantReader(_grant()),
    )

    assert decision.allowed
    assert decision.reason_code == "ALLOWED"


@pytest.mark.parametrize(
    ("grant", "policy", "reason"),
    [
        (_grant(revoked_at=datetime.now(UTC)), "policy-v1", "GRANT_INACTIVE"),
        (_grant(policy_version="old"), "policy-v1", "POLICY_VERSION_STALE"),
        (_grant(environment="production"), "policy-v1", "ENVIRONMENT_MISMATCH"),
        (_grant(operation="tenant:cleanup"), "policy-v1", "OPERATION_MISMATCH"),
        (_grant(tenant_id="tenant-synthetic-b"), "policy-v1", "TENANT_MISMATCH"),
    ],
)
@pytest.mark.asyncio
async def test_scoped_grant_fails_closed_for_stale_or_mismatched_grant(
    grant: ScopedGrant, policy: str, reason: str
) -> None:
    decision = await authorize_scoped(
        principal_id="principal-operator",
        identity_tenant_id=SCOPE.tenant_id,
        required=Capability.CONFIGURE,
        scope=SCOPE,
        policy_version=policy,
        grant_reader=_GrantReader(grant),
    )

    assert not decision.allowed
    assert decision.reason_code == reason


@pytest.mark.asyncio
async def test_platform_scope_is_explicit_and_obeys_separation_of_duties() -> None:
    decision = await authorize_scoped(
        principal_id="principal-operator",
        identity_tenant_id=None,
        required=Capability.APPROVE,
        scope=SCOPE,
        policy_version="policy-v1",
        grant_reader=_GrantReader(None, platform=True),
        requester_id="principal-operator",
    )

    assert not decision.allowed
    assert decision.reason_code == "SEPARATION_OF_DUTIES"


@pytest.mark.asyncio
async def test_platform_grant_does_not_depend_on_tenant_claim() -> None:
    decision = await authorize_scoped(
        principal_id="principal-operator",
        identity_tenant_id=None,
        required=Capability.CONFIGURE,
        scope=SCOPE,
        policy_version="policy-v1",
        grant_reader=_GrantReader(None, platform=True),
    )

    assert decision.allowed
