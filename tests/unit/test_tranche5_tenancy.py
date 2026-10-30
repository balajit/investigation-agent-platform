# tests/unit/test_tranche5_tenancy.py
"""Tranche 5 tenancy and authorization fixes (review R2, R3, R4, R5)."""

from __future__ import annotations

import fnmatch
from unittest.mock import AsyncMock, MagicMock

import pytest

from investigation_agent_platform.api.dependencies import _DEFAULT_PROFILE
from investigation_agent_platform.api.errors import ApiError
from investigation_agent_platform.api.tenant import (
    DEV_ENVIRONMENTS,
    AuthConfigurationError,
    _resolve_verified_identity,
)
from investigation_agent_platform.domain.profile.models import ProfileStatus
from investigation_agent_platform.infrastructure.security.profile_authorizer import (
    ProfileBasedActionAuthorizer,
)

_JWKS = {
    "IAP_AUTH_JWKS_URL": "https://issuer.example.com/.well-known/jwks.json",
    "IAP_AUTH_ISSUER": "test-issuer",
}


def _env(monkeypatch: pytest.MonkeyPatch, environment: str, *, jwks: bool) -> None:
    monkeypatch.setenv("IAP_ENVIRONMENT", environment)
    for key, value in _JWKS.items():
        if jwks:
            monkeypatch.setenv(key, value)
        else:
            monkeypatch.delenv(key, raising=False)


class TestR3HeaderOnlyAuthIsDevOnly:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("environment", sorted(DEV_ENVIRONMENTS) + ["Development", " LOCAL "])
    async def test_named_dev_environment_without_jwks_accepts_header(
        self, monkeypatch: pytest.MonkeyPatch, environment: str
    ) -> None:
        _env(monkeypatch, environment, jwks=False)
        identity = await _resolve_verified_identity("tenant-a", None)
        assert identity.tenant_id == "tenant-a"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "environment", ["production", "prod", "live", "staging", "prd", "", "development2"]
    )
    async def test_any_other_environment_refuses_header_only(
        self, monkeypatch: pytest.MonkeyPatch, environment: str
    ) -> None:
        # No JWKS configured: misconfiguration must be loud, never a header login.
        _env(monkeypatch, environment, jwks=False)
        with pytest.raises(AuthConfigurationError):
            await _resolve_verified_identity("tenant-a", None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("environment", ["prod", "live", "staging", "development", "local"])
    async def test_configured_jwt_verification_never_falls_back_to_header(
        self, monkeypatch: pytest.MonkeyPatch, environment: str
    ) -> None:
        _env(monkeypatch, environment, jwks=True)
        with pytest.raises(ApiError) as info:
            await _resolve_verified_identity("tenant-a", None)
        assert info.value.code == "AUTH_TOKEN_REQUIRED"

    def test_allowlist_is_explicit(self) -> None:
        assert "production" not in DEV_ENVIRONMENTS
        assert "prod" not in DEV_ENVIRONMENTS
        assert "staging" not in DEV_ENVIRONMENTS


def _profile(*, status: ProfileStatus, version: int, capabilities: list[str] | None):
    kwargs: dict = {"status": status, "version": version}
    if capabilities is not None:
        kwargs["allowed_capabilities"] = capabilities
    return _DEFAULT_PROFILE.model_copy(update=kwargs)


def _authorizer(revisions: list) -> ProfileBasedActionAuthorizer:
    repo = MagicMock()
    repo.list_revisions = AsyncMock(return_value=revisions)
    return ProfileBasedActionAuthorizer(repo)


_CTX = {"application_id": "example-app"}


class TestR4UndeclaredCapabilitiesDeny:
    @pytest.mark.asyncio
    async def test_empty_capability_list_denies(self) -> None:
        profile = _profile(status=ProfileStatus.ACTIVE, version=1, capabilities=[])
        assert await _authorizer([profile]).authorize_action("t", "SEARCH_LOGS", "", _CTX) is False

    @pytest.mark.asyncio
    async def test_declared_capability_is_allowed(self) -> None:
        profile = _profile(status=ProfileStatus.ACTIVE, version=1, capabilities=["SEARCH_LOGS"])
        assert await _authorizer([profile]).authorize_action("t", "SEARCH_LOGS", "", _CTX) is True

    @pytest.mark.asyncio
    async def test_undeclared_capability_is_denied_even_if_evidence_type_enabled(self) -> None:
        profile = _profile(status=ProfileStatus.ACTIVE, version=1, capabilities=["SEARCH_LOGS"])
        assert await _authorizer([profile]).authorize_action("t", "QUERY_STATE", "", _CTX) is False

    @pytest.mark.asyncio
    async def test_internal_reasoning_actions_need_no_capability(self) -> None:
        profile = _profile(status=ProfileStatus.ACTIVE, version=1, capabilities=[])
        assert await _authorizer([profile]).authorize_action("t", "CONCLUDE", "", {}) is True

    def test_dev_default_profile_declares_its_capabilities(self) -> None:
        assert _DEFAULT_PROFILE.allowed_capabilities, "dev default would deny every action"


class TestR5OnlyActiveRevisionAuthorizes:
    @pytest.mark.asyncio
    async def test_newer_draft_does_not_outrank_reviewed_active(self) -> None:
        active = _profile(status=ProfileStatus.ACTIVE, version=1, capabilities=["SEARCH_LOGS"])
        draft = _profile(status=ProfileStatus.DRAFT, version=2, capabilities=["SEARCH_LOGS", "GET_CODE"])
        authorizer = _authorizer([draft, active])  # newest first
        assert await authorizer.authorize_action("t", "SEARCH_LOGS", "", _CTX) is True
        # The unreviewed draft's extra capability is NOT granted.
        assert await authorizer.authorize_action("t", "GET_CODE", "", _CTX) is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [ProfileStatus.DRAFT, ProfileStatus.ARCHIVED, ProfileStatus.DEPRECATED])
    async def test_non_active_only_denies(self, status: ProfileStatus) -> None:
        profile = _profile(status=status, version=1, capabilities=["SEARCH_LOGS"])
        assert await _authorizer([profile]).authorize_action("t", "SEARCH_LOGS", "", _CTX) is False

    @pytest.mark.asyncio
    async def test_no_revisions_denies(self) -> None:
        assert await _authorizer([]).authorize_action("t", "SEARCH_LOGS", "", _CTX) is False


class TestR2IndexPatternTenantIsolation:
    """Reproduced, not yet fixable in code alone (needs an operator decision)."""

    @staticmethod
    def _pattern(tenant: str, environment: str = "prod") -> str:
        from investigation_agent_platform.infrastructure.evidence.runtime.validation import (
            build_index_pattern,
        )

        return build_index_pattern(tenant, environment)

    def test_probe_reproduces_the_prefix_collision(self) -> None:
        # Index naming convention: logs-{tenant}-{service}-{env}-{n}.
        # Tenant "acme-eu" with service "svc" owns this index ...
        sibling_index = "logs-acme-eu-svc-prod-1"
        # ... and tenant "acme"'s wildcard also matches it.
        assert fnmatch.fnmatchcase(sibling_index, self._pattern("acme"))

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "R2: needs a reserved tenant/service delimiter in the index naming "
            "convention (re-index + filebeat change) or forbidding '-' in tenant IDs "
            "(breaks existing tenants such as n-a); operator decision pending"
        ),
    )
    def test_tenant_pattern_must_not_match_a_sibling_tenants_index(self) -> None:
        assert not fnmatch.fnmatchcase("logs-acme-eu-svc-prod-1", self._pattern("acme"))
