"""R2 code half: legacy index-scope collision guard and per-service narrowing.

The raw wildcard shape is intentionally unchanged until the ops delimiter
rename; the strict xfail in test_tranche5_tenancy pins that deferred half.
"""

from __future__ import annotations

import fnmatch
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock

import pytest

from investigation_agent_platform.domain.common.exceptions import SecurityPolicyViolationException
from investigation_agent_platform.domain.evidence.requests import RuntimeEvidenceRequest, TimeRange
from investigation_agent_platform.domain.observability.mapping import generic_ecs_mapping
from investigation_agent_platform.domain.profile.models import ApplicationProfile
from investigation_agent_platform.infrastructure.evidence.runtime.elastic import (
    AsyncElasticAdapter,
    ElasticAdapterSettings,
)
from investigation_agent_platform.infrastructure.evidence.runtime.validation import (
    build_index_pattern,
    dash_boundary_siblings,
    resolve_index_pattern,
)
from investigation_agent_platform.infrastructure.persistence.onboarding_registry_repository import (
    SqlAlchemyOnboardingRegistryRepository,
)


def _matches(index: str, pattern: str) -> bool:
    return any(fnmatch.fnmatchcase(index, part) for part in pattern.split(","))


SIBLING_INDEX = "logs-n-a-b-svc-prod-1"  # tenant "n-a-b" (service "svc")


class TestPerServiceNarrowing:
    def test_named_services_get_exact_patterns_for_a_dashed_tenant(self) -> None:
        pattern = build_index_pattern("n-a", "prod", services=["svc", "api"])
        assert pattern == "logs-n-a-api-prod-*,logs-n-a-svc-prod-*"

    def test_narrowed_pattern_cannot_reach_a_sibling_tenants_index(self) -> None:
        assert _matches(SIBLING_INDEX, build_index_pattern("n-a", "prod"))  # wildcard: collides
        assert not _matches(SIBLING_INDEX, build_index_pattern("n-a", "prod", services=["svc"]))

    def test_narrowing_needs_no_sibling_list_and_never_raises_on_collision(self) -> None:
        pattern = build_index_pattern(
            "n-a", "prod", services=["svc"], known_tenant_ids=["n-a", "n-a-b"]
        )
        assert pattern == "logs-n-a-svc-prod-*"

    def test_dash_free_tenant_keeps_the_wildcard_even_when_services_are_named(self) -> None:
        # Narrowing is scoped to dashed tenants (see the rationale in validation.py).
        assert build_index_pattern("acme", "prod", services=["svc"]) == "logs-acme-*-prod-*"
        with pytest.raises(SecurityPolicyViolationException):
            build_index_pattern(
                "acme", "prod", services=["svc"], known_tenant_ids=["acme", "acme-eu"]
            )

    @pytest.mark.parametrize("bad", ["a*", "a,b", "a b", "", "-lead", "a?"])
    def test_non_token_service_falls_back_to_the_wildcard_never_interpolated(
        self, bad: str
    ) -> None:
        pattern = build_index_pattern("n-a", "prod", services=["svc", bad])
        assert pattern == "logs-n-a-*-prod-*"


class TestSiblingGuard:
    def test_raises_when_another_tenant_extends_this_one_at_the_dash(self) -> None:
        with pytest.raises(SecurityPolicyViolationException) as exc:
            build_index_pattern("acme", "prod", known_tenant_ids=["acme", "acme-eu"])
        assert "acme-eu" not in str(exc.value)

    def test_raises_in_the_reverse_direction(self) -> None:
        with pytest.raises(SecurityPolicyViolationException):
            build_index_pattern("acme-eu", "prod", known_tenant_ids=["acme", "acme-eu"])

    def test_does_not_over_block_unrelated_or_non_dash_siblings(self) -> None:
        siblings = ["n-batt", "acmeeu", "other-co", "Acme-EU", "n-a"]
        assert build_index_pattern("n-a", "prod", known_tenant_ids=siblings) == (
            "logs-n-a-*-prod-*"
        )
        assert build_index_pattern("acme", "prod", known_tenant_ids=siblings) == (
            "logs-acme-*-prod-*"
        )

    def test_without_a_sibling_list_behavior_is_unchanged(self) -> None:
        assert build_index_pattern("acme", "prod") == "logs-acme-*-prod-*"
        assert build_index_pattern("tenant-a", "production") == "logs-tenant-a-*-production-*"

    def test_dash_boundary_siblings_is_exact_case_and_excludes_self(self) -> None:
        assert dash_boundary_siblings("acme", ["acme", "acme-eu", "ACME-eu"]) == ["acme-eu"]

    def test_resolve_forwards_services_and_siblings_on_the_legacy_path(self) -> None:
        mapping = generic_ecs_mapping()
        assert mapping.legacy_index_synthesis
        assert resolve_index_pattern(
            mapping, "n-a", "prod", services=["svc"], known_tenant_ids=["n-a-b"]
        ) == ("logs-n-a-svc-prod-*")
        with pytest.raises(SecurityPolicyViolationException):
            resolve_index_pattern(mapping, "acme", "prod", known_tenant_ids=["acme-eu"])


