"""D1 capability authorization matrix for tenant onboarding."""

import pytest

from investigation_agent_platform.application.onboarding.authorization import (
    AuthorizationScope,
    Capability,
    authorize,
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
