"""R6 enforcement: authorizer, registry, gateway, service, activity, fixture."""

from __future__ import annotations

import pytest

from investigation_agent_platform.domain.investigation.models import ActionType
from investigation_agent_platform.domain.profile.capability_policy import DenyReason
from investigation_agent_platform.domain.profile.models import (
    ApplicationProfile,
    InvestigationConfig,
    ProfileStatus,
    Service,
    ServiceCode,
)


def _profile(
    evidence_types,
    capabilities,
    *,
    status=ProfileStatus.ACTIVE,
    version=2,
) -> ApplicationProfile:
    return ApplicationProfile(
        id="app1",
        tenant_id="t1",
        name="n",
        description="d",
        environment="production",
        version=version,
        status=status,
        investigation=InvestigationConfig(enabledEvidenceTypes=list(evidence_types)),
        allowed_capabilities=list(capabilities),
        services=[
            Service(
                name="a",
                code=ServiceCode(org="org", repos=["a"], source_roots=["src"]),
            )
        ],
    )


class _Repos:
    def __init__(self, revisions):  # type: ignore[no-untyped-def]
        self._revisions = revisions

    async def list_revisions(self, tenant_id, application_id):  # type: ignore[no-untyped-def]
        assert (tenant_id, application_id) == ("t1", "app1")
        return self._revisions


def _authorizer(profile):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.infrastructure.security.profile_authorizer import (
        ProfileBasedActionAuthorizer,
    )

    revisions = [] if profile is None else [profile]
    return ProfileBasedActionAuthorizer(_Repos(revisions))


def test_r6_t02_get_code_needs_source_code():
    import asyncio

    async def go():  # type: ignore[no-untyped-def]
        authorizer = _authorizer(_profile(["SOURCE_CODE"], ["GET_CODE"]))
        assert await authorizer.authorize_action(
            "t1", "GET_CODE", "x", {"application_id": "app1"}
        )
        assert await authorizer.decide("t1", "GET_CODE", "x", {"application_id": "app1"})
        legacy = _authorizer(_profile(["COMMIT_HISTORY"], ["GET_CODE"]))
        assert not await legacy.authorize_action(
            "t1", "GET_CODE", "x", {"application_id": "app1"}
        )
        decision = await legacy.decide("t1", "GET_CODE", "x", {"application_id": "app1"})
        assert not decision.allowed
        assert decision.reason == DenyReason.EVIDENCE_TYPE_NOT_ENABLED

    asyncio.run(go())


def test_r6_t04_second_reason_is_live():
    import asyncio

    from investigation_agent_platform.infrastructure.security.profile_authorizer import (
        ProfileBasedCapabilityRegistry,
    )

    async def go():  # type: ignore[no-untyped-def]
        registry = ProfileBasedCapabilityRegistry(
            _Repos([_profile(["SOURCE_CODE"], [])])
        )
        assert not await registry.is_capability_enabled("t1", "SEARCH_LOGS", "app1")
        decision = await registry.decide("t1", "SEARCH_LOGS", "app1")
        assert not decision.allowed
        assert decision.reason == DenyReason.CAPABILITY_NOT_DECLARED

    asyncio.run(go())


def test_r6_t05_registry_requires_application_context():
    import asyncio

    from investigation_agent_platform.infrastructure.security.profile_authorizer import (
        ProfileBasedCapabilityRegistry,
    )

    async def go():  # type: ignore[no-untyped-def]
        registry = ProfileBasedCapabilityRegistry(_Repos([_profile(["LOG"], ["SEARCH_LOGS"])]))
        assert not await registry.is_capability_enabled("t1", "SEARCH_LOGS", None)
        decision = await registry.decide("t1", "SEARCH_LOGS", None)
        assert decision.reason == DenyReason.NO_APPLICATION
        empty = ProfileBasedCapabilityRegistry(_Repos([]))
        decision = await empty.decide("t1", "SEARCH_LOGS", "app1")
        assert decision.reason == DenyReason.NO_ACTIVE_PROFILE

    asyncio.run(go())


def test_r6_t06_registry_ignores_non_active():
    import asyncio

    from investigation_agent_platform.infrastructure.security.profile_authorizer import (
        ProfileBasedCapabilityRegistry,
    )

    async def go():  # type: ignore[no-untyped-def]
        revisions = [
            _profile(["SOURCE_CODE"], ["SEARCH_LOGS"], status=ProfileStatus.DRAFT, version=2),
            _profile(["SOURCE_CODE"], [], status=ProfileStatus.ACTIVE, version=1),
        ]
        registry = ProfileBasedCapabilityRegistry(_Repos(revisions))
        assert not await registry.is_capability_enabled("t1", "SEARCH_LOGS", "app1")

    asyncio.run(go())


