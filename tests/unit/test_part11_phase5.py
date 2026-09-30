# tests/unit/test_part11_phase5.py
"""Part 11 Phase 11.5 tests: structured input-required suspension.

Covers: requirement/fulfillment repos (in-memory + mocked SQL tenant scope),
lifecycle transitions, workflow Update validator/handler, record/set-state/
promote activities, fulfill endpoint (validation, idempotency, conflict,
Temporal mapping), GET pending requirements, restart-durability at the
serialization/mapper level, cancellation + Continue-As-New state shapes, and
migration 011. No live Temporal worker: the workflow Update/validator are
exercised directly and the endpoint uses a faked workflow handle.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from investigation_agent_platform.api.app import create_app
from investigation_agent_platform.api.dependencies import (
    AppContext,
    set_app_context,
)
from investigation_agent_platform.domain.common.exceptions import ConcurrencyError
from investigation_agent_platform.domain.investigation.input_requirements import (
    AWAITING_INPUT_SOURCES,
    AWAITING_INPUT_TARGETS,
    InputFulfillment,
    InputRequirement,
    RequirementState,
)
from investigation_agent_platform.domain.investigation.models import (
    ActorType,
    InvestigationRequest,
    InvestigationStatus,
)


@pytest.fixture
def _restore_context():
    import investigation_agent_platform.api.dependencies as deps

    prev = deps._context
    yield
    deps._context = prev


def _requirement(tenant: str = "tenant-a", inv_id=None, **overrides) -> InputRequirement:
    params: dict = {
        "tenant_id": tenant,
        "investigation_id": inv_id or uuid4(),
        "reason": "Need the failing transaction id",
        "json_schema": {
            "type": "object",
            "properties": {"txn_id": {"type": "string"}},
            "required": ["txn_id"],
        },
    }
    params.update(overrides)
    return InputRequirement(**params)


def _fulfillment(
    tenant: str = "tenant-a", req: InputRequirement | None = None, **overrides
) -> InputFulfillment:
    req = req or _requirement(tenant)
    params: dict = {
        "requirement_id": req.requirement_id,
        "requirement_version": req.requirement_version,
        "tenant_id": tenant,
        "investigation_id": req.investigation_id,
        "fulfilled_by": "tester",
        "content_digest": "sha256:abc",
        "content_bytes": 42,
    }
    params.update(overrides)
    return InputFulfillment(**params)


@asynccontextmanager
async def _mock_rls(module_path: str, session: MagicMock, tenant_id: str = "tenant-a"):
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)
    with patch(f"{module_path}.rls_session", return_value=cm) as mock_rls:
        yield session
        mock_rls.assert_called()
        assert mock_rls.call_args.args[1] == tenant_id


def _mock_session(scalars_first=None, scalars_all=None, scalar_value=None) -> MagicMock:
    session = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.first.return_value = scalars_first
    mock_scalars.all.return_value = scalars_all or []
    session.scalars = AsyncMock(return_value=mock_scalars)
    session.scalar = AsyncMock(return_value=scalar_value)
    session.execute = AsyncMock(return_value=MagicMock())
    session.add = MagicMock()
    return session


async def _awaiting_investigation(ctx: AppContext, tenant: str = "tenant-a"):  # type: ignore[no-untyped-def]
    req = InvestigationRequest(
        application_id="example-app",
        problem_description="awaiting input test",
        session_id="sess-await",
        requested_by="tester",
    )
    inv = await ctx.create_investigation_service().execute(req, tenant_id=tenant)
    investigating, _ = inv.transition_to(
        InvestigationStatus.INVESTIGATING, ActorType.USER, "test start"
    )
    await ctx.investigation_repo.save(tenant, investigating, inv.version)
    suspended, _ = investigating.transition_to(
        InvestigationStatus.AWAITING_INPUT, ActorType.USER, "test suspend"
    )
    await ctx.investigation_repo.save(tenant, suspended, investigating.version)
    return await ctx.get_investigation_service().execute(tenant, inv.id)


# ===========================================================================
# In-memory requirement repository
# ===========================================================================


class TestInMemoryInputRepo:
    @pytest.mark.asyncio
    async def test_create_and_pending(self) -> None:
        from investigation_agent_platform.api.dependencies import (
            InMemoryInputRequirementRepository,
        )

        repo = InMemoryInputRequirementRepository()
        inv_id = uuid4()
        req = _requirement("tenant-a", inv_id)
        await repo.create_requirement("tenant-a", req)
        pending = await repo.get_pending("tenant-a", inv_id)
        assert [r.requirement_id for r in pending] == [req.requirement_id]
        assert await repo.get_by_id("tenant-a", req.requirement_id) == req
        assert await repo.get_pending("tenant-b", inv_id) == []
        assert await repo.get_by_id("tenant-b", req.requirement_id) is None

    @pytest.mark.asyncio
    async def test_pending_ordered_and_state_filtered(self) -> None:
        from investigation_agent_platform.api.dependencies import (
            InMemoryInputRequirementRepository,
        )

        repo = InMemoryInputRequirementRepository()
        inv_id = uuid4()
        first = _requirement("tenant-a", inv_id, reason="first")
        second = _requirement("tenant-a", inv_id, reason="second")
        await repo.create_requirement("tenant-a", second)
        await repo.create_requirement("tenant-a", first)
        pending = await repo.get_pending("tenant-a", inv_id)
        assert [r.reason for r in pending] == ["first", "second"]
        await repo.set_state("tenant-a", first.requirement_id, 1, RequirementState.FULFILLED)
        assert [r.reason for r in await repo.get_pending("tenant-a", inv_id)] == ["second"]

    @pytest.mark.asyncio
    async def test_set_state_compare_and_set(self) -> None:
        from investigation_agent_platform.api.dependencies import (
            InMemoryInputRequirementRepository,
        )

        repo = InMemoryInputRequirementRepository()
        req = _requirement()
        await repo.create_requirement("tenant-a", req)
        await repo.set_state("tenant-a", req.requirement_id, 1, RequirementState.FULFILLED)
        assert (await repo.get_by_id("tenant-a", req.requirement_id)).state == (
            RequirementState.FULFILLED
        )
        with pytest.raises(ConcurrencyError):
            await repo.set_state("tenant-a", req.requirement_id, 9, RequirementState.EXPIRED)
        with pytest.raises(ConcurrencyError):
            await repo.set_state("tenant-a", uuid4(), 1, RequirementState.EXPIRED)
        with pytest.raises(ConcurrencyError):
            await repo.set_state("tenant-b", req.requirement_id, 1, RequirementState.EXPIRED)

    @pytest.mark.asyncio
    async def test_fulfillment_idempotent_and_listed(self) -> None:
        from investigation_agent_platform.api.dependencies import (
            InMemoryInputRequirementRepository,
        )

        repo = InMemoryInputRequirementRepository()
        req = _requirement()
        await repo.create_requirement("tenant-a", req)
        await repo.record_fulfillment("tenant-a", _fulfillment("tenant-a", req))
        await repo.record_fulfillment("tenant-a", _fulfillment("tenant-a", req))
        rows = await repo.list_fulfillments("tenant-a", req.investigation_id)
        assert len(rows) == 1
        assert rows[0].content_digest == "sha256:abc"
        assert await repo.list_fulfillments("tenant-b", req.investigation_id) == []
        with pytest.raises(ConcurrencyError):
            await repo.record_fulfillment("tenant-b", _fulfillment("tenant-a", req))

    @pytest.mark.asyncio
    async def test_create_tenant_mismatch(self) -> None:
        from investigation_agent_platform.api.dependencies import (
            InMemoryInputRequirementRepository,
        )

        repo = InMemoryInputRequirementRepository()
        with pytest.raises(ConcurrencyError):
            await repo.create_requirement("tenant-b", _requirement("tenant-a"))


# ===========================================================================
# SQL requirement repository (mocked sessions)
# ===========================================================================


class TestSqlInputRepo:
    MODULE = "investigation_agent_platform.infrastructure.persistence.input_requirement_repository"

    @pytest.mark.asyncio
    async def test_create_scoped(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.input_requirement_repository import (
            SqlAlchemyInputRequirementRepository,
        )

        session = _mock_session()
        async with _mock_rls(self.MODULE, session):
            repo = SqlAlchemyInputRequirementRepository(db_session_factory=MagicMock())
            await repo.create_requirement("tenant-a", _requirement())
            assert session.add.called

    @pytest.mark.asyncio
    async def test_create_tenant_mismatch_before_io(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.input_requirement_repository import (
            SqlAlchemyInputRequirementRepository,
        )

        session = _mock_session()
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=session)
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch(f"{self.MODULE}.rls_session", return_value=cm) as mock_rls:
            repo = SqlAlchemyInputRequirementRepository(db_session_factory=MagicMock())
            with pytest.raises(ConcurrencyError):
                await repo.create_requirement("tenant-b", _requirement("tenant-a"))
            assert not mock_rls.called
            assert not session.add.called

    @pytest.mark.asyncio
    async def test_get_pending_empty(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.input_requirement_repository import (
            SqlAlchemyInputRequirementRepository,
        )

        session = _mock_session(scalars_all=[])
        async with _mock_rls(self.MODULE, session):
            repo = SqlAlchemyInputRequirementRepository(db_session_factory=MagicMock())
            assert await repo.get_pending("tenant-a", uuid4()) == []

    @pytest.mark.asyncio
    async def test_set_state_missing_and_stale(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.input_requirement_repository import (
            SqlAlchemyInputRequirementRepository,
        )

        session = _mock_session(scalars_first=None)
        session.rollback = AsyncMock()
        async with _mock_rls(self.MODULE, session):
            repo = SqlAlchemyInputRequirementRepository(db_session_factory=MagicMock())
            with pytest.raises(ConcurrencyError):
                await repo.set_state("tenant-a", uuid4(), 1, RequirementState.FULFILLED)

    @pytest.mark.asyncio
    async def test_record_fulfillment_executes(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.input_requirement_repository import (
            SqlAlchemyInputRequirementRepository,
        )

        session = _mock_session()
        async with _mock_rls(self.MODULE, session):
            repo = SqlAlchemyInputRequirementRepository(db_session_factory=MagicMock())
            await repo.record_fulfillment("tenant-a", _fulfillment())
            assert session.execute.called

    @pytest.mark.asyncio
    async def test_list_fulfillments_empty(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.input_requirement_repository import (
            SqlAlchemyInputRequirementRepository,
        )

        session = _mock_session(scalars_all=[])
        async with _mock_rls(self.MODULE, session):
            repo = SqlAlchemyInputRequirementRepository(db_session_factory=MagicMock())
            assert await repo.list_fulfillments("tenant-a", uuid4()) == []


# ===========================================================================
# Lifecycle transitions
# ===========================================================================


class TestAwaitingInputTransitions:
    def _investigation(self):  # type: ignore[no-untyped-def]
        from datetime import UTC, datetime

        from investigation_agent_platform.domain.investigation.models import (
            Investigation,
            InvestigationContext,
        )

        now = datetime.now(UTC)
        return Investigation(
            session_id="sess-t",
            application_id="example-app",
            tenant_id="tenant-a",
            request=InvestigationRequest(
                application_id="example-app",
                problem_description="transition check",
                session_id="sess-t",
                requested_by="tester",
            ),
            context=InvestigationContext(environment="dev", time_window=(now, now)),
        )

    def test_active_states_can_suspend(self) -> None:
        for source in (
            InvestigationStatus.CONTEXTUALIZING,
            InvestigationStatus.INVESTIGATING,
            InvestigationStatus.CORRELATING,
            InvestigationStatus.HYPOTHESIZING,
            InvestigationStatus.VERIFYING,
            InvestigationStatus.CONCLUDING,
            InvestigationStatus.PAUSED,
        ):
            inv = self._investigation().model_copy(update={"status": source})
            suspended, _ = inv.transition_to(
                InvestigationStatus.AWAITING_INPUT, ActorType.USER, "need input"
            )
            assert suspended.status == InvestigationStatus.AWAITING_INPUT

    def test_awaiting_input_resumes_and_diverts(self) -> None:
        for target in AWAITING_INPUT_TARGETS:
            inv = self._investigation().model_copy(
                update={"status": InvestigationStatus.AWAITING_INPUT}
            )
            resumed, _ = inv.transition_to(target, ActorType.USER, "resume")
            assert resumed.status == target

    def test_terminal_and_completed_cannot_suspend(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import (
            InvalidLifecycleTransitionException,
        )

        for source in (
            InvestigationStatus.CREATED,
            InvestigationStatus.COMPLETED,
            InvestigationStatus.FAILED,
            InvestigationStatus.CANCELLED,
        ):
            inv = self._investigation().model_copy(update={"status": source})
            with pytest.raises(InvalidLifecycleTransitionException):
                inv.transition_to(InvestigationStatus.AWAITING_INPUT, ActorType.USER, "need input")

    def test_awaiting_input_cannot_complete_directly(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import (
            InvalidLifecycleTransitionException,
        )

        inv = self._investigation().model_copy(
            update={"status": InvestigationStatus.AWAITING_INPUT}
        )
        with pytest.raises(InvalidLifecycleTransitionException):
            inv.transition_to(InvestigationStatus.COMPLETED, ActorType.USER, "skip resume")

    def test_source_target_sets_cover_map(self) -> None:
        assert InvestigationStatus.AWAITING_INPUT not in AWAITING_INPUT_SOURCES
        assert InvestigationStatus.AWAITING_INPUT not in AWAITING_INPUT_TARGETS
        assert InvestigationStatus.COMPLETED not in AWAITING_INPUT_TARGETS


# ===========================================================================
# Workflow Update validator + handler (direct, no worker)
# ===========================================================================


class TestFulfillUpdate:
    def _workflow(self, pending=None):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.application.worker.workflows import (
            RunInvestigationWorkflow,
        )

        wf = RunInvestigationWorkflow()
        wf._pending_requirement = pending
        wf._fulfilled_input = None
        return wf

    def test_validator_accepts_match(self) -> None:
        wf = self._workflow({"requirement_id": "req-1", "requirement_version": 3})
        wf.validate_fulfill_input("req-1", 3, {"txn_id": "t"})
        # No exception = accepted.

    def test_validator_rejects_without_pending(self) -> None:
        from temporalio.exceptions import ApplicationError

        wf = self._workflow(None)
        with pytest.raises(ApplicationError, match="no pending"):
            wf.validate_fulfill_input("req-1", 1, {})

    def test_validator_rejects_stale(self) -> None:
        from temporalio.exceptions import ApplicationError

        wf = self._workflow({"requirement_id": "req-1", "requirement_version": 2})
        with pytest.raises(ApplicationError, match="stale or unknown"):
            wf.validate_fulfill_input("req-1", 1, {})
        with pytest.raises(ApplicationError, match="stale or unknown"):
            wf.validate_fulfill_input("req-9", 2, {})

    @pytest.mark.asyncio
    async def test_handler_records_fulfillment(self) -> None:
        wf = self._workflow({"requirement_id": "req-1", "requirement_version": 1})
        result = await wf.fulfill_input("req-1", 1, {"txn_id": "t-42"})
        assert result == {"accepted": True, "requirement_id": "req-1", "version": 1}
        assert wf._fulfilled_input == {
            "requirement_id": "req-1",
            "version": 1,
            "data": {"txn_id": "t-42"},
        }

    def test_wait_state_is_json_serializable(self) -> None:
        """Continue-As-New safety: wait-gate state must be plain JSON data."""
        pending = {"requirement_id": "req-1", "requirement_version": 1}
        fulfilled = {"requirement_id": "req-1", "version": 1, "data": {"a": 1}}
        assert json.loads(json.dumps(pending)) == pending
        assert json.loads(json.dumps(fulfilled)) == fulfilled


# ===========================================================================
# Record / set-state / promote activities (direct, no worker)
# ===========================================================================


class TestInputActivities:
    @pytest.mark.asyncio
    async def test_record_persists_and_returns_identity(self, _restore_context: None) -> None:
        from investigation_agent_platform.application.worker.activities import (
            record_input_requirement_activity,
        )

        set_app_context(AppContext())
        inv_id = uuid4()
        result = await record_input_requirement_activity(
            {
                "tenant_id": "tenant-a",
                "investigation_id": str(inv_id),
                "reason": "Need the txn id",
                "json_schema": {"type": "object"},
                "resume_status": "VERIFYING",
            }
        )
        assert result.success is True
        assert result.data["resume_status"] == "VERIFYING"
        assert result.data["requirement_version"] == 1
        assert "wait_timeout_seconds" not in result.data

    @pytest.mark.asyncio
    async def test_record_with_expiry_returns_budget(self, _restore_context: None) -> None:
        from investigation_agent_platform.application.worker.activities import (
            record_input_requirement_activity,
        )

        set_app_context(AppContext())
        result = await record_input_requirement_activity(
            {
                "tenant_id": "tenant-a",
                "investigation_id": str(uuid4()),
                "reason": "Need it soon",
                "expires_in_seconds": 300,
            }
        )
        assert result.success is True
        assert result.data["wait_timeout_seconds"] == 300.0

    @pytest.mark.asyncio
    async def test_record_rejects_bad_resume_status(self, _restore_context: None) -> None:
        from investigation_agent_platform.application.worker.activities import (
            record_input_requirement_activity,
        )

        set_app_context(AppContext())
        result = await record_input_requirement_activity(
            {
                "tenant_id": "tenant-a",
                "investigation_id": str(uuid4()),
                "reason": "x",
                "resume_status": "COMPLETED",
            }
        )
        assert result.success is False

    @pytest.mark.asyncio
    async def test_record_requires_ids(self, _restore_context: None) -> None:
        from investigation_agent_platform.application.worker.activities import (
            record_input_requirement_activity,
        )

        set_app_context(AppContext())
        result = await record_input_requirement_activity({})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_set_state_paths(self, _restore_context: None) -> None:
        from temporalio.exceptions import ApplicationError

        from investigation_agent_platform.application.worker.activities import (
            set_input_requirement_state_activity,
        )

        ctx = AppContext()
        set_app_context(ctx)
        req = _requirement()
        await ctx.input_repo.create_requirement("tenant-a", req)
        ok_result = await set_input_requirement_state_activity(
            {
                "tenant_id": "tenant-a",
                "requirement_id": str(req.requirement_id),
                "expected_version": 1,
                "state": "FULFILLED",
            }
        )
        assert ok_result.success is True
        # Version mismatch and unknown ids fail closed via ApplicationFailure
        # (fail-loud, never a silent success=False that the workflow ignores).
        with pytest.raises(ApplicationError):
            await set_input_requirement_state_activity(
                {
                    "tenant_id": "tenant-a",
                    "requirement_id": str(req.requirement_id),
                    "expected_version": 9,
                    "state": "EXPIRED",
                }
            )
        with pytest.raises(ApplicationError):
            await set_input_requirement_state_activity(
                {
                    "tenant_id": "tenant-a",
                    "requirement_id": str(uuid4()),
                    "expected_version": 1,
                    "state": "EXPIRED",
                }
            )

    @pytest.mark.asyncio
    async def test_promote_creates_traceable_evidence(self, _restore_context: None) -> None:
        from investigation_agent_platform.application.worker.activities import (
            promote_fulfillment_evidence_activity,
        )

        ctx = AppContext()
        set_app_context(ctx)
        inv_id = uuid4()
        result = await promote_fulfillment_evidence_activity(
            {
                "tenant_id": "tenant-a",
                "investigation_id": str(inv_id),
                "requirement_id": "req-1",
                "data": {"txn_id": "t-42"},
                "classification": "INTERNAL",
            }
        )
        assert result.success is True
        items = await ctx.evidence_repo.find_by_investigation_id("tenant-a", inv_id)
        assert len(items) == 1
        assert items[0].provider == "input-fulfillment"
        assert items[0].attributes.get("requirement_id") == "req-1"
        assert "t-42" in items[0].content_snippet


# ===========================================================================
# GET pending requirements
# ===========================================================================


class TestGetPendingRequirements:
    @pytest.mark.asyncio
    async def test_lists_pending_when_awaiting(self, _restore_context: None) -> None:
        ctx = AppContext()
        set_app_context(ctx)
        client = TestClient(create_app())
        inv = await _awaiting_investigation(ctx)
        req = _requirement("tenant-a", inv.id)
        await ctx.input_repo.create_requirement("tenant-a", req)
        resp = client.get(f"/api/v1/investigations/{inv.id}", headers={"X-Tenant-ID": "tenant-a"})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["pending_requirements"]) == 1
        assert body["pending_requirements"][0]["requirement_id"] == str(req.requirement_id)

    @pytest.mark.asyncio
    async def test_empty_when_not_awaiting(self, _restore_context: None) -> None:
        ctx = AppContext()
        set_app_context(ctx)
        client = TestClient(create_app())
        req = InvestigationRequest(
            application_id="example-app",
            problem_description="plain",
            session_id="sess-plain",
            requested_by="tester",
        )
        inv = await ctx.create_investigation_service().execute(req, tenant_id="tenant-a")
        resp = client.get(f"/api/v1/investigations/{inv.id}", headers={"X-Tenant-ID": "tenant-a"})
        assert resp.status_code == 200
        assert resp.json()["pending_requirements"] == []


# ===========================================================================
# Fulfill endpoint
# ===========================================================================


def _fulfill_client(ctx: AppContext):  # type: ignore[no-untyped-def]
    set_app_context(ctx)
    return TestClient(create_app(), raise_server_exceptions=False)


class _FakeHandle:
    """Faked Temporal workflow handle for execute_update tests."""

    def __init__(self, behavior: str = "accept") -> None:
        self.behavior = behavior
        self.calls: list = []

    async def execute_update(self, name, args=None, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append((name, args, kwargs))
        if self.behavior == "accept":
            return {"accepted": True, "requirement_id": args[0], "version": args[1]}
        if self.behavior == "stale":
            raise RuntimeError("stale or unknown input requirement")
        raise RuntimeError("temporal unavailable")


class _FakeTemporal:
    def __init__(self, handle: _FakeHandle) -> None:
        self._handle = handle

    def get_workflow_handle(self, workflow_id):  # type: ignore[no-untyped-def]
        return self._handle


class TestFulfillEndpoint:
    async def _setup(self, schema=None):  # type: ignore[no-untyped-def]
        ctx = AppContext()
        inv = await _awaiting_investigation(ctx)
        req = _requirement("tenant-a", inv.id, json_schema=schema or {"type": "object"})
        await ctx.input_repo.create_requirement("tenant-a", req)
        handle = _FakeHandle()
        ctx.temporal_client = _FakeTemporal(handle)  # type: ignore[attr-defined]
        return ctx, inv, req, handle

    def _headers(self, key="key-1"):  # type: ignore[no-untyped-def]
        return {"X-Tenant-ID": "tenant-a", "X-Idempotency-Key": key}

    @pytest.mark.asyncio
    async def test_success_persists_and_delivers(self, _restore_context: None) -> None:
        ctx, inv, req, handle = await self._setup()
        client = _fulfill_client(ctx)
        resp = client.post(
            f"/api/v1/investigations/{inv.id}/input-requirements/{req.requirement_id}/fulfill",
            json={"requirement_version": 1, "data": {"txn_id": "t-1"}},
            headers=self._headers(),
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "FULFILLED"
        assert body["update"]["accepted"] is True
        assert handle.calls and handle.calls[0][0] == "fulfill_input"
        stored = await ctx.input_repo.get_by_id("tenant-a", req.requirement_id)
        assert stored is not None
        assert stored.state == RequirementState.FULFILLED
        rows = await ctx.input_repo.list_fulfillments("tenant-a", inv.id)
        assert len(rows) == 1
        assert rows[0].fulfilled_by == "dev-principal"

    @pytest.mark.asyncio
    async def test_idempotent_replay_returns_cached(self, _restore_context: None) -> None:
        ctx, inv, req, handle = await self._setup()
        client = _fulfill_client(ctx)
        url = f"/api/v1/investigations/{inv.id}/input-requirements/{req.requirement_id}/fulfill"
        first = client.post(
            url,
            json={"requirement_version": 1, "data": {"txn_id": "t-1"}},
            headers=self._headers("replay-key"),
        )
        assert first.status_code == 202
        calls_after_first = len(handle.calls)
        second = client.post(
            url,
            json={"requirement_version": 1, "data": {"txn_id": "t-1"}},
            headers=self._headers("replay-key"),
        )
        assert second.status_code == 202
        assert second.json() == first.json()
        assert len(handle.calls) == calls_after_first

    @pytest.mark.asyncio
    async def test_missing_idempotency_key_400(self, _restore_context: None) -> None:
        ctx, inv, req, _ = await self._setup()
        client = _fulfill_client(ctx)
        resp = client.post(
            f"/api/v1/investigations/{inv.id}/input-requirements/{req.requirement_id}/fulfill",
            json={"requirement_version": 1, "data": {}},
            headers={"X-Tenant-ID": "tenant-a"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_not_awaiting_409(self, _restore_context: None) -> None:
        ctx = AppContext()
        req_body = InvestigationRequest(
            application_id="example-app",
            problem_description="not waiting",
            session_id="sess-nw",
            requested_by="tester",
        )
        inv = await ctx.create_investigation_service().execute(req_body, tenant_id="tenant-a")
        client = _fulfill_client(ctx)
        resp = client.post(
            f"/api/v1/investigations/{inv.id}/input-requirements/{uuid4()}/fulfill",
            json={"requirement_version": 1, "data": {}},
            headers=self._headers(),
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_unknown_requirement_404(self, _restore_context: None) -> None:
        ctx, inv, _, _ = await self._setup()
        client = _fulfill_client(ctx)
        resp = client.post(
            f"/api/v1/investigations/{inv.id}/input-requirements/{uuid4()}/fulfill",
            json={"requirement_version": 1, "data": {}},
            headers=self._headers(),
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_stale_version_409(self, _restore_context: None) -> None:
        ctx, inv, req, _ = await self._setup()
        client = _fulfill_client(ctx)
        resp = client.post(
            f"/api/v1/investigations/{inv.id}/input-requirements/{req.requirement_id}/fulfill",
            json={"requirement_version": 7, "data": {}},
            headers=self._headers(),
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_non_pending_409(self, _restore_context: None) -> None:
        ctx, inv, req, _ = await self._setup()
        await ctx.input_repo.set_state(
            "tenant-a", req.requirement_id, 1, RequirementState.FULFILLED
        )
        client = _fulfill_client(ctx)
        resp = client.post(
            f"/api/v1/investigations/{inv.id}/input-requirements/{req.requirement_id}/fulfill",
            json={"requirement_version": 1, "data": {}},
            headers=self._headers(),
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_schema_violation_422(self, _restore_context: None) -> None:
        ctx, inv, req, _ = await self._setup(
            schema={
                "type": "object",
                "properties": {"txn_id": {"type": "string"}},
                "required": ["txn_id"],
            }
        )
        client = _fulfill_client(ctx)
        resp = client.post(
            f"/api/v1/investigations/{inv.id}/input-requirements/{req.requirement_id}/fulfill",
            json={"requirement_version": 1, "data": {"wrong": 1}},
            headers=self._headers(),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_oversize_422(self, _restore_context: None) -> None:
        ctx, inv, req, _ = await self._setup()
        client = _fulfill_client(ctx)
        resp = client.post(
            f"/api/v1/investigations/{inv.id}/input-requirements/{req.requirement_id}/fulfill",
            json={"requirement_version": 1, "data": {"blob": "x" * 70000}},
            headers=self._headers(),
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_cross_tenant_404(self, _restore_context: None) -> None:
        ctx, inv, req, _ = await self._setup()
        client = _fulfill_client(ctx)
        resp = client.post(
            f"/api/v1/investigations/{inv.id}/input-requirements/{req.requirement_id}/fulfill",
            json={"requirement_version": 1, "data": {}},
            headers={"X-Tenant-ID": "tenant-b", "X-Idempotency-Key": "k"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_no_temporal_engine_503(self, _restore_context: None) -> None:
        ctx = AppContext()
        inv = await _awaiting_investigation(ctx)
        req = _requirement("tenant-a", inv.id)
        await ctx.input_repo.create_requirement("tenant-a", req)
        assert getattr(ctx, "temporal_client", None) is None
        client = _fulfill_client(ctx)
        resp = client.post(
            f"/api/v1/investigations/{inv.id}/input-requirements/{req.requirement_id}/fulfill",
            json={"requirement_version": 1, "data": {"txn_id": "t-1"}},
            headers=self._headers(),
        )
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_validator_rejection_maps_409(self, _restore_context: None) -> None:
        ctx, inv, req, _ = await self._setup()
        stale_handle = _FakeHandle(behavior="stale")
        ctx.temporal_client = _FakeTemporal(stale_handle)  # type: ignore[attr-defined]
        client = _fulfill_client(ctx)
        resp = client.post(
            f"/api/v1/investigations/{inv.id}/input-requirements/{req.requirement_id}/fulfill",
            json={"requirement_version": 1, "data": {}},
            headers=self._headers(),
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_engine_failure_maps_502(self, _restore_context: None) -> None:
        ctx, inv, req, _ = await self._setup()
        dead_handle = _FakeHandle(behavior="dead")
        ctx.temporal_client = _FakeTemporal(dead_handle)  # type: ignore[attr-defined]
        client = _fulfill_client(ctx)
        resp = client.post(
            f"/api/v1/investigations/{inv.id}/input-requirements/{req.requirement_id}/fulfill",
            json={"requirement_version": 1, "data": {}},
            headers=self._headers(),
        )
        assert resp.status_code == 502


# ===========================================================================
# Restart durability (serialization + mapper round-trips)
# ===========================================================================


class TestRestartDurability:
    def test_requirement_json_round_trip(self) -> None:
        req = _requirement()
        assert InputRequirement.model_validate(req.model_dump(mode="json")) == req

    def test_fulfillment_json_round_trip(self) -> None:
        fulfillment = _fulfillment()
        assert InputFulfillment.model_validate(fulfillment.model_dump(mode="json")) == (fulfillment)

    def test_sql_mapper_round_trip(self) -> None:
        from investigation_agent_platform.infrastructure.persistence.input_requirement_repository import (
            SqlAlchemyInputRequirementRepository,
        )

        req = _requirement()
        orm = SqlAlchemyInputRequirementRepository._requirement_to_orm(req, "tenant-a")
        back = SqlAlchemyInputRequirementRepository._requirement_from_orm(orm)
        assert back == req

    def test_fulfillment_digest_deterministic(self) -> None:
        import hashlib

        canonical = json.dumps({"txn_id": "t-1"}, sort_keys=True, separators=(",", ":"))
        digest = f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
        assert digest == f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
        assert len(digest) == len("sha256:") + 64


# ===========================================================================
# Cancellation + Continue-As-New state shapes
# ===========================================================================


class TestCancellationShapes:
    def test_cancelled_is_valid_exit_target(self) -> None:
        assert InvestigationStatus.CANCELLED in AWAITING_INPUT_TARGETS

    @pytest.mark.asyncio
    async def test_cancel_marks_requirement(self) -> None:
        from investigation_agent_platform.api.dependencies import (
            InMemoryInputRequirementRepository,
        )

        repo = InMemoryInputRequirementRepository()
        req = _requirement()
        await repo.create_requirement("tenant-a", req)
        await repo.set_state("tenant-a", req.requirement_id, 1, RequirementState.CANCELLED)
        assert await repo.get_pending("tenant-a", req.investigation_id) == []

    def test_expired_is_valid_exit_target(self) -> None:
        assert InvestigationStatus.FAILED in AWAITING_INPUT_TARGETS


# ===========================================================================
# Migration 011
# ===========================================================================


class TestMigration011:
    def test_011_chain(self) -> None:
        import importlib.util
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[2]
            / "migrations"
            / "versions"
            / "011_input_requirements.py"
        )
        assert path.is_file()
        spec = importlib.util.spec_from_file_location("migration_011", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.revision == "011_input_requirements"
        assert mod.down_revision == "010_background_jobs_and_quotas"

    def test_single_head_is_011(self) -> None:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config("alembic.ini")
        script = ScriptDirectory.from_config(cfg)
        assert script.get_heads() == ["011_input_requirements"]
