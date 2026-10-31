"""Unit tests for the onboarding application service (fakes, no database)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from investigation_agent_platform.application.onboarding.service import (
    OnboardingService,
    ServiceError,
)
from investigation_agent_platform.infrastructure.persistence.onboarding_acceptance_repository import (
    AcceptanceStaleError,
)
from investigation_agent_platform.infrastructure.persistence.onboarding_registry_repository import (
    RegistryTransitionError,
)
from investigation_agent_platform.infrastructure.persistence.onboarding_request_repository import (
    DraftIdempotencyConflictError,
)


def _draft_row(request_id: UUID, revision: int = 1) -> SimpleNamespace:
    return SimpleNamespace(request_id=request_id, revision=revision)


def _accepted(request_id: UUID, revision: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        request_id=request_id,
        revision=revision,
        job_id=uuid4(),
        id=uuid4(),
        workflow_id="wf-1",
        state="QUEUED",
    )


def _request_row(request_id: UUID, revision: int = 1, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "tenant_id": "tenant-a",
        "request_id": request_id,
        "revision": revision,
        "application_id": "app-1",
        "environment": "prod",
        "policy_version": "p1",
        "catalog_version": "v0.1",
        "content_hash": "c" * 64,
        "state": "QUEUED",
        "state_version": 2,
        "job_id": uuid4(),
    }
    row.update(overrides)
    return row


class _Drafts:
    def __init__(self, behavior: str = "ok", request_id: UUID | None = None) -> None:
        self.behavior = behavior
        self.request_id = request_id or uuid4()

    async def create_draft(self, **kwargs: Any) -> SimpleNamespace:
        if self.behavior == "conflict":
            raise DraftIdempotencyConflictError("bound to different content")
        revision = kwargs.get("expected_revision")
        return _draft_row(self.request_id, revision + 1 if revision else 1)


class _Acceptance:
    def __init__(self, behavior: str = "ok") -> None:
        self.behavior = behavior
        self.activation_calls = 0

    async def accept_draft(self, **kwargs: Any) -> SimpleNamespace:
        if self.behavior == "stale":
            raise AcceptanceStaleError("not DRAFT")
        return _accepted(kwargs["request_id"], kwargs["expected_revision"])

    async def record_activation_audit(self, **kwargs: Any) -> None:
        self.activation_calls += 1


class _Dispatches:
    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = row
        self.cancel_moved = True
        self.retry_moved = True

    async def read_latest_request_row(self, tenant_id: str, request_id: UUID) -> Any:
        return self.row

    async def request_cancellation(self, tenant_id: str, request_id: UUID, revision: int) -> bool:
        return self.cancel_moved

    async def retry_now(self, tenant_id: str, dispatch_id: UUID) -> bool:
        return self.retry_moved


class _Registry:
    def __init__(
        self,
        status: str | None = "RESERVED",
        fail: bool = False,
        registered_ids: list[str] | None = None,
    ) -> None:
        self.status = status
        self.fail = fail
        self.registered_ids = registered_ids or []

    async def list_known_tenant_ids(self) -> list[str]:
        return list(self.registered_ids)

    async def read_entry(self, tenant_id: str) -> Any:
        if self.status is None:
            return None
        return SimpleNamespace(status=self.status)

    async def _guard(self, want: str) -> SimpleNamespace:
        if self.fail or self.status != want:
            raise RegistryTransitionError(f"tenant is {self.status}, not {want}")
        self.status = {"RESERVED": "ACTIVE", "ACTIVE": "SUSPENDED", "SUSPENDED": "ACTIVE"}[want]
        return SimpleNamespace(status=self.status, lifecycle_epoch=2)

    async def activate(self, tenant_id: str) -> Any:
        return await self._guard("RESERVED")

    async def suspend(self, tenant_id: str) -> Any:
        return await self._guard("ACTIVE")

    async def resume(self, tenant_id: str) -> Any:
        return await self._guard("SUSPENDED")


class _Probes:
    def __init__(self, report: Any = None, rows: Any = None) -> None:
        self._report = report
        self._rows = rows or []
        self.recorded_reports: list[Any] = []

    async def report_for(self, tenant_id: str, request_id: UUID, revision: int, tier: str) -> Any:
        return self._report

    async def list_evidence_for(self, scope: Any) -> Any:
        return self._rows

    async def record_evidence(self, scope: Any, evidence: Any) -> Any:
        return SimpleNamespace(
            capability_key=evidence.capability_key, outcome=evidence.outcome.value
        )

    async def record_report(self, report: Any) -> Any:
        self.recorded_reports.append(report)
        return SimpleNamespace(id=uuid4())


class _Coverage:
    def __init__(self, row: Any = None) -> None:
        self.row = row

    async def read_report(
        self, tenant_id: str, request_id: UUID, revision: int, generation: int
    ) -> Any:
        return self.row


def _service(**overrides: Any) -> OnboardingService:
    kwargs: dict[str, Any] = {
        "drafts": _Drafts(),
        "acceptance": _Acceptance(),
        "dispatches": _Dispatches(),
        "registry": _Registry(),
        "probes": _Probes(),
        "coverage": _Coverage(),
    }
    kwargs.update(overrides)
    return OnboardingService(**kwargs)


def _intake() -> Any:
    from investigation_agent_platform.application.onboarding.intake import (
        TenantIntakeEnvelope,
        TenantIntakeV1,
    )

    return TenantIntakeEnvelope(intake=TenantIntakeV1(application_id="app-1", environment="prod"))


async def test_create_request_accepts_after_draft() -> None:
    service = _service(dispatches=_Dispatches(row=None), registry=_Registry(status="ACTIVE"))
    payload = await service.create_request(
        tenant_id="tenant-a",
        actor_sub="actor",
        idempotency_key="k1",
        intake=_intake(),
    )
    assert payload.dispatch_state == "QUEUED"
    assert payload.revision == 1


async def test_create_request_maps_idempotency_conflict() -> None:
    service = _service(drafts=_Drafts(behavior="conflict"), registry=_Registry(status="ACTIVE"))
    with pytest.raises(ServiceError) as exc:
        await service.create_request(
            tenant_id="tenant-a", actor_sub="a", idempotency_key="k", intake=_intake()
        )
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"
    assert exc.value.http_status == 409


async def test_create_request_maps_stale_accept() -> None:
    service = _service(
        acceptance=_Acceptance(behavior="stale"), registry=_Registry(status="ACTIVE")
    )
    with pytest.raises(ServiceError) as exc:
        await service.create_request(
            tenant_id="tenant-a", actor_sub="a", idempotency_key="k", intake=_intake()
        )
    assert exc.value.code == "STALE_REVISION"


async def test_get_status_404_without_row() -> None:
    service = _service(dispatches=_Dispatches(row=None))
    with pytest.raises(ServiceError) as exc:
        await service.get_status("tenant-a", uuid4())
    assert exc.value.code == "REQUEST_NOT_FOUND"


async def test_get_status_reports_readiness_and_registry() -> None:
    request_id = uuid4()
    now = datetime.now(UTC)
    report = SimpleNamespace(
        decision="READY", blocking_codes=[], expires_at=now + timedelta(hours=1)
    )
    service = _service(
        dispatches=_Dispatches(row=_request_row(request_id)),
        probes=_Probes(report=report),
        registry=_Registry(status="ACTIVE"),
    )
    status = await service.get_status("tenant-a", request_id)
    assert status.state == "QUEUED"
    assert status.readiness_decision == "READY"
    assert status.registry_status == "ACTIVE"


async def test_cancel_moves_and_rejects_terminal() -> None:
    request_id = uuid4()
    service = _service(dispatches=_Dispatches(row=_request_row(request_id, state="QUEUED")))
    status = await service.cancel_request("tenant-a", request_id, 1)
    assert status.request_id == request_id

    stuck = _Dispatches(row=_request_row(request_id, state="CANCELED"))
    stuck.cancel_moved = False
    with pytest.raises(ServiceError) as exc:
        await _service(dispatches=stuck).cancel_request("tenant-a", request_id, 1)
    assert exc.value.code == "INVALID_STATE"


async def test_retry_moves_only_retryable() -> None:
    service = _service()
    result = await service.retry_dispatch("tenant-a", uuid4())
    assert result["retried"] is True

    dead = _Dispatches()
    dead.retry_moved = False
    with pytest.raises(ServiceError) as exc:
        await _service(dispatches=dead).retry_dispatch("tenant-a", uuid4())
    assert exc.value.code == "INVALID_STATE"


async def test_suspend_and_resume() -> None:
    suspended = await _service(registry=_Registry(status="ACTIVE")).suspend_tenant("tenant-a")
    assert suspended["status"] == "SUSPENDED"
    resumed = await _service(registry=_Registry(status="SUSPENDED")).resume_tenant("tenant-a")
    assert resumed["status"] == "ACTIVE"
    with pytest.raises(ServiceError) as exc:
        await _service(registry=_Registry(status="ACTIVE")).resume_tenant("tenant-a")
    assert exc.value.code == "INVALID_STATE"


async def test_approve_requires_ready_report() -> None:
    request_id = uuid4()
    now = datetime.now(UTC)
    ready = SimpleNamespace(
        decision="READY",
        blocking_codes=[],
        expires_at=now + timedelta(hours=1),
        policy_version="p1",
        catalog_version="v0.1",
    )
    acceptance = _Acceptance()
    service = _service(
        dispatches=_Dispatches(row=_request_row(request_id)),
        probes=_Probes(report=ready),
        registry=_Registry(status="RESERVED"),
        acceptance=acceptance,
    )
    receipt = await service.approve_and_activate(
        tenant_id="tenant-a",
        actor_sub="approver",
        request_id=request_id,
        revision=1,
        idempotency_key="act-1",
    )
    assert receipt["status"] == "ACTIVE"
    assert acceptance.activation_calls == 1


async def test_approve_rejects_missing_and_blocked_reports() -> None:
    request_id = uuid4()
    now = datetime.now(UTC)
    base = _request_row(request_id)
    blocked = SimpleNamespace(
        decision="BLOCKED",
        blocking_codes=["REPOSITORY_NOT_VERIFIED"],
        expires_at=now + timedelta(hours=1),
        policy_version="p1",
        catalog_version="v0.1",
    )
    with pytest.raises(ServiceError) as exc:
        await _service(
            dispatches=_Dispatches(row=base), probes=_Probes(report=blocked)
        ).approve_and_activate(
            tenant_id="tenant-a",
            actor_sub="a",
            request_id=request_id,
            revision=1,
            idempotency_key="k",
        )
    assert exc.value.code == "NOT_READY"

    with pytest.raises(ServiceError) as exc:
        await _service(dispatches=_Dispatches(row=base)).approve_and_activate(
            tenant_id="tenant-a",
            actor_sub="a",
            request_id=request_id,
            revision=1,
            idempotency_key="k",
        )
    assert exc.value.code == "NOT_READY"


async def test_get_report_404_and_hit() -> None:
    request_id = uuid4()
    with pytest.raises(ServiceError) as exc:
        await _service().get_report("tenant-a", request_id, 1, 1)
    assert exc.value.code == "REQUEST_NOT_FOUND"

    row = SimpleNamespace(report_content_hash="d" * 64, report_json={"ok": True})
    report = await _service(coverage=_Coverage(row=row)).get_report("tenant-a", request_id, 1, 1)
    assert report["report_content_hash"] == "d" * 64


async def test_evaluate_and_file_readiness_ready_path() -> None:
    from investigation_agent_platform.application.onboarding.adapters import (
        AdapterReference,
        AdapterScope,
        SampleDatabaseProviderAdapter,
        SampleGitHostAdapter,
    )

    request_id = uuid4()
    scope = AdapterScope(
        tenant_id="tenant-a",
        application_id="app-1",
        environment="prod",
        request_id=request_id,
        revision=1,
        policy_version="p1",
        catalog_version="v0.1",
    )
    now = datetime.now(UTC)
    git = SampleGitHostAdapter({"org/repo": {"sha": "a"}})
    db = SampleDatabaseProviderAdapter({"db-main": {"connected": True}})
    git_evidence = git.probe(
        scope,
        AdapterReference(kind="repository", provider_key="sample-git-host", resource_id="org/repo"),
        now=now,
    )
    db_evidence = db.probe(
        scope,
        AdapterReference(
            kind="datasource", provider_key="sample-database-provider", resource_id="db-main"
        ),
        now=now,
    )
    assert git_evidence.outcome.value == "VERIFIED"
    assert db_evidence.outcome.value == "VERIFIED"
    stored = [
        SimpleNamespace(
            adapter=evidence.adapter,
            adapter_version=evidence.adapter_version,
            capability_key=evidence.capability_key,
            tenant_id=evidence.tenant_id,
            application_id=evidence.application_id,
            environment=evidence.environment,
            request_id=evidence.request_id,
            revision=evidence.revision,
            policy_version=evidence.policy_version,
            catalog_version=evidence.catalog_version,
            outcome=evidence.outcome,
            diagnostic_code=evidence.diagnostic_code,
            detail=evidence.detail,
            auth_mode=evidence.auth_mode,
            probed_at=evidence.probed_at,
            expires_at=evidence.expires_at,
        )
        for evidence in (git_evidence, db_evidence)
    ]
    probes = _Probes(rows=stored)
    service = _service(dispatches=_Dispatches(row=_request_row(request_id)), probes=probes)
    result = await service.evaluate_and_file_readiness(
        tenant_id="tenant-a", request_id=request_id, revision=1
    )
    assert result["decision"] == "READY"
    assert result["blocking_codes"] == []
    assert len(probes.recorded_reports) == 1


def test_capabilities_lists_catalog_and_rejects_unknown_tier() -> None:
    service = _service()
    capabilities = service.capabilities()
    keys = {entry["capability_key"] for entry in capabilities}
    assert {"repository", "datasource", "activation"} <= keys
    with pytest.raises(ServiceError) as exc:
        service.capabilities(tier="nope")
    assert exc.value.code == "UNKNOWN_TIER"


async def test_create_request_honors_quota_gate() -> None:
    active = _Registry(status="ACTIVE")
    denied = _service(quota_gate=lambda tenant, operation: False, registry=active)
    with pytest.raises(ServiceError) as exc:
        await denied.create_request(
            tenant_id="tenant-a", actor_sub="a", idempotency_key="k", intake=_intake()
        )
    assert exc.value.code == "QUOTA_EXCEEDED"
    assert exc.value.http_status == 429

    allowed = _service(quota_gate=lambda tenant, operation: True, registry=active)
    payload = await allowed.create_request(
        tenant_id="tenant-a", actor_sub="a", idempotency_key="k", intake=_intake()
    )
    assert payload.dispatch_state == "QUEUED"


class _CountingDrafts(_Drafts):
    def __init__(self) -> None:
        super().__init__()
        self.created = 0

    async def create_draft(self, **kwargs: Any) -> SimpleNamespace:
        self.created += 1
        return await super().create_draft(**kwargs)


async def _create(service: OnboardingService, tenant_id: str) -> Any:
    return await service.create_request(
        tenant_id=tenant_id, actor_sub="a", idempotency_key="k", intake=_intake()
    )


@pytest.mark.parametrize("status", ["RESERVED", "SUSPENDED", None])
async def test_new_tenant_with_dash_is_rejected_before_any_write(status: str | None) -> None:
    drafts = _CountingDrafts()
    quota_calls: list[str] = []
    service = _service(
        drafts=drafts,
        registry=_Registry(status=status),
        quota_gate=lambda tenant, operation: quota_calls.append(tenant) or True,
    )
    with pytest.raises(ServiceError) as exc:
        await _create(service, "acme-eu")
    assert exc.value.code == "TENANT_ID_NOT_ALLOWED"
    assert exc.value.http_status == 400
    assert drafts.created == 0
    assert quota_calls == []


async def test_new_tenant_that_is_dash_prefix_of_registered_tenant_is_rejected() -> None:
    drafts = _CountingDrafts()
    service = _service(
        drafts=drafts, registry=_Registry(status="RESERVED", registered_ids=["acme-eu", "acme"])
    )
    with pytest.raises(ServiceError) as exc:
        await _create(service, "acme")
    assert exc.value.code == "TENANT_ID_NOT_ALLOWED"
    # The error never names the sibling tenant.
    assert "acme-eu" not in exc.value.message
    assert drafts.created == 0


async def test_new_dash_free_tenant_without_prefix_collision_is_accepted() -> None:
    service = _service(
        dispatches=_Dispatches(row=None),
        registry=_Registry(status="RESERVED", registered_ids=["acmeeu", "other-co", "acme"]),
    )
    payload = await _create(service, "acme")
    assert payload.dispatch_state == "QUEUED"


async def test_active_dashed_tenant_is_untouched_even_with_a_dash_sibling() -> None:
    service = _service(
        dispatches=_Dispatches(row=None),
        registry=_Registry(status="ACTIVE", registered_ids=["n-a", "n-a-b", "n"]),
    )
    payload = await _create(service, "n-a")
    assert payload.dispatch_state == "QUEUED"
