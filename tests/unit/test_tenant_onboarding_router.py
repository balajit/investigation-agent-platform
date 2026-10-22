"""Inert onboarding route contract and synthetic example tests."""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import AppContext, set_app_context
from investigation_agent_platform.application.onboarding.intake import (
    TenantIntakeEnvelope,
    TenantIntakeV1,
)

HEADERS = {"X-Tenant-ID": "tenant-a"}


def test_onboarding_routes_require_authentication() -> None:
    client = TestClient(create_app())
    response = client.post(
        "/api/v1/tenants/tenant-a/onboarding/requests",
        json={"intake": {"application_id": "app-a", "environment": "test"}},
    )
    assert response.status_code == 401


def test_create_read_and_submit_are_explicitly_not_implemented() -> None:
    set_app_context(AppContext())
    client = TestClient(create_app())
    payload = {
        "intake": {"application_id": "app-a", "environment": "test"},
        "coverage_report": None,
    }

    create = client.post(
        "/api/v1/tenants/tenant-a/onboarding/requests", json=payload, headers=HEADERS
    )
    read = client.get(
        "/api/v1/tenants/tenant-a/onboarding/requests/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        headers=HEADERS,
    )
    submit = client.post(
        "/api/v1/tenants/tenant-a/onboarding/requests/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/submit",
        headers=HEADERS,
    )

    for response in (create, read, submit):
        assert response.status_code == 501
        assert response.json()["error"]["code"] == "ONBOARDING_NOT_IMPLEMENTED"
        assert not 200 <= response.status_code < 300


def test_tenant_mismatch_is_rejected_before_not_implemented_response() -> None:
    set_app_context(AppContext())
    client = TestClient(create_app())
    response = client.get(
        "/api/v1/tenants/tenant-b/onboarding/requests/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
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
