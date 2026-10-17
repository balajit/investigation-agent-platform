# tests/unit/test_investigations_wire.py
"""Golden wire tests for investigations + events routers (OpenAPI spec 1.0).

Pins exact response bodies (including explicit nulls) and the unified
{"error": {...}} envelope across the lifecycle endpoints.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import AppContext, set_app_context

HEADERS = {"X-Tenant-ID": "tenant-a"}


@pytest.fixture
def _restore_context():
    import investigation_agent_platform.api.dependencies as deps

    prev = deps._context
    yield
    deps._context = prev


def _client():
    ctx = AppContext()
    set_app_context(ctx)
    return ctx, TestClient(create_app())


def _create(client, **overrides):
    body = {
        "application_id": "example-app",
        "problem_description": "latency spike after deploy",
        "session_id": "sess-wire-1",
    }
    body.update(overrides)
    return client.post("/api/v1/investigations", json=body, headers=HEADERS)


class TestInvestigationsGolden:
    def test_create_body_exact(self, _restore_context: None) -> None:
        _, client = _client()
        resp = _create(client)
        assert resp.status_code == 201
        body = resp.json()
        assert set(body) == {
            "id",
            "session_id",
            "application_id",
            "status",
            "tenant_id",
            "investigation",
            "disposition",
        }
        assert body["application_id"] == "example-app"
        assert body["tenant_id"] == "tenant-a"
        assert body["investigation"]["application_id"] == "example-app"
        assert body["investigation"]["id"] == body["id"]
        assert body["investigation"]["status"] == "CREATED"
        # §2: fresh request carries a FRESH disposition with no target.
        assert body["disposition"]["action"] == "fresh"
        assert body["disposition"]["start_run"] is True
        assert body["disposition"]["primary_target_id"] is None

    def test_create_replay_adds_only_flag(self, _restore_context: None) -> None:
        _, client = _client()
        headers = {**HEADERS, "X-Idempotency-Key": "wire-replay-1"}
        first = client.post(
            "/api/v1/investigations",
            json={
                "application_id": "example-app",
                "problem_description": "x",
                "session_id": "sess-r",
            },
            headers=headers,
        )
        assert first.status_code == 201
        assert "idempotent_replay" not in first.json()
        second = client.post(
            "/api/v1/investigations",
            json={
                "application_id": "example-app",
                "problem_description": "x",
                "session_id": "sess-r",
            },
            headers=headers,
        )
        assert second.status_code == 201  # replay keeps the create status (pinned)
        # Replay carries the identical payload (no envelope drift on replay).
        assert second.json()["id"] == first.json()["id"]

    def test_list_shape(self, _restore_context: None) -> None:
        _, client = _client()
        _create(client)
        body = client.get("/api/v1/investigations", headers=HEADERS).json()
        assert set(body) == {"items", "total", "offset", "limit"}
        assert body["offset"] == 0
        assert body["total"] == len(body["items"]) >= 1
        assert set(body["items"][0]) == {
            "investigation_id",
            "status",
            "phase",
            "application_id",
            "created_at",
            "updated_at",
        }

    def test_detail_shape(self, _restore_context: None) -> None:
        _, client = _client()
        inv_id = _create(client).json()["id"]
        body = client.get(f"/api/v1/investigations/{inv_id}", headers=HEADERS).json()
        assert set(body) == {"investigation", "pending_requirements", "links"}
        assert body["pending_requirements"] == []
        assert body["links"] == []

    def test_conclusion_null_pinned(self, _restore_context: None) -> None:
        _, client = _client()
        inv_id = _create(client).json()["id"]
        body = client.get(
            f"/api/v1/investigations/{inv_id}/conclusion", headers=HEADERS
        ).json()
        # conclusion:null must be PRESENT (not dropped by exclusion settings).
        assert body == {
            "investigation_id": inv_id,
            "status": "CREATED",
            "conclusion": None,
            "failure": None,
        }

    def test_findings_empty_shape(self, _restore_context: None) -> None:
        _, client = _client()
        inv_id = _create(client).json()["id"]
        body = client.get(
            f"/api/v1/investigations/{inv_id}/findings", headers=HEADERS
        ).json()
        assert body == {"items": [], "total": 0, "offset": 0, "limit": 50}

    def test_start_without_temporal_503_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        inv_id = _create(client).json()["id"]
        resp = client.post(f"/api/v1/investigations/{inv_id}/start", headers=HEADERS)
        assert resp.status_code == 503
        body = resp.json()["error"]
        assert body["code"] == "TEMPORAL_UNAVAILABLE"
        assert resp.headers.get("x-correlation-id") == body["correlation_id"]


class TestInvestigationsErrors:
    def test_unknown_investigation_404_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.get(
            "/api/v1/investigations/00000000-0000-0000-0000-000000000000", headers=HEADERS
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "INVESTIGATION_NOT_FOUND"

    def test_bad_uuid_400_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.get("/api/v1/investigations/not-a-uuid", headers=HEADERS)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_INVESTIGATION_ID"

    def test_invalid_utf8_body_400_envelope(self, _restore_context: None) -> None:
        # Fuzz-found: undecodable bytes bypassed RequestValidationError and
        # FastAPI answered a bare {"detail": ...}, breaking the envelope.
        _, client = _client()
        resp = client.post(
            "/api/v1/investigations",
            content=b"\x80",
            headers={**HEADERS, "Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "ValidationError"

    def test_intake_forbidden_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.post(
            "/api/v1/intake/errors",
            json={"application_id": "x", "error_class": "E"},
            headers=HEADERS,
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "INTAKE_FORBIDDEN"

    def test_fulfill_requires_key_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        inv_id = _create(client).json()["id"]
        resp = client.post(
            f"/api/v1/investigations/{inv_id}/input-requirements/00000000-0000-0000-0000-000000000000/fulfill",
            json={"requirement_version": 1, "data": {}},
            headers=HEADERS,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


class TestConclusionFailure:
    def _failed_inv(self, ctx, metadata):
        import asyncio as _asyncio
        from datetime import UTC, datetime, timedelta

        from investigation_agent_platform.domain.investigation.models import (
            Investigation,
            InvestigationContext,
            InvestigationRequest,
            InvestigationStatus,
        )

        now = datetime.now(UTC)
        inv = Investigation(
            session_id="sess-fail-1",
            application_id="example-app",
            tenant_id="tenant-a",
            request=InvestigationRequest(
                application_id="example-app",
                problem_description="x",
                session_id="sess-fail-1",
                requested_by="tester",
            ),
            context=InvestigationContext(
                environment="production", time_window=(now - timedelta(hours=1), now)
            ),
            status=InvestigationStatus.FAILED,
            created_at=now,
            updated_at=now,
            version=2,
            metadata=metadata,
        )
        _asyncio.run(ctx.investigation_repo.create("tenant-a", inv))
        return inv

    def test_failure_code_mapping(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            _failure_code_for,
        )

        assert _failure_code_for("budget_exceeded", False) == "BUDGET_EXCEEDED"
        assert _failure_code_for("input_expired", False) == "INPUT_EXPIRED"
        assert (
            _failure_code_for("verification_failed_or_limit_reached", False) == "UNVERIFIED"
        )
        assert _failure_code_for("", False) == "UNKNOWN"
        assert _failure_code_for("whatever", False) == "UNKNOWN"
        assert _failure_code_for("", True) == "CONCLUSION_DENIED"

    def test_conclusion_reports_failure_record(self, _restore_context: None) -> None:
        ctx, client = _client()
        inv = self._failed_inv(
            ctx, {"failure": {"code": "UNVERIFIED", "reason": "", "failed_at": "t"}}
        )
        body = client.get(
            f"/api/v1/investigations/{inv.id}/conclusion", headers=HEADERS
        ).json()
        assert body["status"] == "FAILED"
        assert body["conclusion"] is None
        assert body["failure"] == {"code": "UNVERIFIED", "reason": "", "failed_at": "t"}

    def test_conclusion_failure_null_when_absent(self, _restore_context: None) -> None:
        ctx, client = _client()
        inv = self._failed_inv(ctx, {})
        body = client.get(
            f"/api/v1/investigations/{inv.id}/conclusion", headers=HEADERS
        ).json()
        assert body["failure"] is None


class TestEventsWire:
    def test_pause_never_started_409_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        inv_id = _create(client).json()["id"]
        resp = client.post(f"/api/v1/investigations/{inv_id}/pause", headers=HEADERS)
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "INVESTIGATION_NOT_STARTED"

    def test_pause_unknown_404_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.post(
            "/api/v1/investigations/00000000-0000-0000-0000-000000000000/pause",
            headers=HEADERS,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "INVESTIGATION_NOT_FOUND"


class TestEvidenceHypothesesTimelineWire:
    def test_evidence_empty_no_pagination_keys(self, _restore_context: None) -> None:
        _, client = _client()
        inv_id = _create(client).json()["id"]
        body = client.get(
            f"/api/v1/investigations/{inv_id}/evidence", headers=HEADERS
        ).json()
        # No offset/limit keys on this route (unlike hypotheses/timeline).
        assert body == {"items": [], "total": 0}

    def test_evidence_unknown_404_envelope(self, _restore_context: None) -> None:
        _, client = _client()
        inv_id = _create(client).json()["id"]
        resp = client.get(
            f"/api/v1/investigations/{inv_id}/evidence/00000000-0000-0000-0000-000000000000",
            headers=HEADERS,
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "EVIDENCE_NOT_FOUND"

    def test_hypotheses_empty_shape(self, _restore_context: None) -> None:
        _, client = _client()
        inv_id = _create(client).json()["id"]
        body = client.get(
            f"/api/v1/investigations/{inv_id}/hypotheses", headers=HEADERS
        ).json()
        assert body == {"items": [], "total": 0, "offset": 0, "limit": 50}

    def test_timeline_empty_shape(self, _restore_context: None) -> None:
        _, client = _client()
        inv_id = _create(client).json()["id"]
        body = client.get(
            f"/api/v1/investigations/{inv_id}/timeline", headers=HEADERS
        ).json()
        assert body == {"items": [], "total": 0, "offset": 0, "limit": 100}


class TestDispatchBudget:
    def _started_inv(self):  # type: ignore[no-untyped-def]
        from datetime import UTC, datetime, timedelta

        from investigation_agent_platform.domain.investigation.models import (
            Investigation,
            InvestigationContext,
            InvestigationRequest,
            InvestigationStatus,
        )

        now = datetime.now(UTC)
        return Investigation(
            session_id="sess-budget",
            application_id="example-app",
            tenant_id="tenant-a",
            request=InvestigationRequest(
                application_id="example-app",
                problem_description="x",
                session_id="sess-budget",
                requested_by="tester",
            ),
            context=InvestigationContext(
                environment="production", time_window=(now - timedelta(hours=1), now)
            ),
            status=InvestigationStatus.CREATED,
            created_at=now,
            updated_at=now,
            version=1,
        )

    @pytest.mark.asyncio
    async def test_dispatch_passes_profile_iteration_budget(
        self, _restore_context: None, tmp_path
    ) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        from investigation_agent_platform.api.dependencies import (
            AppContext,
            InMemoryInvestigationRepository,
            set_app_context,
        )
        from investigation_agent_platform.api.v1.routers.investigations import (
            _dispatch_workflow,
        )
        from investigation_agent_platform.infrastructure.persistence.buffered import (
            BufferedProxy,
            SqliteWal,
        )

        ctx = AppContext()
        wal = SqliteWal(str(tmp_path / "dispatch-wal.sqlite3"))
        ctx.investigation_repo = BufferedProxy(
            InMemoryInvestigationRepository(),
            ctx.investigation_repo,
            wal,
            "investigation_repo",
        )
        profile = SimpleNamespace(
            status=SimpleNamespace(value="ACTIVE"),
            investigation=SimpleNamespace(evidence_budget={"max_iterations": 20}),
        )
        ctx.profile_repo = SimpleNamespace(
            list_revisions=AsyncMock(return_value=[profile])
        )
        ctx.temporal_client = MagicMock()
        ctx.temporal_client.start_workflow = AsyncMock(return_value=None)
        set_app_context(ctx)
        investigation = self._started_inv()
        await ctx.investigation_repo.create("tenant-a", investigation)
        ack = await _dispatch_workflow(ctx, investigation, "tenant-a", str(investigation.id), None)
        assert ack.status == "RUNNING"
        (wf_name, wf_input), _ = ctx.temporal_client.start_workflow.call_args
        assert wf_name == "RunInvestigationWorkflow"
        assert wf_input.max_iterations == 20
        # The aggregate itself is RUNNING (D14: real status, not a DTO literal).
        stored = await ctx.investigation_repo.get_by_id("tenant-a", investigation.id)
        assert stored.status.value == "RUNNING"
        assert stored.started_at is not None

    @pytest.mark.asyncio
    async def test_dispatch_defaults_without_profile(
        self, _restore_context: None, tmp_path
    ) -> None:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        from investigation_agent_platform.api.dependencies import (
            AppContext,
            InMemoryInvestigationRepository,
            set_app_context,
        )
        from investigation_agent_platform.api.v1.routers.investigations import (
            _dispatch_workflow,
        )
        from investigation_agent_platform.infrastructure.persistence.buffered import (
            BufferedProxy,
            SqliteWal,
        )

        ctx = AppContext()
        wal = SqliteWal(str(tmp_path / "dispatch-wal2.sqlite3"))
        ctx.investigation_repo = BufferedProxy(
            InMemoryInvestigationRepository(),
            ctx.investigation_repo,
            wal,
            "investigation_repo",
        )
        ctx.profile_repo = SimpleNamespace(list_revisions=AsyncMock(return_value=[]))
        ctx.temporal_client = MagicMock()
        ctx.temporal_client.start_workflow = AsyncMock(return_value=None)
        set_app_context(ctx)
        investigation = self._started_inv()
        await ctx.investigation_repo.create("tenant-a", investigation)
        await _dispatch_workflow(ctx, investigation, "tenant-a", str(investigation.id), None)
        (_, wf_input), _ = ctx.temporal_client.start_workflow.call_args
        assert wf_input.max_iterations is None


class TestListFilters:
    def _seed(self, ctx, status, application_id="example-app", tenant="tenant-a"):  # type: ignore[no-untyped-def]
        import asyncio as _asyncio
        from datetime import UTC, datetime, timedelta

        from investigation_agent_platform.domain.investigation.models import (
            Investigation,
            InvestigationContext,
            InvestigationRequest,
        )

        now = datetime.now(UTC)
        inv = Investigation(
            session_id=f"sess-{uuid4().hex[:8]}",
            application_id=application_id,
            tenant_id=tenant,
            request=InvestigationRequest(
                application_id=application_id,
                problem_description="x",
                session_id="s",
                requested_by="tester",
            ),
            context=InvestigationContext(
                environment="production", time_window=(now - timedelta(hours=1), now)
            ),
            status=status,
            created_at=now,
            updated_at=now,
            version=1,
        )
        _asyncio.run(ctx.investigation_repo.create(tenant, inv))
        return inv

    def test_default_lists_open_only(self, _restore_context: None) -> None:
        from investigation_agent_platform.domain.investigation.models import (
            InvestigationStatus,
        )

        ctx, client = _client()
        open_inv = self._seed(ctx, InvestigationStatus.INVESTIGATING)
        closed_inv = self._seed(ctx, InvestigationStatus.FAILED)
        body = client.get("/api/v1/investigations", headers=HEADERS).json()
        ids = [i["investigation_id"] for i in body["items"]]
        assert str(open_inv.id) in ids
        assert str(closed_inv.id) not in ids

    def test_status_filter_includes_terminal(self, _restore_context: None) -> None:
        from investigation_agent_platform.domain.investigation.models import (
            InvestigationStatus,
        )

        ctx, client = _client()
        failed = self._seed(ctx, InvestigationStatus.FAILED)
        done = self._seed(ctx, InvestigationStatus.COMPLETED)
        body = client.get(
            "/api/v1/investigations?status=FAILED", headers=HEADERS
        ).json()
        ids = [i["investigation_id"] for i in body["items"]]
        assert str(failed.id) in ids
        assert str(done.id) not in ids
        body = client.get(
            "/api/v1/investigations?status=failed,completed", headers=HEADERS
        ).json()
        ids = [i["investigation_id"] for i in body["items"]]
        assert str(failed.id) in ids
        assert str(done.id) in ids

    def test_application_filter_scopes(self, _restore_context: None) -> None:
        from investigation_agent_platform.domain.investigation.models import (
            InvestigationStatus,
        )

        ctx, client = _client()
        mine = self._seed(ctx, InvestigationStatus.FAILED, application_id="other-app")
        other = self._seed(ctx, InvestigationStatus.FAILED, application_id="example-app")
        body = client.get(
            "/api/v1/investigations?status=FAILED&application_id=other-app",
            headers=HEADERS,
        ).json()
        ids = [i["investigation_id"] for i in body["items"]]
        assert str(mine.id) in ids
        assert str(other.id) not in ids

    def test_invalid_status_400(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.get("/api/v1/investigations?status=BOGUS", headers=HEADERS)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_STATUS_FILTER"


class TestPrioritySeverityEnums:
    def test_invalid_priority_422(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.post(
            "/api/v1/investigations",
            json={
                "application_id": "example-app",
                "problem_description": "x",
                "priority": "URGENT",
            },
            headers=HEADERS,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "ValidationError"

    def test_valid_priorities_accepted(self, _restore_context: None) -> None:
        _, client = _client()
        for priority in ["LOW", "NORMAL", "HIGH", "CRITICAL"]:
            resp = client.post(
                "/api/v1/investigations",
                json={
                    "application_id": "example-app",
                    "problem_description": "x",
                    "priority": priority,
                },
                headers=HEADERS,
            )
            assert resp.status_code == 201, priority
            assert resp.json()["investigation"]["request"]["priority"] == priority

    def test_invalid_severity_422(self, _restore_context: None) -> None:
        _, client = _client()
        resp = client.post(
            "/api/v1/intake/errors",
            json={"application_id": "x", "error_class": "E", "severity": "BORKED"},
            headers=HEADERS,
        )
        assert resp.status_code == 422


class TestStartGates:
    def test_start_on_completed_409(self, _restore_context: None) -> None:
        import asyncio as _asyncio
        from datetime import UTC, datetime, timedelta

        from investigation_agent_platform.domain.investigation.models import (
            Investigation,
            InvestigationContext,
            InvestigationRequest,
            InvestigationStatus,
        )

        ctx, client = _client()
        now = datetime.now(UTC)
        inv = Investigation(
            session_id="sess-done",
            application_id="example-app",
            tenant_id="tenant-a",
            request=InvestigationRequest(
                application_id="example-app",
                problem_description="x",
                session_id="sess-done",
                requested_by="tester",
            ),
            context=InvestigationContext(
                environment="production", time_window=(now - timedelta(hours=1), now)
            ),
            status=InvestigationStatus.COMPLETED,
            created_at=now,
            updated_at=now,
            version=1,
        )
        _asyncio.run(ctx.investigation_repo.create("tenant-a", inv))
        resp = client.post(
            f"/api/v1/investigations/{inv.id}/start", headers=HEADERS
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "INVESTIGATION_NOT_STARTABLE"

    def test_start_persists_running(self, _restore_context: None, tmp_path) -> None:
        import asyncio as _asyncio
        from datetime import UTC, datetime, timedelta
        from unittest.mock import AsyncMock, MagicMock

        from investigation_agent_platform.api.dependencies import (
            InMemoryInvestigationRepository,
        )
        from investigation_agent_platform.domain.investigation.models import (
            Investigation,
            InvestigationContext,
            InvestigationRequest,
            InvestigationStatus,
        )
        from investigation_agent_platform.infrastructure.persistence.buffered import (
            BufferedProxy,
            SqliteWal,
        )

        ctx, client = _client()
        wal = SqliteWal(str(tmp_path / "start-wal.sqlite3"))
        ctx.investigation_repo = BufferedProxy(
            InMemoryInvestigationRepository(),
            ctx.investigation_repo,
            wal,
            "investigation_repo",
        )
        now = datetime.now(UTC)
        inv = Investigation(
            session_id="sess-run",
            application_id="example-app",
            tenant_id="tenant-a",
            request=InvestigationRequest(
                application_id="example-app",
                problem_description="x",
                session_id="sess-run",
                requested_by="tester",
            ),
            context=InvestigationContext(
                environment="production", time_window=(now - timedelta(hours=1), now)
            ),
            status=InvestigationStatus.CREATED,
            created_at=now,
            updated_at=now,
            version=1,
        )
        _asyncio.run(ctx.investigation_repo.create("tenant-a", inv))
        fake_client = MagicMock()
        fake_client.start_workflow = AsyncMock(return_value=None)
        ctx.temporal_client = fake_client
        try:
            resp = client.post(
                f"/api/v1/investigations/{inv.id}/start", headers=HEADERS
            )
            assert resp.status_code == 202
            stored = _asyncio.run(ctx.investigation_repo.get_by_id("tenant-a", inv.id))
            assert stored.status == InvestigationStatus.RUNNING
        finally:
            del ctx.temporal_client
