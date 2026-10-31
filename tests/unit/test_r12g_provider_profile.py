"""R12G: a construction-time provider profile applies to every tenant.

The gateway accepts the attached profile only when it belongs to the
request's tenant and application; otherwise it falls through to the
repository lookup. Latent in the production wiring (no caller passes a
profile today) — pinned so a future caller cannot silently cross tenants.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _gateway(profile_repo):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.evidence.gateway import (
        AsyncEvidenceGateway,
    )

    gateway = AsyncEvidenceGateway.__new__(AsyncEvidenceGateway)
    gateway._profile_repo = profile_repo
    return gateway


def _provider(profile):  # type: ignore[no-untyped-def]
    return SimpleNamespace(profile=profile)


@pytest.mark.asyncio
async def test_r12g_t01_other_tenants_static_profile_is_ignored() -> None:
    from unittest.mock import AsyncMock

    tenant_a_profile = SimpleNamespace(tenant_id="tenant-a", id="app-1")
    tenant_b_profile = SimpleNamespace(tenant_id="tenant-b", id="app-1")
    repo = SimpleNamespace(
        get_by_application_id=AsyncMock(return_value=tenant_b_profile)
    )
    gateway = _gateway(repo)
    result = await gateway._effective_profile(
        _provider(tenant_a_profile), "tenant-b", "app-1"
    )
    assert result is tenant_b_profile
    repo.get_by_application_id.assert_awaited_once_with("tenant-b", "app-1")


@pytest.mark.asyncio
async def test_r12g_t02_matching_profile_is_accepted() -> None:
    from unittest.mock import AsyncMock

    profile = SimpleNamespace(tenant_id="tenant-a", id="app-1")
    repo = SimpleNamespace(get_by_application_id=AsyncMock())
    gateway = _gateway(repo)
    assert await gateway._effective_profile(_provider(profile), "tenant-a", "app-1") is profile
    repo.get_by_application_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_r12g_t02b_application_mismatch_falls_through() -> None:
    from unittest.mock import AsyncMock

    profile = SimpleNamespace(tenant_id="tenant-a", id="app-1")
    fallback = SimpleNamespace(tenant_id="tenant-a", id="app-2")
    repo = SimpleNamespace(get_by_application_id=AsyncMock(return_value=fallback))
    gateway = _gateway(repo)
    assert (
        await gateway._effective_profile(_provider(profile), "tenant-a", "app-2")
        is fallback
    )