def _request(**overrides: Any) -> RuntimeEvidenceRequest:
    now = datetime.now(UTC)
    kwargs: dict[str, Any] = {
        "environment": "prod",
        "time_range": TimeRange(start_time=now - timedelta(minutes=5), end_time=now),
        "limit": 10,
    }
    kwargs.update(overrides)
    return RuntimeEvidenceRequest(**kwargs)


def _profile(tenant: str) -> ApplicationProfile:
    return ApplicationProfile(
        id="app", tenant_id=tenant, name="n", description="d", environment="prod"
    )


def _adapter(client: Any, known: Any = None) -> AsyncElasticAdapter:
    return AsyncElasticAdapter(
        client=client,
        settings=ElasticAdapterSettings(cursor_signing_key=b"test-signing-key"),
        known_tenant_ids=known,
    )


def _client() -> MagicMock:
    client = MagicMock()
    client.search = AsyncMock(return_value={"hits": {"hits": [], "total": {"value": 0}}})
    return client


class TestAdapterRouting:
    async def test_colliding_wildcard_is_refused_before_any_search(self) -> None:
        client = _client()
        adapter = _adapter(client, AsyncMock(return_value=["acme", "acme-eu"]))
        with pytest.raises(SecurityPolicyViolationException):
            await adapter.search_runtime_evidence(
                "acme", uuid.uuid4(), _request(), _profile("acme")
            )
        client.search.assert_not_called()

    async def test_named_services_search_the_narrowed_scope_for_a_dashed_tenant(self) -> None:
        client = _client()
        adapter = _adapter(client, AsyncMock(return_value=["n-a", "n-a-b"]))
        await adapter.search_runtime_evidence(
            "n-a", uuid.uuid4(), _request(services=["svc"]), _profile("n-a")
        )
        assert client.search.call_args.kwargs["index"] == "logs-n-a-svc-prod-*"

    async def test_no_provider_keeps_the_historical_wildcard(self) -> None:
        client = _client()
        await _adapter(client).search_runtime_evidence(
            "acme", uuid.uuid4(), _request(), _profile("acme")
        )
        assert client.search.call_args.kwargs["index"] == "logs-acme-*-prod-*"

    async def test_non_colliding_tenant_is_served_and_ids_are_cached(self) -> None:
        client = _client()
        known = AsyncMock(return_value=["n-a", "n-batt"])
        adapter = _adapter(client, known)
        for _ in range(2):
            await adapter.search_runtime_evidence("n-a", uuid.uuid4(), _request(), _profile("n-a"))
        assert known.await_count == 1
        assert client.search.call_args.kwargs["index"] == "logs-n-a-*-prod-*"

    async def test_lookup_failure_fails_closed(self) -> None:
        client = _client()
        adapter = _adapter(client, AsyncMock(side_effect=RuntimeError("registry down")))
        with pytest.raises(RuntimeError):
            await adapter.search_runtime_evidence(
                "acme", uuid.uuid4(), _request(), _profile("acme")
            )
        client.search.assert_not_called()


class _Session:
    """Fake session: each ``scalars`` call answers the next source (registry, investigations, profiles)."""

    def __init__(self, per_source: list[list[str]]) -> None:
        self._per_source = list(per_source)
        self.queries = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def scalars(self, _stmt: object) -> list[str]:
        self.queries += 1
        return self._per_source.pop(0)


async def test_known_tenant_ids_is_the_deduplicated_union_of_all_three_sources() -> None:
    session = _Session([["acme"], ["n-batt", "acme", "n-a"], ["n-a", "legacy-co"]])
    repo = SqlAlchemyOnboardingRegistryRepository(lambda: session)
    assert await repo.list_known_tenant_ids() == ["acme", "legacy-co", "n-a", "n-batt"]
    assert session.queries == 3


async def test_known_tenant_ids_finds_a_dashed_sibling_that_is_not_registered() -> None:
    # Registry is empty; the sibling exists only in investigations. The guard must still see it.
    session = _Session([[], ["acme-eu"], []])
    repo = SqlAlchemyOnboardingRegistryRepository(lambda: session)
    known = await repo.list_known_tenant_ids()
    with pytest.raises(SecurityPolicyViolationException):
        build_index_pattern("acme", "prod", known_tenant_ids=known)
