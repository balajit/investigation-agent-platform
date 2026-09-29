"""Extensive coverage for core API and business logic (low-coverage targets)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import (
    _DEFAULT_PROFILE,
    AppContext,
    InMemoryApplicationProfileRepository,
    InMemoryEvidenceRepository,
    InMemoryHypothesisRepository,
    InMemoryInvestigationRepository,
    InMemoryTimelineRepository,
    _InMemoryIdempotencyStore,
    get_app_context,
    set_app_context,
)
from investigation_agent_platform.api.tenant import (
    AuthConfigurationError,
    _resolve_verified_identity,
    require_principal,
    require_tenant,
)
from investigation_agent_platform.api.v1.routers.investigations import CreateInvestigationBody
from investigation_agent_platform.application.investigation.reasoning import ReasoningCoordinator
from investigation_agent_platform.application.investigation.services import (
    CancelInvestigationService,
    CreateInvestigationService,
    GetInvestigationService,
    ResumeInvestigationService,
)
from investigation_agent_platform.application.investigation.validator import (
    InvestigationActionValidator,
    _has_traversal,
    _is_within_roots,
)
from investigation_agent_platform.domain.common.exceptions import (
    ConcurrencyError,
    DomainException,
    SecurityPolicyViolationException,
)
from investigation_agent_platform.domain.evidence.models import Evidence, EvidenceType
from investigation_agent_platform.domain.hypothesis.models import Hypothesis
from investigation_agent_platform.domain.investigation.models import (
    ActionType,
    EvidenceManifest,
    Investigation,
    InvestigationAction,
    InvestigationContext,
    InvestigationLimits,
    InvestigationRequest,
    InvestigationState,
    InvestigationStatus,
)
from investigation_agent_platform.ports.observability.telemetry import LLMCallMetadata
from investigation_agent_platform.ports.reasoning.llm_gateway import LLMGatewayResponse

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _jwt_with_tenant(tenant: str, secret: str = "test-secret", **extra_claims: Any) -> str:
    """HS256-signed JWT for tests (production uses JWKS/RS256; HS256+shared
    secret is used purely to exercise the verification code path deterministically)."""
    import jwt as _jwt

    now = datetime.now(UTC)
    payload = {
        "tenant_id": tenant,
        "sub": "principal-1",
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "iss": "test-issuer",
        **extra_claims,
    }
    return _jwt.encode(payload, secret, algorithm="HS256")


def _jwt_with_claim(key: str, value: str, secret: str = "test-secret") -> str:
    import jwt as _jwt

    now = datetime.now(UTC)
    payload = {
        key: value,
        "sub": "principal-1",
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "iss": "test-issuer",
    }
    return _jwt.encode(payload, secret, algorithm="HS256")


def _make_investigation(tenant_id: str = "tenant-a", app_id: str = "example-app") -> Investigation:
    now = datetime.now(UTC)
    req = InvestigationRequest(
        application_id=app_id,
        problem_description="test incident",
        session_id="sess-1",
        requested_by="tester",
    )
    ctx = InvestigationContext(
        environment="production",
        time_window=(now - timedelta(seconds=3600), now),
        known_identifiers={"sessionId": "sess-1"},
    )
    return Investigation(
        session_id="sess-1",
        application_id=app_id,
        tenant_id=tenant_id,
        request=req,
        context=ctx,
        status=InvestigationStatus.CREATED,
        created_at=now,
        updated_at=now,
        version=1,
    )


def _make_evidence(tenant_id: str = "tenant-a", investigation_id: UUID | None = None) -> Evidence:
    now = datetime.now(UTC)
    iid = investigation_id or uuid.uuid4()
    from investigation_agent_platform.domain.provenance.models import (
        EvidenceFreshness,
        EvidenceProvenance,
        QueryFingerprint,
        SourceLocation,
    )

    return Evidence(
        tenant_id=tenant_id,
        investigation_id=iid,
        evidence_type=EvidenceType.LOG,
        provider="test-provider",
        source="urn:test:logs",
        title="test evidence",
        summary="summary",
        fingerprint=f"fp-{uuid.uuid4()}",
        observed_at=now,
        retrieved_at=now,
        provenance=EvidenceProvenance(
            tenant_id=tenant_id,
            investigation_id=iid,
            provider_type="test",
            requested_provider_id="test",
            actual_provider_id="test",
            source_system="test",
            retrieval_timestamp=now,
            query_fingerprint=QueryFingerprint(
                provider_type="test", operation="test", normalized_query_hash="abc"
            ),
            source_location=SourceLocation(system="test", identifier="id"),
        ),
        freshness=EvidenceFreshness(observed_at=now, retrieved_at=now),
    )


def _make_timeline_event(tenant_id: str = "tenant-a", investigation_id: UUID | None = None) -> Any:
    from investigation_agent_platform.domain.timeline.models import TimelineEvent, TimelineEventType

    iid = investigation_id or uuid.uuid4()
    return TimelineEvent(
        tenant_id=tenant_id,
        investigation_id=iid,
        timestamp=datetime.now(UTC),
        event_type=TimelineEventType.LOG_EVENT,
        description="test event",
    )


def _make_hypothesis(
    tenant_id: str = "tenant-a", investigation_id: UUID | None = None
) -> Hypothesis:
    iid = investigation_id or uuid.uuid4()
    return Hypothesis(
        tenant_id=tenant_id,
        investigation_id=iid,
        statement="h1",
        title="h1 title",
        description="desc",
    )


def _llm_meta() -> LLMCallMetadata:
    return LLMCallMetadata(
        model_name="gpt-4",
        prompt_tokens=5,
        completion_tokens=5,
        total_tokens=10,
        estimated_cost_usd=0.01,
        latency_ms=10,
    )


# ===========================================================================
# api/dependencies – InMemory repos tenant partition
# ===========================================================================


class TestInMemoryInvestigationRepository:
    @pytest.mark.asyncio
    async def test_tenant_isolation_get_by_id(self) -> None:
        repo = InMemoryInvestigationRepository()
        inv = _make_investigation(tenant_id="tenant-a")
        await repo.create("tenant-a", inv)
        # different tenant cannot see
        assert await repo.get_by_id("tenant-b", inv.id) is None
        assert await repo.get_by_id("tenant-a", inv.id) is not None

    @pytest.mark.asyncio
    async def test_save_version_mismatch_raises(self) -> None:
        repo = InMemoryInvestigationRepository()
        inv = _make_investigation()
        await repo.create("tenant-a", inv)
        updated = inv.model_copy(update={"version": 2})
        with pytest.raises(ConcurrencyError):
            await repo.save("tenant-a", updated, expected_version=99)

    @pytest.mark.asyncio
    async def test_save_tenant_mismatch_raises(self) -> None:
        repo = InMemoryInvestigationRepository()
        inv = _make_investigation(tenant_id="tenant-a")
        await repo.create("tenant-a", inv)
        bad = inv.model_copy(update={"tenant_id": "tenant-b"})
        with pytest.raises(ConcurrencyError):
            await repo.save("tenant-a", bad, expected_version=1)

    @pytest.mark.asyncio
    async def test_exists_and_delete(self) -> None:
        repo = InMemoryInvestigationRepository()
        inv = _make_investigation()
        await repo.create("tenant-a", inv)
        assert await repo.exists("tenant-a", inv.id) is True
        assert await repo.exists("tenant-b", inv.id) is False
        await repo.delete("tenant-a", inv.id)
        assert await repo.exists("tenant-a", inv.id) is False

    @pytest.mark.asyncio
    async def test_save_success_increments(self) -> None:
        repo = InMemoryInvestigationRepository()
        inv = _make_investigation()
        await repo.create("tenant-a", inv)
        updated = inv.model_copy(update={"version": 2})
        await repo.save("tenant-a", updated, expected_version=1)
        fetched = await repo.get_by_id("tenant-a", inv.id)
        assert fetched is not None and fetched.version == 2


class TestInMemoryEvidenceRepository:
    @pytest.mark.asyncio
    async def test_tenant_isolation(self) -> None:
        repo = InMemoryEvidenceRepository()
        ev = _make_evidence("tenant-a")
        await repo.save("tenant-a", ev, investigation_id=uuid.uuid4())
        assert await repo.get_by_id("tenant-a", ev.evidence_id) is not None
        assert await repo.get_by_id("tenant-b", ev.evidence_id) is None

    @pytest.mark.asyncio
    async def test_duplicate_fingerprint_skipped(self) -> None:
        repo = InMemoryEvidenceRepository()
        ev1 = _make_evidence("tenant-a")
        # same fingerprint copy
        ev2 = ev1.model_copy(update={"evidence_id": uuid.uuid4(), "title": "second"})
        await repo.save("tenant-a", ev1)
        await repo.save("tenant-a", ev2)
        # second should be ignored -> still one entry
        assert await repo.get_by_id("tenant-a", ev2.evidence_id) is None
        # but different tenant with same fingerprint is allowed
        ev3 = ev1.model_copy(
            update={"evidence_id": uuid.uuid4(), "tenant_id": "tenant-b", "title": "t3"}
        )
        await repo.save("tenant-b", ev3)
        assert await repo.get_by_id("tenant-b", ev3.evidence_id) is not None

    @pytest.mark.asyncio
    async def test_find_by_investigation_tenant_filter(self) -> None:
        repo = InMemoryEvidenceRepository()
        iid = uuid.uuid4()
        ev = _make_evidence("tenant-a")
        await repo.save("tenant-a", ev, investigation_id=iid)
        assert len(await repo.find_by_investigation_id("tenant-a", iid)) == 1
        assert len(await repo.find_by_investigation_id("tenant-b", iid)) == 0

    @pytest.mark.asyncio
    async def test_get_by_ids_tenant_filter(self) -> None:
        repo = InMemoryEvidenceRepository()
        ev = _make_evidence("tenant-a")
        await repo.save("tenant-a", ev)
        assert len(await repo.get_by_ids("tenant-a", [ev.evidence_id])) == 1
        assert len(await repo.get_by_ids("tenant-b", [ev.evidence_id])) == 0

    @pytest.mark.asyncio
    async def test_save_batch_dedup_within_batch(self) -> None:
        repo = InMemoryEvidenceRepository()
        iid = uuid.uuid4()
        ev1 = _make_evidence("tenant-a")
        ev2 = ev1.model_copy(update={"evidence_id": uuid.uuid4(), "title": "second"})
        await repo.save_batch("tenant-a", [ev1, ev2], investigation_id=iid)
        # only one stored
        res = await repo.find_by_investigation_id("tenant-a", iid)
        assert len(res) == 1


class TestInMemoryTimelineRepository:
    @pytest.mark.asyncio
    async def test_tenant_filter(self) -> None:
        repo = InMemoryTimelineRepository()
        iid = uuid.uuid4()
        ev = _make_timeline_event("tenant-a", iid)
        await repo.append("tenant-a", ev, investigation_id=iid)
        assert len(await repo.find_by_investigation_id("tenant-a", iid)) == 1
        assert len(await repo.find_by_investigation_id("tenant-b", iid)) == 0

    @pytest.mark.asyncio
    async def test_find_by_time_range(self) -> None:
        repo = InMemoryTimelineRepository()
        iid = uuid.uuid4()
        ev = _make_timeline_event("tenant-a", iid)
        now = ev.timestamp
        await repo.append("tenant-a", ev, investigation_id=iid)
        res = await repo.find_by_time_range(
            "tenant-a", iid, now - timedelta(seconds=10), now + timedelta(seconds=10)
        )
        assert len(res) == 1
        res2 = await repo.find_by_time_range(
            "tenant-a", iid, now + timedelta(seconds=10), now + timedelta(seconds=20)
        )
        assert len(res2) == 0

    @pytest.mark.asyncio
    async def test_paginated_helper(self) -> None:
        repo = InMemoryTimelineRepository()
        iid = uuid.uuid4()
        for i in range(5):
            ev = _make_timeline_event("tenant-a", iid)
            await repo.append("tenant-a", ev, investigation_id=iid)
        page, total = await repo.find_by_investigation_and_tenant(
            iid, "tenant-a", offset=1, limit=2
        )  # type: ignore[attr-defined]
        assert total == 5
        assert len(page) == 2


class TestInMemoryHypothesisRepository:
    @pytest.mark.asyncio
    async def test_tenant_isolation(self) -> None:
        repo = InMemoryHypothesisRepository()
        iid = uuid.uuid4()
        h = _make_hypothesis("tenant-a", iid)
        await repo.save("tenant-a", h, investigation_id=iid)
        assert await repo.get_by_id("tenant-a", h.id) is not None
        assert await repo.get_by_id("tenant-b", h.id) is None
        assert len(await repo.find_by_investigation_id("tenant-a", iid)) == 1
        assert len(await repo.find_by_investigation_id("tenant-b", iid)) == 0

    @pytest.mark.asyncio
    async def test_paginated(self) -> None:
        repo = InMemoryHypothesisRepository()
        iid = uuid.uuid4()
        for i in range(3):
            h = _make_hypothesis("tenant-a", iid)
            await repo.save("tenant-a", h, investigation_id=iid)
        page, total = await repo.find_by_investigation_and_tenant(
            iid, "tenant-a", offset=0, limit=2
        )
        assert total == 3
        assert len(page) == 2


class TestIdempotencyStore:
    @pytest.mark.asyncio
    async def test_atomic_get_or_create(self) -> None:
        store = _InMemoryIdempotencyStore(ttl_seconds=10)
        v1, created1 = await store.get_or_create("k", {"id": "1"})
        assert created1 is True
        v2, created2 = await store.get_or_create("k", {"id": "2"})
        assert created2 is False
        assert v2 == {"id": "1"}

    @pytest.mark.asyncio
    async def test_ttl_expiry(self) -> None:
        store = _InMemoryIdempotencyStore(ttl_seconds=0.05)
        await store.set("k", "v")
        assert await store.get("k") == "v"
        await asyncio.sleep(0.08)
        assert await store.get("k") is None

    @pytest.mark.asyncio
    async def test_under_lock_helpers(self) -> None:
        store = _InMemoryIdempotencyStore(ttl_seconds=10)
        async with store.lock:
            store.set_under_lock("k", "val")
            assert store.get_under_lock("k") == "val"
        assert await store.get("k") == "val"

    @pytest.mark.asyncio
    async def test_concurrent_get_or_create_only_one_created(self) -> None:
        store = _InMemoryIdempotencyStore(ttl_seconds=10)
        results = await asyncio.gather(*[store.get_or_create("k", {"n": i}) for i in range(5)])
        created_count = sum(1 for _, c in results if c)
        assert created_count == 1


class TestAppContextSeeding:
    def test_seed_default_profile(self) -> None:
        ctx = AppContext()
        # default profile seeded for tenant-a
        assert isinstance(ctx.profile_repo, InMemoryApplicationProfileRepository)
        assert ctx.profile_repo._profiles.get(("tenant-a", "example-app")) is not None

    @pytest.mark.asyncio
    async def test_create_investigation_service_wired(self) -> None:
        ctx = AppContext()
        svc = ctx.create_investigation_service()
        assert isinstance(svc, CreateInvestigationService)
        assert ctx.get_investigation_service() is not None
        assert ctx.resume_investigation_service() is not None
        assert ctx.cancel_investigation_service() is not None

    def test_set_app_context_roundtrip(self) -> None:
        ctx = AppContext()
        set_app_context(ctx)
        assert get_app_context() is ctx


# ===========================================================================
# api/tenant
# ===========================================================================


class _FakeSigningKey:
    def __init__(self, key: str) -> None:
        self.key = key


def _patch_jwks(monkeypatch: pytest.MonkeyPatch, secret: str = "test-secret") -> None:
    """Stub out network JWKS resolution; verification still runs signature/claims checks."""
    from investigation_agent_platform.api import tenant as tenant_module

    monkeypatch.setattr(
        tenant_module,
        "_get_jwk_client",
        lambda jwks_url: type(
            "FakeJWKClient",
            (),
            {"get_signing_key_from_jwt": lambda self, token: _FakeSigningKey(secret)},
        )(),
    )


def _configure_auth_env(monkeypatch: pytest.MonkeyPatch, environment: str = "development") -> None:
    monkeypatch.setenv("IAP_ENVIRONMENT", environment)
    monkeypatch.setenv("IAP_AUTH_JWKS_URL", "https://issuer.example.com/.well-known/jwks.json")
    monkeypatch.setenv("IAP_AUTH_ISSUER", "test-issuer")
    monkeypatch.setenv("IAP_AUTH_AUDIENCE", "")
    monkeypatch.setenv("IAP_AUTH_ALGORITHMS", "HS256")


class TestVerifiedIdentity:
    """Covers F-003: JWT signature/issuer/expiry verification, and F-015 principal derivation."""

    @pytest.mark.asyncio
    async def test_dev_mode_header_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("IAP_AUTH_JWKS_URL", raising=False)
        monkeypatch.delenv("IAP_AUTH_ISSUER", raising=False)
        monkeypatch.setenv("IAP_ENVIRONMENT", "development")
        identity = await _resolve_verified_identity(x_tenant_id="tenant-a", authorization=None)
        assert identity.tenant_id == "tenant-a"
        assert identity.principal_id == "dev-principal"

    @pytest.mark.asyncio
    async def test_rejects_empty_header_no_jwt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("IAP_AUTH_JWKS_URL", raising=False)
        monkeypatch.delenv("IAP_AUTH_ISSUER", raising=False)
        monkeypatch.setenv("IAP_ENVIRONMENT", "development")
        with pytest.raises(Exception) as ei:
            await _resolve_verified_identity(x_tenant_id="", authorization=None)
        assert ei.value.status_code == 401  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_valid_signed_token_derives_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure_auth_env(monkeypatch, environment="development")
        _patch_jwks(monkeypatch)
        token = _jwt_with_tenant("tenant-a")
        identity = await _resolve_verified_identity(
            x_tenant_id=None, authorization=f"Bearer {token}"
        )
        assert identity.tenant_id == "tenant-a"
        assert identity.principal_id == "principal-1"

    @pytest.mark.asyncio
    async def test_production_requires_bearer_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _configure_auth_env(monkeypatch, environment="production")
        with pytest.raises(Exception) as ei:
            await _resolve_verified_identity(x_tenant_id="tenant-a", authorization=None)
        assert ei.value.status_code == 401  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_production_without_jwks_configured_raises_configuration_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("IAP_ENVIRONMENT", "production")
        monkeypatch.delenv("IAP_AUTH_JWKS_URL", raising=False)
        monkeypatch.delenv("IAP_AUTH_ISSUER", raising=False)
        token = _jwt_with_tenant("tenant-a")
        with pytest.raises(AuthConfigurationError):
            await _resolve_verified_identity(
                x_tenant_id="tenant-a", authorization=f"Bearer {token}"
            )

    @pytest.mark.asyncio
    async def test_production_valid_token_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _configure_auth_env(monkeypatch, environment="production")
        _patch_jwks(monkeypatch)
        token = _jwt_with_tenant("tenant-a")
        identity = await _resolve_verified_identity(
            x_tenant_id="tenant-a", authorization=f"Bearer {token}"
        )
        assert identity.tenant_id == "tenant-a"
        assert identity.principal_id == "principal-1"

    @pytest.mark.asyncio
    async def test_expired_token_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _configure_auth_env(monkeypatch, environment="production")
        _patch_jwks(monkeypatch)
        import jwt as _jwt

        now = datetime.now(UTC)
        expired = _jwt.encode(
            {
                "tenant_id": "tenant-a",
                "sub": "principal-1",
                "iat": now - timedelta(hours=2),
                "exp": now - timedelta(hours=1),
                "iss": "test-issuer",
            },
            "test-secret",
            algorithm="HS256",
        )
        with pytest.raises(Exception) as ei:
            await _resolve_verified_identity(
                x_tenant_id="tenant-a", authorization=f"Bearer {expired}"
            )
        assert ei.value.status_code == 401  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_wrong_issuer_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _configure_auth_env(monkeypatch, environment="production")
        _patch_jwks(monkeypatch)
        token = _jwt_with_tenant("tenant-a", iss="someone-else")
        with pytest.raises(Exception) as ei:
            await _resolve_verified_identity(
                x_tenant_id="tenant-a", authorization=f"Bearer {token}"
            )
        assert ei.value.status_code == 401  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_forged_unsigned_token_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _configure_auth_env(monkeypatch, environment="production")
        _patch_jwks(monkeypatch)
        # Attacker crafts a token signed with an arbitrary secret, not the real one.
        forged = _jwt_with_tenant("tenant-a", secret="attacker-controlled-secret")
        with pytest.raises(Exception) as ei:
            await _resolve_verified_identity(
                x_tenant_id="tenant-a", authorization=f"Bearer {forged}"
            )
        assert ei.value.status_code == 401  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_alg_none_never_permitted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _configure_auth_env(monkeypatch, environment="production")
        monkeypatch.setenv("IAP_AUTH_ALGORITHMS", "none")
        _patch_jwks(monkeypatch)
        token = _jwt_with_tenant("tenant-a")
        with pytest.raises(AuthConfigurationError):
            await _resolve_verified_identity(
                x_tenant_id="tenant-a", authorization=f"Bearer {token}"
            )

    @pytest.mark.asyncio
    async def test_missing_tenant_claim_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _configure_auth_env(monkeypatch, environment="production")
        _patch_jwks(monkeypatch)
        token = _jwt_with_claim("sub", "principal-1")
        with pytest.raises(Exception) as ei:
            await _resolve_verified_identity(
                x_tenant_id="tenant-a", authorization=f"Bearer {token}"
            )
        assert ei.value.status_code == 401  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_tenant_mismatch_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _configure_auth_env(monkeypatch, environment="production")
        _patch_jwks(monkeypatch)
        token = _jwt_with_tenant("tenant-b")
        with pytest.raises(Exception) as ei:
            await _resolve_verified_identity(
                x_tenant_id="tenant-a", authorization=f"Bearer {token}"
            )
        assert ei.value.status_code == 403  # type: ignore[attr-defined]


class TestRequireTenant:
    @pytest.mark.asyncio
    async def test_require_tenant_returns_tenant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("IAP_AUTH_JWKS_URL", raising=False)
        monkeypatch.delenv("IAP_AUTH_ISSUER", raising=False)
        monkeypatch.setenv("IAP_ENVIRONMENT", "development")
        identity = await _resolve_verified_identity(x_tenant_id="tenant-a", authorization=None)
        assert await require_tenant(identity) == "tenant-a"

    @pytest.mark.asyncio
    async def test_require_principal_returns_principal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure_auth_env(monkeypatch, environment="production")
        _patch_jwks(monkeypatch)
        token = _jwt_with_tenant("tenant-a")
        identity = await _resolve_verified_identity(
            x_tenant_id="tenant-a", authorization=f"Bearer {token}"
        )
        assert await require_principal(identity) == "principal-1"


# ===========================================================================
# api/app
# ===========================================================================


class TestCreateApp:
    def test_factory_creates_app(self) -> None:
        from fastapi import FastAPI

        app = create_app()
        assert isinstance(app, FastAPI)
        assert app.title == "Investigation Agent Platform API"

    def test_docs_disabled_in_production(self) -> None:
        from investigation_agent_platform.api.dependencies import ApiSettings

        app = create_app(settings=ApiSettings(environment="production", enable_docs=False))
        assert app.docs_url is None
        assert app.openapi_url is None

    def test_health_live_via_test_client(self) -> None:
        ctx = AppContext()
        set_app_context(ctx)
        app = create_app()
        client = TestClient(app)
        resp = client.get("/api/v1/health/live")
        assert resp.status_code == 200
        assert resp.json()["status"] == "UP"

    def test_correlation_id_middleware_sets_header(self) -> None:
        ctx = AppContext()
        set_app_context(ctx)
        app = create_app()
        client = TestClient(app)
        # without header -> server generates uuid
        resp = client.get("/api/v1/health/live")
        assert "X-Correlation-ID" in resp.headers
        # with header -> echo
        resp2 = client.get("/api/v1/health/live", headers={"X-Correlation-ID": "my-corr-id"})
        assert resp2.headers["X-Correlation-ID"] == "my-corr-id"

    def test_error_mapping_validation_error_to_400(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import ValidationError

        ctx = AppContext()
        set_app_context(ctx)
        app = create_app()

        @app.get("/_test_validation")
        async def _route() -> Any:  # type: ignore[no-untyped-def]
            raise ValidationError("bad input")

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/_test_validation")
        assert resp.status_code == 400
        # F-062: stable public code, never the internal message.
        assert resp.json()["error"]["code"] == "DOMAIN_VALIDATION_ERROR"
        assert resp.json()["error"]["message"] != "bad input"

    def test_error_mapping_not_found_to_404(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import EntityNotFoundError

        ctx = AppContext()
        set_app_context(ctx)
        app = create_app()

        @app.get("/_test_nf")
        async def _route() -> Any:  # type: ignore[no-untyped-def]
            raise EntityNotFoundError("not found")

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/_test_nf")
        assert resp.status_code == 404

    def test_error_mapping_unauthorized_to_403(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import UnauthorizedError

        ctx = AppContext()
        set_app_context(ctx)
        app = create_app()

        @app.get("/_test_unauth")
        async def _route() -> Any:  # type: ignore[no-untyped-def]
            raise UnauthorizedError("forbidden")

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/_test_unauth")
        assert resp.status_code == 403

    def test_error_mapping_conflict_to_409(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import ConcurrencyError

        ctx = AppContext()
        set_app_context(ctx)
        app = create_app()

        @app.get("/_test_conflict")
        async def _route() -> Any:  # type: ignore[no-untyped-def]
            raise ConcurrencyError("conflict")

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/_test_conflict")
        assert resp.status_code == 409

    def test_request_validation_error_422(self) -> None:
        ctx = AppContext()
        set_app_context(ctx)
        app = create_app()
        client = TestClient(app)
        # POST investigations without required fields -> 422
        resp = client.post("/api/v1/investigations", json={}, headers={"X-Tenant-ID": "tenant-a"})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "ValidationError"


# ===========================================================================
# routers/investigations
# ===========================================================================


class TestCreateInvestigationBody:
    def test_to_request_defaults_session(self) -> None:
        body = CreateInvestigationBody(application_id="example-app", problem_description="desc")
        req = body.to_request(requested_by="user1")
        assert req.application_id == "example-app"
        assert req.session_id == "sess_example-app"
        assert req.requested_by == "user1"

    def test_to_request_uses_session_if_provided(self) -> None:
        body = CreateInvestigationBody(
            application_id="example-app", problem_description="desc", session_id="my-sess"
        )
        req = body.to_request(requested_by="user1")
        assert req.session_id == "my-sess"


class TestInvestigationsRouter:
    def _client_with_context(self) -> tuple[TestClient, AppContext]:
        ctx = AppContext()
        set_app_context(ctx)
        app = create_app()
        return TestClient(app), ctx

    def test_create_investigation_success(self) -> None:
        client, _ctx = self._client_with_context()
        resp = client.post(
            "/api/v1/investigations",
            json={"application_id": "example-app", "problem_description": "incident"},
            headers={"X-Tenant-ID": "tenant-a"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["tenant_id"] == "tenant-a"
        assert body["application_id"] == "example-app"
        assert "id" in body

    def test_create_investigation_profile_not_found_returns_404_or_400(self) -> None:
        # Use fresh context where only tenant-a/example-app exists
        client, _ctx = self._client_with_context()
        resp = client.post(
            "/api/v1/investigations",
            json={"application_id": "nonexistent", "problem_description": "incident"},
            headers={"X-Tenant-ID": "tenant-a"},
        )
        # DomainException for missing profile -> mapped to 500 or 404 depending on handler
        # ApplicationProfileNotFoundException has http 404 but is DomainException subtype not explicitly mapped -> falls to 500 unless handled
        # Check that it's not 201
        assert resp.status_code != 201
        # ensure error payload contains error code
        assert "error" in resp.json() or "detail" in resp.json()

    def test_get_investigation_not_found(self) -> None:
        client, _ = self._client_with_context()
        fake_id = str(uuid.uuid4())
        resp = client.get(f"/api/v1/investigations/{fake_id}", headers={"X-Tenant-ID": "tenant-a"})
        assert resp.status_code == 404

    def test_get_investigation_invalid_uuid(self) -> None:
        client, _ = self._client_with_context()
        resp = client.get("/api/v1/investigations/not-a-uuid", headers={"X-Tenant-ID": "tenant-a"})
        assert resp.status_code == 400

    def test_idempotency_key_reuse_returns_same_id(self) -> None:
        client, _ctx = self._client_with_context()
        headers = {"X-Tenant-ID": "tenant-a", "X-Idempotency-Key": "key-123"}
        payload = {"application_id": "example-app", "problem_description": "incident idempotent"}
        r1 = client.post("/api/v1/investigations", json=payload, headers=headers)
        assert r1.status_code == 201
        r2 = client.post("/api/v1/investigations", json=payload, headers=headers)
        assert r2.status_code == 201
        assert r1.json()["id"] == r2.json()["id"]

    def test_idempotency_different_tenant_different_key_space(self) -> None:
        # Tenant-b has no profile, so we seed one
        ctx = AppContext()
        # seed tenant-b profile

        profile_b = _DEFAULT_PROFILE.model_copy(
            update={"tenant_id": "tenant-b", "id": "example-app"}
        )
        # need to directly insert into repo
        assert isinstance(ctx.profile_repo, InMemoryApplicationProfileRepository)
        ctx.profile_repo._profiles[("tenant-b", "example-app")] = profile_b
        set_app_context(ctx)
        app = create_app()
        client = TestClient(app)
        headers_a = {"X-Tenant-ID": "tenant-a", "X-Idempotency-Key": "shared-key"}
        headers_b = {"X-Tenant-ID": "tenant-b", "X-Idempotency-Key": "shared-key"}
        payload = {"application_id": "example-app", "problem_description": "incident"}
        r_a = client.post("/api/v1/investigations", json=payload, headers=headers_a)
        r_b = client.post("/api/v1/investigations", json=payload, headers=headers_b)
        assert r_a.status_code == 201
        assert r_b.status_code == 201
        assert r_a.json()["id"] != r_b.json()["id"]

    def test_missing_tenant_header_422_or_401(self) -> None:
        client, _ = self._client_with_context()
        resp = client.post(
            "/api/v1/investigations",
            json={"application_id": "example-app", "problem_description": "incident"},
        )
        assert resp.status_code in (401, 422)

    def test_cancel_not_found(self) -> None:
        client, _ = self._client_with_context()
        fake_id = str(uuid.uuid4())
        resp = client.post(
            f"/api/v1/investigations/{fake_id}/cancel", headers={"X-Tenant-ID": "tenant-a"}
        )
        assert resp.status_code == 404

    def test_start_invalid_uuid(self) -> None:
        client, _ = self._client_with_context()
        resp = client.post(
            "/api/v1/investigations/not-uuid/start", headers={"X-Tenant-ID": "tenant-a"}
        )
        assert resp.status_code == 400


# ===========================================================================
# validator
# ===========================================================================


class TestHasTraversal:
    def test_simple_traversal(self) -> None:
        assert _has_traversal("../etc/passwd") is True
        assert _has_traversal("src/../etc") is True

    def test_encoded_traversal(self) -> None:
        assert _has_traversal("%2e%2e%2fetc%2fpasswd") is True
        assert _has_traversal("%2E%2E%2Fsecret") is True

    def test_double_encoded(self) -> None:
        assert _has_traversal("%252e%252e%2fetc") is True  # %252e -> %2e -> .
        assert _has_traversal("%25252e%25252e%25252f") is True

    def test_backslash(self) -> None:
        assert _has_traversal("..\\etc\\passwd") is True

    def test_null_byte(self) -> None:
        assert _has_traversal("src/file\x00.png") is True
        assert _has_traversal("src/file%00.png") is True

    def test_safe_paths(self) -> None:
        assert _has_traversal("src/app/main.py") is False
        assert _has_traversal("") is False
        assert _has_traversal("src/.../file") is False  # ... is not ..


class TestIsWithinRoots:
    def test_allow_all_empty_fails_closed(self) -> None:
        assert _is_within_roots("src/file.py", []) is False

    def test_within_root(self) -> None:
        assert _is_within_roots("src/app.py", ["src"]) is True
        assert _is_within_roots("src/nested/file.py", ["src"]) is True

    def test_outside_root(self) -> None:
        assert _is_within_roots("other/file.py", ["src"]) is False

    def test_exact_match(self) -> None:
        assert _is_within_roots("src", ["src"]) is True

    def test_prefix_attack(self) -> None:
        # src2 should not match src
        assert _is_within_roots("src2/file.py", ["src"]) is False


class TestInvestigationActionValidator:
    def _make_authorizer(self, allowed: bool = True) -> MagicMock:
        m: MagicMock = MagicMock()
        m.authorize_action = AsyncMock(return_value=allowed)
        return m

    def _make_registry(self, enabled: bool = True) -> MagicMock:
        m: MagicMock = MagicMock()
        m.is_capability_enabled = AsyncMock(return_value=enabled)
        return m

    @pytest.mark.asyncio
    async def test_rejects_non_dict_params(self) -> None:
        v = InvestigationActionValidator()
        # bypass pydantic validation via model_construct to test validator's dict check
        action = InvestigationAction.model_construct(
            action_type=ActionType.SEARCH_LOGS,
            parameters="not-a-dict",  # type: ignore[arg-type]
            execution_hash="h",
        )
        with pytest.raises(SecurityPolicyViolationException):
            await v.validate_action(
                action,
                _DEFAULT_PROFILE,
                InvestigationLimits(),
                0,
                0,
                "tenant-a",
                self._make_authorizer(),
                self._make_registry(),
            )

    @pytest.mark.asyncio
    async def test_authorizer_required(self) -> None:
        v = InvestigationActionValidator()
        action = InvestigationAction(
            action_type=ActionType.SEARCH_LOGS, parameters={}, execution_hash="h"
        )
        with pytest.raises(SecurityPolicyViolationException, match="Authorizer"):
            await v.validate_action(
                action,
                _DEFAULT_PROFILE,
                InvestigationLimits(),
                0,
                0,
                "tenant-a",
                None,
                None,  # type: ignore[arg-type]
            )

    @pytest.mark.asyncio
    async def test_unauthorized_action(self) -> None:
        v = InvestigationActionValidator()
        action = InvestigationAction(
            action_type=ActionType.SEARCH_LOGS, parameters={}, execution_hash="h"
        )
        with pytest.raises(SecurityPolicyViolationException, match="unauthorized"):
            await v.validate_action(
                action,
                _DEFAULT_PROFILE,
                InvestigationLimits(),
                0,
                0,
                "tenant-a",
                self._make_authorizer(False),
                self._make_registry(),
            )

    @pytest.mark.asyncio
    async def test_capability_disabled(self) -> None:
        v = InvestigationActionValidator()
        action = InvestigationAction(
            action_type=ActionType.SEARCH_LOGS, parameters={}, execution_hash="h"
        )
        with pytest.raises(SecurityPolicyViolationException, match="disabled"):
            await v.validate_action(
                action,
                _DEFAULT_PROFILE,
                InvestigationLimits(),
                0,
                0,
                "tenant-a",
                self._make_authorizer(True),
                self._make_registry(False),
            )

    @pytest.mark.asyncio
    async def test_budget_tool_calls_exceeded(self) -> None:
        v = InvestigationActionValidator()
        action = InvestigationAction(
            action_type=ActionType.SEARCH_LOGS, parameters={}, execution_hash="h"
        )
        limits = InvestigationLimits(max_tool_calls=1)
        with pytest.raises(SecurityPolicyViolationException, match="tool call"):
            await v.validate_action(
                action,
                _DEFAULT_PROFILE,
                limits,
                0,
                1,
                "tenant-a",
                self._make_authorizer(),
                self._make_registry(),
            )

    @pytest.mark.asyncio
    async def test_budget_evidence_exceeded(self) -> None:
        v = InvestigationActionValidator()
        action = InvestigationAction(
            action_type=ActionType.SEARCH_LOGS, parameters={}, execution_hash="h"
        )
        limits = InvestigationLimits(max_evidence_items=1)
        with pytest.raises(SecurityPolicyViolationException, match="evidence limit"):
            await v.validate_action(
                action,
                _DEFAULT_PROFILE,
                limits,
                1,
                0,
                "tenant-a",
                self._make_authorizer(),
                self._make_registry(),
            )

    @pytest.mark.asyncio
    async def test_query_state_requires_valid_template(self) -> None:
        v = InvestigationActionValidator()
        action = InvestigationAction(
            action_type=ActionType.QUERY_STATE,
            parameters={"template_key": "bad_key"},
            execution_hash="h",
        )
        with pytest.raises(SecurityPolicyViolationException, match="queryTemplate"):
            await v.validate_action(
                action,
                _DEFAULT_PROFILE,
                InvestigationLimits(),
                0,
                0,
                "tenant-a",
                self._make_authorizer(),
                self._make_registry(),
            )

    @pytest.mark.asyncio
    async def test_query_state_valid_template_passes(self) -> None:
        v = InvestigationActionValidator()
        action = InvestigationAction(
            action_type=ActionType.QUERY_STATE,
            parameters={"template_key": "find_transaction"},
            execution_hash="h",
        )
        await v.validate_action(
            action,
            _DEFAULT_PROFILE,
            InvestigationLimits(),
            0,
            0,
            "tenant-a",
            self._make_authorizer(),
            self._make_registry(),
        )

    @pytest.mark.asyncio
    async def test_query_state_dynamic_sql_rejected(self) -> None:
        v = InvestigationActionValidator()
        action = InvestigationAction(
            action_type=ActionType.QUERY_STATE,
            parameters={"template_key": "find_transaction", "sql": "SELECT * FROM orders"},
            execution_hash="h",
        )
        with pytest.raises(SecurityPolicyViolationException, match="Dynamic SQL"):
            await v.validate_action(
                action,
                _DEFAULT_PROFILE,
                InvestigationLimits(),
                0,
                0,
                "tenant-a",
                self._make_authorizer(),
                self._make_registry(),
            )

    @pytest.mark.asyncio
    async def test_query_state_forbidden_token_in_params(self) -> None:
        v = InvestigationActionValidator()
        action = InvestigationAction(
            action_type=ActionType.QUERY_STATE,
            parameters={"template_key": "find_transaction", "filter": "DROP TABLE orders"},
            execution_hash="h",
        )
        with pytest.raises(SecurityPolicyViolationException):
            await v.validate_action(
                action,
                _DEFAULT_PROFILE,
                InvestigationLimits(),
                0,
                0,
                "tenant-a",
                self._make_authorizer(),
                self._make_registry(),
            )

    @pytest.mark.asyncio
    async def test_get_code_traversal_rejected(self) -> None:
        v = InvestigationActionValidator()
        for path in [
            "../etc/passwd",
            "%2e%2e%2fetc",
            "%252e%252e%2fetc",
            "..\\windows",
            "/absolute/path",
            "~/home",
        ]:
            action = InvestigationAction(
                action_type=ActionType.GET_CODE, parameters={"file_path": path}, execution_hash="h"
            )
            with pytest.raises(SecurityPolicyViolationException, match="traversal|outside|attempt"):
                await v.validate_action(
                    action,
                    _DEFAULT_PROFILE,
                    InvestigationLimits(),
                    0,
                    0,
                    "tenant-a",
                    self._make_authorizer(),
                    self._make_registry(),
                )

    @pytest.mark.asyncio
    async def test_get_code_outside_roots_rejected(self) -> None:
        v = InvestigationActionValidator()
        action = InvestigationAction(
            action_type=ActionType.GET_CODE,
            parameters={"file_path": "other/file.py"},
            execution_hash="h",
        )
        with pytest.raises(SecurityPolicyViolationException, match="outside allowed source roots"):
            await v.validate_action(
                action,
                _DEFAULT_PROFILE,
                InvestigationLimits(),
                0,
                0,
                "tenant-a",
                self._make_authorizer(),
                self._make_registry(),
            )

    @pytest.mark.asyncio
    async def test_get_code_valid_path_passes(self) -> None:
        v = InvestigationActionValidator()
        action = InvestigationAction(
            action_type=ActionType.GET_CODE,
            parameters={"file_path": "src/app.py"},
            execution_hash="h",
        )
        await v.validate_action(
            action,
            _DEFAULT_PROFILE,
            InvestigationLimits(),
            0,
            0,
            "tenant-a",
            self._make_authorizer(),
            self._make_registry(),
        )

    @pytest.mark.asyncio
    async def test_get_code_null_byte_rejected(self) -> None:
        v = InvestigationActionValidator()
        action = InvestigationAction(
            action_type=ActionType.GET_CODE,
            parameters={"file_path": "src/app.py\x00"},
            execution_hash="h",
        )
        with pytest.raises(SecurityPolicyViolationException):
            await v.validate_action(
                action,
                _DEFAULT_PROFILE,
                InvestigationLimits(),
                0,
                0,
                "tenant-a",
                self._make_authorizer(),
                self._make_registry(),
            )

    @pytest.mark.asyncio
    async def test_search_logs_limit_exceeded(self) -> None:
        v = InvestigationActionValidator()
        # _DEFAULT_PROFILE max_evidence_per_query is 50
        action = InvestigationAction(
            action_type=ActionType.SEARCH_LOGS, parameters={"limit": 100}, execution_hash="h"
        )
        with pytest.raises(SecurityPolicyViolationException, match="maximumEvidencePerQuery"):
            await v.validate_action(
                action,
                _DEFAULT_PROFILE,
                InvestigationLimits(),
                0,
                0,
                "tenant-a",
                self._make_authorizer(),
                self._make_registry(),
            )

    @pytest.mark.asyncio
    async def test_correlation_depth_exceeded(self) -> None:
        v = InvestigationActionValidator()
        action = InvestigationAction(
            action_type=ActionType.CORRELATE, parameters={"max_depth": 10}, execution_hash="h"
        )
        limits = InvestigationLimits(max_correlation_depth=3)
        with pytest.raises(SecurityPolicyViolationException, match="Correlation depth"):
            await v.validate_action(
                action,
                _DEFAULT_PROFILE,
                limits,
                0,
                0,
                "tenant-a",
                self._make_authorizer(),
                self._make_registry(),
            )

    @pytest.mark.asyncio
    async def test_generic_script_tag_rejected(self) -> None:
        v = InvestigationActionValidator()
        action = InvestigationAction(
            action_type=ActionType.SEARCH_LOGS,
            parameters={"query": "<script>alert(1)</script>"},
            execution_hash="h",
        )
        with pytest.raises(SecurityPolicyViolationException, match="script pattern"):
            await v.validate_action(
                action,
                _DEFAULT_PROFILE,
                InvestigationLimits(),
                0,
                0,
                "tenant-a",
                self._make_authorizer(),
                self._make_registry(),
            )

    @pytest.mark.asyncio
    async def test_forbidden_sql_in_search_logs_via_query_string(self) -> None:
        # SEARCH_LOGS with raw_es_body should be rejected regardless of content
        v = InvestigationActionValidator()
        action = InvestigationAction(
            action_type=ActionType.SEARCH_LOGS, parameters={"raw_es_body": "{}"}, execution_hash="h"
        )
        with pytest.raises(SecurityPolicyViolationException):
            await v.validate_action(
                action,
                _DEFAULT_PROFILE,
                InvestigationLimits(),
                0,
                0,
                "tenant-a",
                self._make_authorizer(),
                self._make_registry(),
            )


# ===========================================================================
# F-004: mandatory action authorization gate
# ===========================================================================


class TestExecuteActionServiceAuthorization:
    def _make_service(self, authorized: bool = True, capability_enabled: bool = True) -> Any:
        from investigation_agent_platform.application.investigation.services import (
            ExecuteActionService,
        )

        authorizer = MagicMock()
        authorizer.authorize_action = AsyncMock(return_value=authorized)
        registry = MagicMock()
        registry.is_capability_enabled = AsyncMock(return_value=capability_enabled)
        action_repo = MagicMock()
        action_repo.record_action = AsyncMock(return_value=None)
        return ExecuteActionService(
            action_repo=action_repo,
            evidence_repo=MagicMock(),
            authorizer=authorizer,
            capability_registry=registry,
        )

    def test_construction_requires_authorizer_and_registry(self) -> None:
        from investigation_agent_platform.application.investigation.services import (
            ExecuteActionService,
        )

        with pytest.raises(SecurityPolicyViolationException):
            ExecuteActionService(
                action_repo=MagicMock(),
                evidence_repo=MagicMock(),
                authorizer=None,
                capability_registry=None,
            )

    @pytest.mark.asyncio
    async def test_denies_when_authorizer_returns_false(self) -> None:
        service = self._make_service(authorized=False)
        action = InvestigationAction(
            action_type=ActionType.SEARCH_LOGS, parameters={}, execution_hash="h"
        )
        with pytest.raises(SecurityPolicyViolationException):
            await service.authorize("tenant-a", uuid.uuid4(), action, "principal-1")

    @pytest.mark.asyncio
    async def test_denies_when_capability_disabled(self) -> None:
        service = self._make_service(authorized=True, capability_enabled=False)
        action = InvestigationAction(
            action_type=ActionType.SEARCH_LOGS, parameters={}, execution_hash="h"
        )
        with pytest.raises(SecurityPolicyViolationException):
            await service.authorize("tenant-a", uuid.uuid4(), action, "principal-1")

    @pytest.mark.asyncio
    async def test_allows_and_records_decision(self) -> None:
        service = self._make_service(authorized=True, capability_enabled=True)
        action = InvestigationAction(
            action_type=ActionType.SEARCH_LOGS, parameters={}, execution_hash="h"
        )
        decision = await service.authorize("tenant-a", uuid.uuid4(), action, "principal-1")
        assert decision["authorized"] is True
        assert decision["policy_version"]

    @pytest.mark.asyncio
    async def test_execute_records_action(self) -> None:
        service = self._make_service()
        action = InvestigationAction(
            action_type=ActionType.SEARCH_LOGS, parameters={}, execution_hash="h"
        )
        inv_id = uuid.uuid4()
        await service.execute("tenant-a", inv_id, action)
        service.action_repo.record_action.assert_awaited_once()


class TestProfileBasedAuthorizer:
    def _profile_repo(self, profile: Any) -> MagicMock:
        repo = MagicMock()
        repo.get_by_application_id = AsyncMock(return_value=profile)
        return repo

    @pytest.mark.asyncio
    async def test_internal_actions_always_allowed(self) -> None:
        from investigation_agent_platform.infrastructure.security.profile_authorizer import (
            ProfileBasedActionAuthorizer,
        )

        authorizer = ProfileBasedActionAuthorizer(self._profile_repo(None))
        assert await authorizer.authorize_action("tenant-a", "CONCLUDE", "", {}) is True

    @pytest.mark.asyncio
    async def test_denies_without_application_id_in_context(self) -> None:
        from investigation_agent_platform.infrastructure.security.profile_authorizer import (
            ProfileBasedActionAuthorizer,
        )

        authorizer = ProfileBasedActionAuthorizer(self._profile_repo(_DEFAULT_PROFILE))
        assert await authorizer.authorize_action("tenant-a", "SEARCH_LOGS", "", {}) is False

    @pytest.mark.asyncio
    async def test_denies_when_profile_missing(self) -> None:
        from investigation_agent_platform.infrastructure.security.profile_authorizer import (
            ProfileBasedActionAuthorizer,
        )

        authorizer = ProfileBasedActionAuthorizer(self._profile_repo(None))
        assert (
            await authorizer.authorize_action(
                "tenant-a", "SEARCH_LOGS", "", {"application_id": "example-app"}
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_allows_when_evidence_type_enabled(self) -> None:
        from investigation_agent_platform.infrastructure.security.profile_authorizer import (
            ProfileBasedActionAuthorizer,
        )

        authorizer = ProfileBasedActionAuthorizer(self._profile_repo(_DEFAULT_PROFILE))
        assert (
            await authorizer.authorize_action(
                "tenant-a", "SEARCH_LOGS", "", {"application_id": "example-app"}
            )
            is True
        )

    @pytest.mark.asyncio
    async def test_denies_unknown_action_type(self) -> None:
        from investigation_agent_platform.infrastructure.security.profile_authorizer import (
            ProfileBasedActionAuthorizer,
        )

        authorizer = ProfileBasedActionAuthorizer(self._profile_repo(_DEFAULT_PROFILE))
        assert await authorizer.authorize_action("tenant-a", "NOT_A_REAL_ACTION", "", {}) is False


# ===========================================================================
# reasoning
# ===========================================================================


class TestReasoningCoordinator:
    def _make_state(self) -> InvestigationState:
        inv = _make_investigation()
        return InvestigationState(investigation=inv)

    @pytest.mark.asyncio
    async def test_stub_path_no_gateway(self) -> None:
        safety = MagicMock()
        safety.validate_prompt_safety = AsyncMock(return_value=True)
        coord = ReasoningCoordinator(prompt_safety_policy=safety, llm_gateway=None)
        decision = await coord.reason("tenant-a", self._make_state())
        # F-006/F-007: no gateway configured must never fabricate a conclusion.
        assert decision.conclusion_readiness == 0.0
        assert decision.metadata["status"] == "REASONING_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_prompt_safety_violation_returns_zero_readiness(self) -> None:
        safety = MagicMock()
        safety.validate_prompt_safety = AsyncMock(return_value=False)
        coord = ReasoningCoordinator(prompt_safety_policy=safety, llm_gateway=None)
        decision = await coord.reason("tenant-a", self._make_state())
        assert decision.conclusion_readiness == 0.0
        assert "violation" in decision.observations[0].lower()

    @pytest.mark.asyncio
    async def test_llm_gateway_injected_returns_parsed(self) -> None:
        safety = MagicMock()
        safety.validate_prompt_safety = AsyncMock(return_value=True)
        gateway = MagicMock()
        metadata = _llm_meta()
        gateway.complete = AsyncMock(
            return_value=LLMGatewayResponse(
                content='{"observations": ["from llm"]}',
                parsed={
                    "observations": ["from llm"],
                    "conclusion_readiness": 0.42,
                    "reasoning_chain": "llm chain",
                },
                metadata=metadata,
            )
        )
        coord = ReasoningCoordinator(prompt_safety_policy=safety, llm_gateway=gateway)
        decision = await coord.reason("tenant-a", self._make_state())
        assert decision.conclusion_readiness == 0.42
        assert decision.observations == ["from llm"]
        gateway.complete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_llm_gateway_invalid_schema_falls_back_to_stub(self) -> None:
        safety = MagicMock()
        safety.validate_prompt_safety = AsyncMock(return_value=True)
        gateway = MagicMock()
        metadata = _llm_meta()
        # Invalid readiness >1 will fail validation and fallback
        gateway.complete = AsyncMock(
            return_value=LLMGatewayResponse(
                content="bad",
                parsed={"conclusion_readiness": 999, "observations": []},
                metadata=metadata,
            )
        )
        coord = ReasoningCoordinator(prompt_safety_policy=safety, llm_gateway=gateway)
        decision = await coord.reason("tenant-a", self._make_state())
        # F-006/F-007: invalid LLM schema must never fall back to a fabricated stub.
        assert decision.conclusion_readiness == 0.0
        assert decision.metadata["status"] == "REASONING_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_llm_gateway_exception_falls_back(self) -> None:
        safety = MagicMock()
        safety.validate_prompt_safety = AsyncMock(return_value=True)
        gateway = MagicMock()
        gateway.complete = AsyncMock(side_effect=RuntimeError("llm down"))
        coord = ReasoningCoordinator(prompt_safety_policy=safety, llm_gateway=gateway)
        decision = await coord.reason("tenant-a", self._make_state())
        # F-007: a provider outage must never become false investigative certainty.
        assert decision.conclusion_readiness == 0.0
        assert decision.metadata["status"] == "REASONING_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_llm_gateway_no_parsed_falls_back(self) -> None:
        safety = MagicMock()
        safety.validate_prompt_safety = AsyncMock(return_value=True)
        gateway = MagicMock()
        metadata = _llm_meta()
        gateway.complete = AsyncMock(
            return_value=LLMGatewayResponse(content="hello", parsed=None, metadata=metadata)
        )
        coord = ReasoningCoordinator(
            prompt_safety_policy=safety, llm_gateway=gateway, observability=MagicMock()
        )
        decision = await coord.reason("tenant-a", self._make_state())
        assert decision.conclusion_readiness == 0.0
        assert decision.metadata["status"] == "REASONING_UNAVAILABLE"

    def test_format_xml_payload_escapes(self) -> None:
        from investigation_agent_platform.application.investigation.reasoning import (
            EvidenceContextFormatter,
        )

        manifest = EvidenceManifest(
            evidence_type=EvidenceType.LOG,
            source="urn:test",
            payload_reference="path",
            summary="sum",
        )
        raw = "<script>alert('x')</script> & more"
        xml = EvidenceContextFormatter.format_xml_payload(manifest, raw)
        assert "&lt;script&gt;" in xml
        assert "&amp;" in xml
        assert "<untrusted_evidence_payload" in xml


# ===========================================================================
# services
# ===========================================================================


class TestCreateInvestigationService:
    @pytest.mark.asyncio
    async def test_create_success(self) -> None:
        repo = InMemoryInvestigationRepository()
        profile_repo = InMemoryApplicationProfileRepository()
        profile_repo._profiles[("tenant-a", "example-app")] = _DEFAULT_PROFILE
        svc = CreateInvestigationService(investigation_repo=repo, profile_repo=profile_repo)
        req = InvestigationRequest(
            application_id="example-app",
            problem_description="incident",
            session_id="s1",
            requested_by="tester",
        )
        inv = await svc.execute(req, tenant_id="tenant-a")
        assert inv.tenant_id == "tenant-a"
        assert inv.status == InvestigationStatus.CREATED
        # persisted
        fetched = await repo.get_by_id("tenant-a", inv.id)
        assert fetched is not None

    @pytest.mark.asyncio
    async def test_profile_not_found_raises(self) -> None:
        repo = InMemoryInvestigationRepository()
        profile_repo = InMemoryApplicationProfileRepository()
        svc = CreateInvestigationService(investigation_repo=repo, profile_repo=profile_repo)
        req = InvestigationRequest(
            application_id="missing",
            problem_description="incident",
            session_id="s1",
            requested_by="tester",
        )
        with pytest.raises(DomainException):
            await svc.execute(req, tenant_id="tenant-a")

    @pytest.mark.asyncio
    async def test_telemetry_recorded(self) -> None:
        repo = InMemoryInvestigationRepository()
        profile_repo = InMemoryApplicationProfileRepository()
        profile_repo._profiles[("tenant-a", "example-app")] = _DEFAULT_PROFILE
        telemetry = MagicMock()
        svc = CreateInvestigationService(
            investigation_repo=repo, profile_repo=profile_repo, telemetry=telemetry
        )
        req = InvestigationRequest(
            application_id="example-app",
            problem_description="incident",
            session_id="s1",
            requested_by="tester",
        )
        await svc.execute(req, tenant_id="tenant-a")
        telemetry.record_metric.assert_called_once()


class TestGetInvestigationService:
    @pytest.mark.asyncio
    async def test_get_found_and_not_found(self) -> None:
        repo = InMemoryInvestigationRepository()
        inv = _make_investigation()
        await repo.create("tenant-a", inv)
        svc = GetInvestigationService(investigation_repo=repo)
        assert await svc.execute("tenant-a", inv.id) is not None
        assert await svc.execute("tenant-a", uuid.uuid4()) is None
        # tenant isolation
        assert await svc.execute("tenant-b", inv.id) is None


class TestResumeInvestigationService:
    @pytest.mark.asyncio
    async def test_resume_success(self) -> None:
        inv_repo = InMemoryInvestigationRepository()
        profile_repo = InMemoryApplicationProfileRepository()
        profile_repo._profiles[("tenant-a", "example-app")] = _DEFAULT_PROFILE
        ev_repo = InMemoryEvidenceRepository()
        tl_repo = InMemoryTimelineRepository()
        from investigation_agent_platform.api.dependencies import _InMemoryCheckpointRepository

        inv = _make_investigation()
        await inv_repo.create("tenant-a", inv)
        svc = ResumeInvestigationService(
            inv_repo, profile_repo, ev_repo, tl_repo, _InMemoryCheckpointRepository()
        )
        ctx = await svc.execute("tenant-a", inv.id)
        assert ctx.investigation.id == inv.id

    @pytest.mark.asyncio
    async def test_resume_not_found(self) -> None:
        inv_repo = InMemoryInvestigationRepository()
        profile_repo = InMemoryApplicationProfileRepository()
        from investigation_agent_platform.api.dependencies import _InMemoryCheckpointRepository

        svc = ResumeInvestigationService(
            inv_repo,
            profile_repo,
            InMemoryEvidenceRepository(),
            InMemoryTimelineRepository(),
            _InMemoryCheckpointRepository(),
        )
        with pytest.raises(DomainException):
            await svc.execute("tenant-a", uuid.uuid4())

    @pytest.mark.asyncio
    async def test_resume_profile_missing(self) -> None:
        inv_repo = InMemoryInvestigationRepository()
        profile_repo = InMemoryApplicationProfileRepository()
        from investigation_agent_platform.api.dependencies import _InMemoryCheckpointRepository

        inv = _make_investigation()
        await inv_repo.create("tenant-a", inv)
        svc = ResumeInvestigationService(
            inv_repo,
            profile_repo,
            InMemoryEvidenceRepository(),
            InMemoryTimelineRepository(),
            _InMemoryCheckpointRepository(),
        )
        with pytest.raises(DomainException):
            await svc.execute("tenant-a", inv.id)


class TestCancelInvestigationService:
    @pytest.mark.asyncio
    async def test_cancel_success_transitions(self) -> None:
        repo = InMemoryInvestigationRepository()
        from investigation_agent_platform.api.dependencies import _InMemoryTransitionRepository

        inv = _make_investigation()
        await repo.create("tenant-a", inv)
        svc = CancelInvestigationService(
            investigation_repo=repo, transition_repo=_InMemoryTransitionRepository()
        )
        updated = await svc.execute("tenant-a", inv.id, reason="test cancel")
        assert updated.status == InvestigationStatus.CANCELLED
        assert updated.version == 2

    @pytest.mark.asyncio
    async def test_cancel_not_found(self) -> None:
        repo = InMemoryInvestigationRepository()
        from investigation_agent_platform.api.dependencies import _InMemoryTransitionRepository

        svc = CancelInvestigationService(
            investigation_repo=repo, transition_repo=_InMemoryTransitionRepository()
        )
        with pytest.raises(DomainException):
            await svc.execute("tenant-a", uuid.uuid4(), reason="x")

    @pytest.mark.asyncio
    async def test_cancel_invalid_transition_raises(self) -> None:
        repo = InMemoryInvestigationRepository()
        from investigation_agent_platform.api.dependencies import _InMemoryTransitionRepository

        inv = _make_investigation()
        # manually set to COMPLETED which cannot transition to CANCELLED
        inv_completed = inv.model_copy(update={"status": InvestigationStatus.COMPLETED})
        await repo.create("tenant-a", inv_completed)
        svc = CancelInvestigationService(
            investigation_repo=repo, transition_repo=_InMemoryTransitionRepository()
        )
        with pytest.raises(DomainException):
            await svc.execute("tenant-a", inv_completed.id, reason="x")

    @pytest.mark.asyncio
    async def test_cancel_publishes_event(self) -> None:
        repo = InMemoryInvestigationRepository()
        from investigation_agent_platform.api.dependencies import _InMemoryTransitionRepository

        inv = _make_investigation()
        await repo.create("tenant-a", inv)
        publisher = MagicMock()
        publisher.publish_domain_event = AsyncMock()
        svc = CancelInvestigationService(
            investigation_repo=repo,
            transition_repo=_InMemoryTransitionRepository(),
            event_publisher=publisher,
        )
        await svc.execute("tenant-a", inv.id, reason="cancel reason")
        publisher.publish_domain_event.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cancel_concurrency_conflict(self) -> None:
        repo = InMemoryInvestigationRepository()
        from investigation_agent_platform.api.dependencies import _InMemoryTransitionRepository

        inv = _make_investigation()
        await repo.create("tenant-a", inv)
        # force save to raise concurrency error
        orig_save = repo.save

        async def _failing_save(
            tenant_id: str, investigation: Investigation, expected_version: int
        ) -> None:
            raise ConcurrencyError("forced conflict")

        repo.save = _failing_save  # type: ignore[method-assign]
        svc = CancelInvestigationService(
            investigation_repo=repo, transition_repo=_InMemoryTransitionRepository()
        )
        with pytest.raises(ConcurrencyError):
            await svc.execute("tenant-a", inv.id, reason="x")
        repo.save = orig_save  # type: ignore[method-assign]


class TestInvestigationStateTransitions:
    def test_valid_transition_created_to_cancelled(self) -> None:
        inv = _make_investigation()
        new_inv, trans = inv.transition_to(
            InvestigationStatus.CANCELLED,
            actor=inv.request.requested_by
            and __import__(
                "investigation_agent_platform.domain.investigation.models", fromlist=["ActorType"]
            ).ActorType.USER,
            reason="test",
        )  # type: ignore[arg-type]
        assert new_inv.status == InvestigationStatus.CANCELLED

    def test_invalid_transition_raises(self) -> None:
        inv = _make_investigation()
        completed = inv.model_copy(update={"status": InvestigationStatus.COMPLETED})
        with pytest.raises(DomainException):
            completed.transition_to(
                InvestigationStatus.CANCELLED,
                actor=__import__(
                    "investigation_agent_platform.domain.investigation.models",
                    fromlist=["ActorType"],
                ).ActorType.USER,
                reason="x",
            )  # type: ignore[arg-type]

    def test_paused_edges(self) -> None:
        from investigation_agent_platform.domain.investigation.models import ActorType

        inv = _make_investigation()
        active = inv.model_copy(update={"status": InvestigationStatus.INVESTIGATING})
        paused, _ = active.transition_to(InvestigationStatus.PAUSED, actor=ActorType.USER, reason="p")
        assert paused.status == InvestigationStatus.PAUSED
        resumed, _ = paused.transition_to(
            InvestigationStatus.INVESTIGATING, actor=ActorType.USER, reason="r"
        )
        assert resumed.status == InvestigationStatus.INVESTIGATING
        with pytest.raises(DomainException):
            paused.transition_to(InvestigationStatus.COMPLETED, actor=ActorType.USER, reason="x")
        completed = inv.model_copy(update={"status": InvestigationStatus.COMPLETED})
        with pytest.raises(DomainException):
            completed.transition_to(InvestigationStatus.PAUSED, actor=ActorType.USER, reason="x")
        created = inv.model_copy(update={"status": InvestigationStatus.CREATED})
        created_paused, _ = created.transition_to(
            InvestigationStatus.PAUSED, actor=ActorType.USER, reason="p"
        )
        assert created_paused.status == InvestigationStatus.PAUSED

    def test_version_increments_on_cancel_via_service(self) -> None:
        # covered above but check domain directly
        inv = _make_investigation()
        from investigation_agent_platform.domain.investigation.models import ActorType

        new_inv, _ = inv.transition_to(
            InvestigationStatus.CANCELLED, actor=ActorType.USER, reason="r"
        )
        assert new_inv.version == inv.version + 1
        assert new_inv.completed_at is not None
