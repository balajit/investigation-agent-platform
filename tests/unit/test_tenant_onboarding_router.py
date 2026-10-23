"""Onboarding route contracts: enabled service behavior and explicit 501s."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import yaml
from fastapi.testclient import TestClient

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import AppContext, set_app_context
from investigation_agent_platform.application.onboarding.intake import (
    TenantIntakeEnvelope,
    TenantIntakeV1,
)
from investigation_agent_platform.application.onboarding.service import ServiceError

HEADERS = {"X-Tenant-ID": "tenant-a", "X-Idempotency-Key": "route-test-1"}
REQUEST_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class _FakeService:
    def __init__(self, behavior: str = "ok") -> None:
        self.behavior = behavior

    async def create_request(self, **kwargs: Any) -> SimpleNamespace:
        if self.behavior == "conflict":
            raise ServiceError("IDEMPOTENCY_CONFLICT", 409, "bound to different content")
        return SimpleNamespace(
            tenant_id=kwargs["tenant_id"],
            request_id=UUID(REQUEST_ID),
            revision=1,
            job_id=uuid4(),
            dispatch_id=uuid4(),
            workflow_id="wf-1",
            dispatch_state="QUEUED",
        )

    async def put_intake(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "tenant_id": kwargs["tenant_id"],
            "request_id": kwargs["request_id"],
            "revision": kwargs["expected_revision"] + 1,
            "content_hash": "c" * 64,
            "accepted": False,
        }

    async def get_status(self, tenant_id: str, request_id: UUID) -> SimpleNamespace:
        if str(request_id) == "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb":
            raise ServiceError("REQUEST_NOT_FOUND", 404, "not found")
        return SimpleNamespace(
            tenant_id=tenant_id,
            request_id=request_id,
            revision=1,
            state="QUEUED",
            state_version=2,
            job_id=None,
            readiness_decision=None,
            blocking_codes=[],
            registry_status="RESERVED",
        )

    async def approve_and_activate(self, **kwargs: Any) -> dict[str, Any]:
        return {"tenant_id": kwargs["tenant_id"], "status": "ACTIVE", "epoch": 2}

    async def retry_dispatch(self, tenant_id: str, dispatch_id: UUID) -> dict[str, Any]:
        return {"tenant_id": tenant_id, "dispatch_id": dispatch_id, "retried": True}

    async def cancel_request(
        self, tenant_id: str, request_id: UUID, revision: int
    ) -> SimpleNamespace:
        return SimpleNamespace(request_id=request_id, state="CANCEL_REQUESTED")

    async def suspend_tenant(self, tenant_id: str) -> dict[str, Any]:
        return {"tenant_id": tenant_id, "status": "SUSPENDED", "epoch": 2}

    async def resume_tenant(self, tenant_id: str) -> dict[str, Any]:
        return {"tenant_id": tenant_id, "status": "ACTIVE", "epoch": 3}

    def capabilities(self, tier: str = "standard") -> list[dict[str, Any]]:
        if tier == "nope":
            raise ServiceError("UNKNOWN_TIER", 404, "unknown tier")
        return [
            {
                "capability_key": "repository",
                "version": "v0.1",
                "required": True,
                "disposition": "REQUIRED",
                "owner": "profile-service",
                "readiness_probe": "sample-git-host:resolve",
            }
        ]

    async def record_probe_evidence(self, **kwargs: Any) -> dict[str, Any]:
        return {"capability_key": "repository", "outcome": "VERIFIED"}

    async def evaluate_and_file_readiness(self, **kwargs: Any) -> dict[str, Any]:
        return {"decision": "READY", "blocking_codes": []}

    async def get_report(
        self, tenant_id: str, request_id: UUID, revision: int, generation: int
    ) -> dict[str, Any]:
        return {"report_content_hash": "d" * 64, "report": {}}

    async def preview(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "tenant_id": kwargs["tenant_id"],
            "manifest_id": uuid4(),
            "plan_hash": "p" * 64,
            "state": "APPROVAL_PENDING",
            "blocking_codes": [],
            "resource_counts": {"s1": 1},
        }

    async def load_registry(self, deployment_id: str) -> Any:
        return SimpleNamespace(deployment_id=deployment_id)

    async def mint_approval(self, **kwargs: Any) -> dict[str, Any]:
        from datetime import UTC as _UTC
        from datetime import datetime as _datetime
        from datetime import timedelta as _timedelta

        return {
            "tenant_id": kwargs["tenant_id"],
            "manifest_id": kwargs["manifest_id"],
            "approval_token": "tok",
            "expires_at": _datetime.now(_UTC) + _timedelta(hours=1),
            "consumed": False,
        }

    async def consume_approval(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "tenant_id": kwargs["tenant_id"],
            "manifest_id": kwargs["manifest_id"],
            "purge_job_id": uuid4(),
            "consumed": True,
        }

    async def get_deletion(self, tenant_id: str, manifest_id: UUID) -> dict[str, Any]:
        from datetime import UTC as _UTC2
        from datetime import datetime as _datetime2
        from datetime import timedelta as _timedelta2

        return {
            "tenant_id": tenant_id,
            "manifest_id": manifest_id,
            "state": "COMPLETE",
            "plan_hash": "p" * 64,
            "store_registry_version": "reg-v1",
            "outcomes": [{"entry_key": "s1:t:r1", "state": "DONE"}],
            "blocking_codes": [],
            "expires_at": _datetime2.now(_UTC2) + _timedelta2(hours=1),
        }


def _client_with(service: Any | None, cleanup: Any | None = "default") -> TestClient:
    if cleanup == "default":
        cleanup = service
    set_app_context(AppContext(onboarding_service=service, cleanup_service=cleanup))
    return TestClient(create_app())


def _payload() -> dict[str, Any]:
    return {"intake": {"application_id": "app-a", "environment": "test"}}


def test_onboarding_routes_require_authentication() -> None:
    client = _client_with(_FakeService())
    response = client.post(
        "/api/v1/tenants/tenant-a/onboarding/requests",
        json=_payload(),
    )
    assert response.status_code == 401


def test_unconfigured_service_stays_explicitly_not_implemented() -> None:
    client = _client_with(None)
    create = client.post(
        "/api/v1/tenants/tenant-a/onboarding/requests", json=_payload(), headers=HEADERS
    )
    read = client.get(
        f"/api/v1/tenants/tenant-a/onboarding/requests/{REQUEST_ID}",
        headers=HEADERS,
    )
    for response in (create, read):
        assert response.status_code == 501
        assert response.json()["error"]["code"] == "ONBOARDING_NOT_IMPLEMENTED"


def test_create_returns_202_after_accept_and_read_reports_status() -> None:
    client = _client_with(_FakeService())
    create = client.post(
        "/api/v1/tenants/tenant-a/onboarding/requests", json=_payload(), headers=HEADERS
    )
    assert create.status_code == 202
    body = create.json()
    assert body["request_id"] == REQUEST_ID
    assert body["dispatch_state"] == "QUEUED"
    assert body["status_url"].endswith(f"/onboarding/requests/{REQUEST_ID}")

    read = client.get(f"/api/v1/tenants/tenant-a/onboarding/requests/{REQUEST_ID}", headers=HEADERS)
    assert read.status_code == 200
    assert read.json()["state"] == "QUEUED"

    missing = client.get(
        "/api/v1/tenants/tenant-a/onboarding/requests/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        headers=HEADERS,
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "REQUEST_NOT_FOUND"


def test_create_conflict_maps_to_409() -> None:
    client = _client_with(_FakeService(behavior="conflict"))
    response = client.post(
        "/api/v1/tenants/tenant-a/onboarding/requests", json=_payload(), headers=HEADERS
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_create_requires_idempotency_key() -> None:
    client = _client_with(_FakeService())
    response = client.post(
        "/api/v1/tenants/tenant-a/onboarding/requests",
        json=_payload(),
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_lifecycle_transitions_and_capabilities() -> None:
    client = _client_with(_FakeService())
    put = client.put(
        f"/api/v1/tenants/tenant-a/onboarding/requests/{REQUEST_ID}/intake",
        json={"expected_revision": 1, "idempotency_key": "k2", **_payload()},
        headers=HEADERS,
    )
    assert put.status_code == 200
    assert put.json()["revision"] == 2

    approve = client.post(
        f"/api/v1/tenants/tenant-a/onboarding/requests/{REQUEST_ID}:approve",
        json={"revision": 1, "idempotency_key": "act-1"},
        headers=HEADERS,
    )
    assert approve.status_code == 200
    assert approve.json()["status"] == "ACTIVE"

    cancel = client.post(
        f"/api/v1/tenants/tenant-a/onboarding/requests/{REQUEST_ID}:cancel",
        json={"revision": 1},
        headers=HEADERS,
    )
    assert cancel.status_code == 200

    retry = client.post(
        f"/api/v1/tenants/tenant-a/onboarding/requests/{REQUEST_ID}:retry",
        json={"dispatch_id": str(uuid4())},
        headers=HEADERS,
    )
    assert retry.status_code == 200

    suspend = client.post("/api/v1/tenants/tenant-a:suspend", headers=HEADERS)
    assert suspend.json()["status"] == "SUSPENDED"
    resume = client.post("/api/v1/tenants/tenant-a:resume", headers=HEADERS)
    assert resume.json()["status"] == "ACTIVE"

    capabilities = client.get("/api/v1/tenants/tenant-a/capabilities", headers=HEADERS)
    assert capabilities.status_code == 200
    assert capabilities.json()["capabilities"][0]["capability_key"] == "repository"


def test_deletion_lifecycle_over_rest() -> None:
    client = _client_with(_FakeService())
    preview = client.post(
        "/api/v1/tenants/tenant-a:deletion-request",
        json={
            "environment": "prod",
            "deployment_id": "dep-1",
            "policy_version": "p1",
            "requester_id": "req-1",
            "approver_ids": ["approver-1"],
            "backup_id": "backup-1",
            "expires_at": "2026-12-01T00:00:00+00:00",
        },
        headers=HEADERS,
    )
    assert preview.status_code == 201
    manifest_id = preview.json()["manifest_id"]

    mint = client.post(
        f"/api/v1/tenants/tenant-a/deletion-requests/{manifest_id}:approve-mint",
        json={
            "manifest_id": manifest_id,
            "approver_id": "approver-1",
            "requester_id": "req-1",
            "policy_version": "p1",
            "store_registry_version": "reg-v1",
            "expires_at": "2026-12-01T00:00:00+00:00",
        },
        headers=HEADERS,
    )
    assert mint.status_code == 201
    assert mint.json()["consumed"] is False

    consume = client.post(
        f"/api/v1/deletion-requests/{manifest_id}:approve",
        json={"approval_token": "tok", "approver_id": "approver-1"},
        headers=HEADERS,
    )
    assert consume.status_code == 200
    assert consume.json()["consumed"] is True

    status = client.get(f"/api/v1/deletion-requests/{manifest_id}", headers=HEADERS)
    assert status.status_code == 200
    assert status.json()["state"] == "COMPLETE"


def test_deletion_stays_501_without_cleanup_service() -> None:
    client = _client_with(_FakeService(), cleanup=None)
    preview = client.post(
        "/api/v1/tenants/tenant-a:deletion-request",
        json={
            "environment": "prod",
            "deployment_id": "dep-1",
            "policy_version": "p1",
            "requester_id": "req-1",
            "approver_ids": ["approver-1"],
            "backup_id": "backup-1",
            "expires_at": "2026-12-01T00:00:00+00:00",
        },
        headers=HEADERS,
    )
    assert preview.status_code == 501
    assert preview.json()["error"]["code"] == "CLEANUP_NOT_ENABLED"


def test_tenant_mismatch_is_rejected_before_service_effects() -> None:
    client = _client_with(_FakeService())
    response = client.get(
        f"/api/v1/tenants/tenant-b/onboarding/requests/{REQUEST_ID}",
        headers=HEADERS,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TENANT_MISMATCH"


def test_create_body_is_linked_to_typed_intake_schema() -> None:
    spec = create_app().openapi()
    operation = spec["paths"]["/api/v1/tenants/{tenant_id}/onboarding/requests"]["post"]
    body_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert body_schema["$ref"].endswith("/TenantIntakeEnvelope")
    assert TenantIntakeEnvelope.model_config["extra"] == "forbid"


def test_example_yaml_validates_against_typed_intake_and_contains_no_secret_values() -> None:
    path = Path(__file__).resolve().parents[2] / "config" / "tenant-id.env.example.yaml"
    example = yaml.safe_load(path.read_text(encoding="utf-8"))
    intake = TenantIntakeV1.model_validate(example)
    assert intake.application_id == "example-application"
    assert intake.sources[0].secret_references[0].name == "EXAMPLE_DATABASE_READ_ONLY"
    text = path.read_text(encoding="utf-8").lower()
    assert "password:" not in text
    assert "token:" not in text
    assert "https://" not in text
    assert "file:///" not in text
