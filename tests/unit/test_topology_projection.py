# tests/unit/test_topology_projection.py
"""Scheduled topology projection + GitHub webhook (cognee pilot wiring)."""

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import AppContext, set_app_context


@pytest.fixture
def _restore_context():
    import investigation_agent_platform.api.dependencies as deps

    prev = deps._context
    yield
    deps._context = prev


def _client(monkeypatch):
    monkeypatch.setenv("IAP_ENVIRONMENT", "development")
    ctx = AppContext()
    set_app_context(ctx)
    return TestClient(create_app()), ctx


def _headers(tenant="tenant-a"):
    return {"X-Tenant-ID": tenant}


def _run(coro):
    import asyncio as _asyncio

    return _asyncio.run(coro)


# --- activity ---


def test_activity_skips_when_disabled(monkeypatch):
    from investigation_agent_platform.application.worker.activities import (
        ProjectTopologyInput,
        project_topology_activity,
    )

    monkeypatch.delenv("IAP_COGNEE_ENABLED", raising=False)
    out = _run(project_topology_activity(ProjectTopologyInput(tenant_id="t", repository_id="r")))
    assert out.success is True and out.data["status"] == "skipped"


def test_activity_rejects_traversal(monkeypatch, tmp_path):
    from investigation_agent_platform.application.worker.activities import (
        ProjectTopologyInput,
        project_topology_activity,
    )

    monkeypatch.setenv("IAP_COGNEE_ENABLED", "true")
    monkeypatch.setenv("IAP_COGNEE_DATASET_SALT", "salt")
    monkeypatch.setenv("IAP_CODE_REPO_BASE", str(tmp_path))
    out = _run(
        project_topology_activity(
            ProjectTopologyInput(tenant_id="t", repository_id="r", checkout="../../etc")
        )
    )
    assert out.success is True and out.data["status"] == "skipped"


def test_activity_skips_absent_checkout(monkeypatch, tmp_path):
    from investigation_agent_platform.application.worker.activities import (
        ProjectTopologyInput,
        project_topology_activity,
    )

    monkeypatch.setenv("IAP_COGNEE_ENABLED", "true")
    monkeypatch.setenv("IAP_COGNEE_DATASET_SALT", "salt")
    monkeypatch.setenv("IAP_CODE_REPO_BASE", str(tmp_path))
    out = _run(
        project_topology_activity(
            ProjectTopologyInput(tenant_id="t", repository_id="nope", checkout="nope")
        )
    )
    assert out.success is True and out.data["status"] == "skipped"