def _gateway(stub_allowed, profile):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.evidence.gateway import (
        AsyncEvidenceGateway,
    )

    gateway = AsyncEvidenceGateway.__new__(AsyncEvidenceGateway)
    if stub_allowed is None:
        gateway._authorizer = None
    else:
        from unittest.mock import AsyncMock

        gateway._authorizer = AsyncMock()
        gateway._authorizer.authorize_action.return_value = stub_allowed
    from investigation_agent_platform.infrastructure.security.profile_authorizer import (
        ProfileBasedCapabilityRegistry,
    )

    gateway._capability_registry = ProfileBasedCapabilityRegistry(_Repos([profile]))
    return gateway


def test_r6_t07_gateway_defense_in_depth():
    import asyncio

    from investigation_agent_platform.domain.common.exceptions import (
        SecurityPolicyViolationException,
    )

    async def go():  # type: ignore[no-untyped-def]
        gateway = _gateway(True, _profile(["SOURCE_CODE"], []))
        with pytest.raises(SecurityPolicyViolationException, match="is disabled"):
            await gateway._enforce_authorization(
                "t1", "SEARCH_LOGS", "x", application_id="app1"
            )

    asyncio.run(go())


def test_r6_t08_platform_operations():
    import asyncio

    async def go():  # type: ignore[no-untyped-def]
        gateway = _gateway(None, _profile(["SOURCE_CODE"], []))
        await gateway._enforce_authorization(
            "t1", "QUERY_CODE_TOPOLOGY", "f", application_id="app1"
        )
        await gateway._enforce_authorization(
            "t1", "RESOLVE_DOMAIN_ATTRIBUTION", "f", application_id="app1"
        )
        thin = _gateway(None, _profile([], []))
        from investigation_agent_platform.domain.common.exceptions import (
            SecurityPolicyViolationException,
        )

        with pytest.raises(SecurityPolicyViolationException):
            await thin._enforce_authorization(
                "t1", "QUERY_CODE_TOPOLOGY", "f", application_id="app1"
            )

    asyncio.run(go())


def test_r6_t10_typed_reason_rides_exception_and_outcome():
    import asyncio

    from investigation_agent_platform.application.investigation.services import (
        ExecuteActionService,
    )
    from investigation_agent_platform.domain.common.exceptions import (
        SecurityPolicyViolationException,
    )
    from investigation_agent_platform.domain.investigation.models import InvestigationAction

    async def go():  # type: ignore[no-untyped-def]
        authorizer = _authorizer(_profile(["SOURCE_CODE"], []))
        service = ExecuteActionService.__new__(ExecuteActionService)
        service.authorizer = authorizer
        from unittest.mock import AsyncMock

        service.capability_registry = AsyncMock()
        action = InvestigationAction(
            action_id="00000000-0000-0000-0000-000000000000",
            action_type=ActionType.GET_CODE,
            parameters={},
            execution_hash="h",
        )
        with pytest.raises(SecurityPolicyViolationException) as exc:
            await service.authorize(
                "t1",
                "00000000-0000-0000-0000-000000000001",
                action,
                "p",
                application_id="app1",
            )
        assert exc.value.details.get("deny_reason") == "capability_not_declared"

    asyncio.run(go())


def test_r6_t11_effective_list_is_profile_derived():
    import asyncio

    from investigation_agent_platform.infrastructure.security.profile_authorizer import (
        ProfileBasedCapabilityRegistry,
    )

    async def go():  # type: ignore[no-untyped-def]
        registry = ProfileBasedCapabilityRegistry(
            _Repos([_profile(["LOG", "SOURCE_CODE"], ["SEARCH_LOGS", "GET_CODE"])])
        )
        allowed = await registry.list_allowed_capabilities("t1", "app1")
        assert allowed == ["GET_CODE", "SEARCH_LOGS"]
        empty = ProfileBasedCapabilityRegistry(_Repos([]))
        assert await empty.list_allowed_capabilities("t1", "app1") == []

    asyncio.run(go())


def test_r6_t12_dev_default_still_works():
    import asyncio

    from investigation_agent_platform.api.dependencies import _DEFAULT_PROFILE
    from investigation_agent_platform.domain.profile.capability_policy import (
        capability_available,
    )

    async def go():  # type: ignore[no-untyped-def]
        assert _DEFAULT_PROFILE.status == ProfileStatus.ACTIVE
        for capability in _DEFAULT_PROFILE.allowed_capabilities:
            try:
                action = ActionType(capability)
            except ValueError:
                continue
            assert capability_available(_DEFAULT_PROFILE, action).allowed, capability

    asyncio.run(go())
