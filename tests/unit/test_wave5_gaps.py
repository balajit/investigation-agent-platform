# tests/unit/test_wave5_gaps.py
"""Wave 5 gap tests (TO_DO-009/010/018/026/027/028/029/030).

Pins the Wave 5 fixes without touching other waves' files: log-redaction
policy, MCP failure-telemetry shape, router-local body placement, POST
/batch-intake shape, chat SSE production fail-closed guard, artifact key
scoping, stdio actor attribution, and knowledge cross-tenant redaction.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

# --- TO_DO-009: sanitize_extra redaction -------------------------------------


def test_sanitize_extra_redacts_secret_bearing_values() -> None:
    from investigation_agent_platform.api.errors import sanitize_extra

    extra = sanitize_extra(
        {
            "tenant_id": "tenant-a",
            "token": "Bearer abcdef123456",
            "api_key": "sk-test-secret",
            "nested": {"password": "hunter2", "count": 3},
        }
    )
    assert extra["tenant_id"] == "tenant-a"
    assert extra["token"] == "[REDACTED]"
    assert extra["api_key"] == "[REDACTED]"
    # Secret-bearing keys are redacted along with their names (code wins:
    # redact_value replaces the key with [REDACTED_KEY]).
    assert extra["nested"] == {"[REDACTED_KEY]": "[REDACTED]", "count": 3}


# --- TO_DO-010: MCP tool failure telemetry ------------------------------------


def _scope(tenant: str = "tenant-a"):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.investigation.context import (
        InvestigationScope,
    )

    return InvestigationScope.create(tenant_id=tenant, investigation_id=uuid4())


@pytest.mark.asyncio
async def test_mcp_tool_failure_logs_exception_with_top_level_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from investigation_agent_platform.domain.common.exceptions import (
        EvidenceNotFoundException,
    )
    from investigation_agent_platform.mcp.errors import McpErrorCode, McpErrorEnvelope
    from investigation_agent_platform.mcp.tools.evidence_detail import get_evidence_core

    services = MagicMock()
    services.evidence_detail.get = AsyncMock(side_effect=EvidenceNotFoundException("nope"))
    scope = _scope()
    with caplog.at_level(
        logging.WARNING, logger="investigation_agent_platform.mcp.tools.evidence_detail"
    ):
        output = await get_evidence_core(
            services=services, scope=scope, evidence_id=uuid4()
        )
    assert isinstance(output, McpErrorEnvelope)
    assert output.error.code == McpErrorCode.EVIDENCE_NOT_FOUND
    failures = [r for r in caplog.records if "failed" in r.getMessage()]
    assert failures, "expected a failure log record"
    record = failures[0]
    assert record.levelno >= logging.WARNING
    assert record.exc_info is not None, "failure path must carry a stack trace"
    assert getattr(record, "tenant_id", None) == scope.tenant_id
    assert getattr(record, "investigation_id", None) == str(scope.investigation_id)
    assert getattr(record, "event", None) == "mcp.get_evidence.failed"
    assert getattr(record, "error_code", None) == McpErrorCode.EVIDENCE_NOT_FOUND.value


@pytest.mark.asyncio
async def test_mcp_unhandled_failure_logs_exception_with_scope_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from investigation_agent_platform.mcp.errors import error_envelope

    with caplog.at_level(logging.WARNING, logger="investigation_agent_platform.mcp.errors"):
        envelope = error_envelope(
            RuntimeError("boom"), tenant_id="tenant-a", investigation_id="inv-1"
        )
    assert envelope.error.code.value == "INTERNAL_ERROR"
    assert "boom" not in envelope.error.message
    records = [r for r in caplog.records if "Unhandled MCP tool failure" in r.getMessage()]
    assert records and records[0].exc_info is not None
    assert getattr(records[0], "tenant_id", None) == "tenant-a"


# --- TO_DO-018: router-local bodies live in schemas/ --------------------------


def test_no_router_local_body_classes() -> None:
    routers = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "investigation_agent_platform"
        / "api"
        / "v1"
        / "routers"
    )
    offenders = [
        f"{path.name}:{match}"
        for path in sorted(routers.glob("*.py"))
        for match in re.findall(r"^class (\w*Body)\b", path.read_text(), re.MULTILINE)
    ]
    assert offenders == [], f"router-local body classes remain: {offenders}"

def test_moved_bodies_importable_from_schemas_and_routers() -> None:
    # NOTE: `api.v1.routers/__init__` rebinds submodule names to APIRouter
    # objects (e.g. routers.investigations is the router), so import the
    # bodies from the submodule path rather than via the package attribute.
    from investigation_agent_platform.api.v1.routers.aggregate_reports import (
        BuildReportBody as RouterBuildReportBody,
    )
    from investigation_agent_platform.api.v1.routers.batch_intake import (
        BatchIntakeBody as RouterBatchIntakeBody,
    )
    from investigation_agent_platform.api.v1.routers.batch_intake import (
        CsvPayload as RouterCsvPayload,
    )
    from investigation_agent_platform.api.v1.routers.chat import (
        CreateSessionBody as RouterCreateSessionBody,
    )
    from investigation_agent_platform.api.v1.routers.chat import (
        PostMessageBody as RouterPostMessageBody,
    )
    from investigation_agent_platform.api.v1.routers.clusters import (
        RunClustersBody as RouterRunClustersBody,
    )
    from investigation_agent_platform.api.v1.routers.investigations import (
        CreateInvestigationBody as RouterCreateInvestigationBody,
    )
    from investigation_agent_platform.api.v1.routers.investigations import (
        ErrorIntakeBody as RouterErrorIntakeBody,
    )
    from investigation_agent_platform.api.v1.routers.investigations import (
        FulfillInputBody as RouterFulfillInputBody,
    )
    from investigation_agent_platform.api.v1.routers.reference_docs import (
        ReindexBody as RouterReindexBody,
    )
    from investigation_agent_platform.api.v1.routers.reference_docs import (
        RollbackBody as RouterRollbackBody,
    )
    from investigation_agent_platform.api.v1.schemas import aggregate_reports as agg_schemas
    from investigation_agent_platform.api.v1.schemas import batch_intake as batch_schemas
    from investigation_agent_platform.api.v1.schemas import chat as chat_schemas
    from investigation_agent_platform.api.v1.schemas import clusters as cluster_schemas
    from investigation_agent_platform.api.v1.schemas import investigations as inv_schemas
    from investigation_agent_platform.api.v1.schemas import reference_docs as ref_schemas

    assert RouterCreateInvestigationBody is inv_schemas.CreateInvestigationBody
    assert RouterFulfillInputBody is inv_schemas.FulfillInputBody
    assert RouterErrorIntakeBody is inv_schemas.ErrorIntakeBody
    assert RouterCreateSessionBody is chat_schemas.CreateSessionBody
    assert RouterPostMessageBody is chat_schemas.PostMessageBody
    assert RouterBatchIntakeBody is batch_schemas.BatchIntakeBody
    assert RouterCsvPayload is batch_schemas.CsvPayload
    assert RouterReindexBody is ref_schemas.ReindexBody
    assert RouterRollbackBody is ref_schemas.RollbackBody
    assert RouterBuildReportBody is agg_schemas.BuildReportBody
    assert RouterRunClustersBody is cluster_schemas.RunClustersBody


# --- TO_DO-026: POST /batch-intake returns BatchSubmitAck ---------------------


def test_post_batch_intake_shape_is_ack_not_detail() -> None:
    from investigation_agent_platform.api.app import create_app

    spec = create_app().openapi()
    op = spec["paths"]["/api/v1/batch-intake"]["post"]
    schema = op["responses"]["202"]["content"]["application/json"]["schema"]
    ref = schema.get("$ref", "")
    assert ref.endswith("/BatchSubmitAck"), f"POST shape moved: {ref}"


# --- TO_DO-027: production chat guard fails closed ----------------------------


def _chat_client(monkeypatch) -> tuple:  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from investigation_agent_platform.api.app import create_app
    from investigation_agent_platform.api.dependencies import AppContext, set_app_context
    from investigation_agent_platform.api.tenant import require_principal, require_tenant

    monkeypatch.setenv("IAP_ENVIRONMENT", "production")
    # No LLM/database env: quota-policy config load fails, so the guard path
    # is exercised deterministically.
    monkeypatch.delenv("IAP_DATABASE_URI", raising=False)
    ctx = AppContext()
    set_app_context(ctx)
    app = create_app()
    # Production routes require verified JWT; override identity deps so the
    # test exercises the quota guard, not the auth layer.
    app.dependency_overrides[require_tenant] = lambda: "tenant-a"
    app.dependency_overrides[require_principal] = lambda: "test-principal"
    return TestClient(app), ctx


def test_chat_session_create_fails_closed_without_quota_guard(monkeypatch) -> None:
    client, _ = _chat_client(monkeypatch)
    resp = client.post(
        "/api/v1/chat/sessions",
        json={},
        headers={"X-Tenant-ID": "tenant-a", "X-Idempotency-Key": "guard-1"},
    )
    assert resp.status_code == 429, resp.text
    assert resp.headers.get("Retry-After") == "60"


def test_chat_sse_fails_closed_without_quota_guard(monkeypatch) -> None:
    import anyio

    from investigation_agent_platform.application.chat.chat_service import ChatService

    client, ctx = _chat_client(monkeypatch)

    async def _seed() -> str:
        service = ChatService(chat_repo=getattr(ctx, "chat_repo", None))
        session = await service.create_session("tenant-a")
        return str(session.id)

    session_id = anyio.run(_seed)
    resp = client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"content": "hello"},
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert resp.status_code == 429, resp.text


def test_chat_guard_best_effort_outside_production(monkeypatch) -> None:
    from investigation_agent_platform.api.v1.routers.chat import _require_quota_guard

    monkeypatch.setenv("IAP_ENVIRONMENT", "development")
    # Must not raise when the guard is missing outside production.
    _require_quota_guard("tenant-a", None, None)


# --- TO_DO-028: artifact key scoping ------------------------------------------


def test_artifact_keys_are_tenant_prefixed_and_unforgeable() -> None:
    from investigation_agent_platform.infrastructure.artifacts.keys import build_object_key

    key_a = build_object_key("tenant-a", "app-1", "reports/r1.html")
    key_b = build_object_key("tenant-b", "app-1", "reports/r1.html")
    assert key_a.startswith("tenant-a/app-1/")
    # Same logical key under another tenant never reuses the storage key.
    assert key_a != key_b
    assert not key_b.startswith("tenant-a/")
    # A tenant-b caller addressing tenant-a's logical key lands under its own
    # prefix — there is no path to the other tenant's object.
    assert build_object_key("tenant-b", "app-1", "reports/r1.html") != key_a


def test_artifact_key_traversal_rejected() -> None:
    import pytest as _pytest

    from investigation_agent_platform.infrastructure.artifacts.keys import build_object_key

    for bad in ("../escape", "/absolute", "a/../../b", "", "ok\\win", "a//b"):
        with _pytest.raises(ValueError):
            build_object_key("tenant-a", "app-1", bad)
    for bad_segment in ("..", ".", "a/b", "a\\b", ""):
        with _pytest.raises(ValueError):
            build_object_key(bad_segment, "app-1", "k")


def test_resolve_mcp_none_contract_documented() -> None:
    import inspect

    from investigation_agent_platform.infrastructure.evidence.datasources.registry import (
        DatasourceRegistry,
    )

    doc = inspect.getdoc(DatasourceRegistry.resolve_mcp) or ""
    assert "None" in doc and "fail-closed" in doc.lower()


# --- TO_DO-029: stdio actor default warns outside local dev -------------------


def test_stdio_default_actor_warns_outside_local(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from investigation_agent_platform.mcp.context import stdio_context_from_env

    monkeypatch.setenv("IAP_ENVIRONMENT", "production")
    investigation_id = str(uuid4())
    with caplog.at_level(logging.WARNING, logger="investigation_agent_platform.mcp.context"):
        context = stdio_context_from_env("tenant-a", investigation_id, None)
    assert context.actor_id == "local-operator"
    events = [getattr(r, "event", "") for r in caplog.records]
    assert "mcp.stdio.default_actor" in events


def test_stdio_explicit_actor_is_quiet(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from investigation_agent_platform.mcp.context import stdio_context_from_env

    monkeypatch.setenv("IAP_ENVIRONMENT", "production")
    with caplog.at_level(logging.WARNING, logger="investigation_agent_platform.mcp.context"):
        context = stdio_context_from_env("tenant-a", str(uuid4()), "op-1")
    assert context.actor_id == "op-1"
    assert "mcp.stdio.default_actor" not in [
        getattr(r, "event", "") for r in caplog.records
    ]


# --- TO_DO-030: OtherTenantSessionView redaction ------------------------------


def test_other_tenant_session_view_redacts_identity_fields() -> None:
    from pydantic import ValidationError

    from investigation_agent_platform.api.v1.schemas.knowledge import OtherTenantSessionView

    view = OtherTenantSessionView(
        session_number=3, occurred_at="2026-10-01T00:00:00Z", tenant="other"
    )
    dumped = view.model_dump(mode="json")
    # Exactly the redacted surface: counts/numbers truthful, identity never leaves.
    assert set(dumped) == {"session_number", "occurred_at", "tenant"}
    for forbidden in (
        "log_refs",
        "trace_refs",
        "status",
        "content",
        "messages",
        "tenant_id",
        "investigation_id",
        "artifacts",
    ):
        assert forbidden not in dumped
    with pytest.raises(ValidationError):
        OtherTenantSessionView(
            session_number=1,
            occurred_at="2026-10-01T00:00:00Z",
            tenant="other",
            log_refs=["leak"],  # type: ignore[call-arg]
        )