def test_activity_projects(monkeypatch, tmp_path):
    import investigation_agent_platform.application.worker.activities as activities_mod
    from investigation_agent_platform.application.worker.activities import (
        ProjectTopologyInput,
        project_topology_activity,
    )

    repo = tmp_path / "acme--pay"
    repo.mkdir()
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setenv("IAP_COGNEE_ENABLED", "true")
    monkeypatch.setenv("IAP_COGNEE_DATASET_SALT", "salt")
    monkeypatch.setenv("IAP_CODE_REPO_BASE", str(tmp_path))

    async def _fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(activities_mod.asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(
        "investigation_agent_platform.infrastructure.topology.payload.build_payload_for_repo",
        AsyncMock(
            return_value=(
                SimpleNamespace(
                    source_files=[], ast_nodes=[], calls=[], references=[],
                    database_accesses=[], diagnostics=[],
                ),
                False,
            )
        ),
    )

    holder = MagicMock()
    holder.connect = AsyncMock(return_value=holder)
    holder.close = AsyncMock()
    result = SimpleNamespace(dataset="ds-1", point_count=10, edge_count=4)
    adapter = MagicMock()
    adapter.project_snapshot = AsyncMock(return_value=result)
    monkeypatch.setattr(
        "investigation_agent_platform.infrastructure.topology.cognee_adapter.CogneeTopologyAdapter",
        MagicMock(return_value=adapter),
    )
    monkeypatch.setattr(
        "investigation_agent_platform.infrastructure.topology.cognee_adapter.SharedNeo4jDriver",
        MagicMock(**{"from_env.return_value": holder}),
    )
    out = _run(
        project_topology_activity(
            ProjectTopologyInput(
                tenant_id="t", repository_id="acme--pay", revision="abc123",
                checkout="acme--pay", changed_files=["a.py"],
            )
        )
    )
    assert out.success is True and out.data["status"] == "projected"
    assert out.data["dataset"] == "ds-1" and out.data["points"] == 10
    assert out.data["changed_files"] == ["a.py"]
    holder.close.assert_awaited_once()


# --- webhook ---


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _push(full_name="acme/pay", after="f" * 40):
    return {
        "ref": "refs/heads/main",
        "after": after,
        "repository": {"full_name": full_name},
        "commits": [
            {"added": ["src/a.py"], "modified": ["src/b.py"], "removed": []},
            {"added": [], "modified": [], "removed": ["old.py"]},
        ],
    }


def test_webhook_rejects_bad_signature(monkeypatch):
    client, _ = _client(monkeypatch)
    monkeypatch.setenv("IAP_GITHUB_WEBHOOK_SECRET", "s3cret")
    body = json.dumps(_push()).encode()
    resp = client.post(
        "/api/v1/integrations/github/tenant-a/webhook",
        content=body,
        headers={**_headers(), "X-Hub-Signature-256": "sha256=wrong", "X-GitHub-Event": "push"},
    )
    assert resp.status_code == 403


def test_webhook_requires_configured_secret(monkeypatch):
    client, _ = _client(monkeypatch)
    monkeypatch.delenv("IAP_GITHUB_WEBHOOK_SECRET", raising=False)
    resp = client.post(
        "/api/v1/integrations/github/tenant-a/webhook",
        content=b"{}",
        headers={**_headers(), "X-GitHub-Event": "push"},
    )
    assert resp.status_code == 503


def test_webhook_ignores_non_push(monkeypatch):
    client, _ = _client(monkeypatch)
    monkeypatch.setenv("IAP_GITHUB_WEBHOOK_SECRET", "s3cret")
    body = json.dumps({"zen": "hi"}).encode()
    resp = client.post(
        "/api/v1/integrations/github/tenant-a/webhook",
        content=body,
        headers={**_headers(), "X-Hub-Signature-256": _sign("s3cret", body), "X-GitHub-Event": "ping"},
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "ignored"


def test_webhook_skips_branch_deletion(monkeypatch):
    client, _ = _client(monkeypatch)
    monkeypatch.setenv("IAP_GITHUB_WEBHOOK_SECRET", "s3cret")
    body = json.dumps(_push(after="0" * 40)).encode()
    resp = client.post(
        "/api/v1/integrations/github/tenant-a/webhook",
        content=body,
        headers={**_headers(), "X-Hub-Signature-256": _sign("s3cret", body), "X-GitHub-Event": "push"},
    )
    assert resp.status_code == 202
    assert resp.json()["status"] == "skipped"


def test_webhook_dispatches_push(monkeypatch):
    client, ctx = _client(monkeypatch)
    monkeypatch.setenv("IAP_GITHUB_WEBHOOK_SECRET", "s3cret")
    ctx.temporal_client = MagicMock()
    ctx.temporal_client.start_workflow = AsyncMock()
    body = json.dumps(_push()).encode()
    resp = client.post(
        "/api/v1/integrations/github/tenant-a/webhook",
        content=body,
        headers={**_headers(), "X-Hub-Signature-256": _sign("s3cret", body), "X-GitHub-Event": "push"},
    )
    assert resp.status_code == 202, resp.text
    data = resp.json()
    assert data["status"] == "dispatched"
    assert data["repository"] == "acme/pay"
    assert data["changed_files"] == 3
    assert data["workflow_id"].startswith("wf-topology-tenant-a-acme--pay-")
    ctx.temporal_client.start_workflow.assert_awaited_once()


def test_webhook_tenant_mismatch(monkeypatch):
    client, _ = _client(monkeypatch)
    monkeypatch.setenv("IAP_GITHUB_WEBHOOK_SECRET", "s3cret")
    body = json.dumps(_push()).encode()
    resp = client.post(
        "/api/v1/integrations/github/other-tenant/webhook",
        content=body,
        headers={**_headers("tenant-a"), "X-Hub-Signature-256": _sign("s3cret", body), "X-GitHub-Event": "push"},
    )
    assert resp.status_code == 403


def test_changed_files_union_and_cap():
    import investigation_agent_platform.api.v1.routers.integrations as router_mod

    payload = {
        "commits": [
            {"added": ["a.py", "a.py"], "modified": ["b.py"], "removed": ["a.py"]},
        ]
    }
    assert router_mod._changed_files(payload) == ["a.py", "b.py"]
    big = {"commits": [{"added": [f"f{i}.py" for i in range(600)], "modified": [], "removed": []}]}
    assert len(router_mod._changed_files(big)) == 500
