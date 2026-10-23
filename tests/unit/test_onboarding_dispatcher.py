"""Phase 2 dispatcher + reconciler: idempotent starts, fencing, crash recovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from investigation_agent_platform.application.onboarding.dispatch_ports import (
    PinnedWorkflowInput,
    WorkflowStartReceipt,
)
from investigation_agent_platform.application.onboarding.dispatcher import (
    DispatchAlreadyStartedError,
    DispatchFatalError,
    DispatchOutcome,
    DispatchStartTimeoutError,
    DispatchUnreachableError,
    OnboardingDispatcher,
    retry_backoff_seconds,
    terminal_for_mirror,
)
from investigation_agent_platform.infrastructure.persistence.onboarding_dispatch_repository import (
    ClaimedOnboardingDispatch,
)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class FakeRow:
    values: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


class FakeDispatchRepo:
    """In-memory dispatches + requests with CAS semantics."""

    def __init__(self) -> None:
        self.dispatches: dict[UUID, dict[str, Any]] = {}
        self.requests: dict[tuple[str, UUID, int], dict[str, Any]] = {}
        self.requeue_calls = 0

    def add_request(
        self, tenant: str = "tenant-a", state: str = "QUEUED", **overrides: Any
    ) -> dict[str, Any]:
        row = {
            "id": uuid4(),
            "tenant_id": tenant,
            "request_id": overrides.get("request_id", uuid4()),
            "revision": 1,
            "state": state,
            "state_version": 1,
            "content_hash": "c" * 64,
        }
        row.update(overrides)
        self.requests[(tenant, row["request_id"], row["revision"])] = row
        return row

    def add_dispatch(self, request: dict[str, Any], **overrides: Any) -> dict[str, Any]:
        row = {
            "id": uuid4(),
            "tenant_id": request["tenant_id"],
            "request_id": request["request_id"],
            "revision": request["revision"],
            "job_id": uuid4(),
            "workflow_id": f"onboarding-{request['tenant_id']}-{request['request_id']}",
            "schema_version": "onboarding-dispatch/v1",
            "state": "PENDING",
            "attempts": 0,
            "next_attempt_at": None,
            "lease_owner": None,
            "lease_fencing_token": 0,
            "lease_expires_at": None,
            "execution_generation": 1,
            "temporal_run_id": None,
            "last_error_code": None,
        }
        row.update(overrides)
        self.dispatches[row["id"]] = row
        return row

    def _claim_row(self, worker_id: str) -> dict[str, Any] | None:
        due = [
            row
            for row in self.dispatches.values()
            if row["state"] in ("PENDING", "RETRYABLE")
        ]
        if not due:
            return None
        due.sort(key=lambda row: str(row["id"]))
        return due[0]

    async def claim_and_reconcile(
        self, *, worker_id: str, lease_duration: Any = None
    ) -> ClaimedOnboardingDispatch | None:
        row = self._claim_row(worker_id)
        if row is None:
            return None
        row["state"] = "CLAIMED"
        row["lease_owner"] = worker_id
        row["lease_fencing_token"] += 1
        row["lease_expires_at"] = _now() + timedelta(seconds=60)
        row["attempts"] += 1
        return ClaimedOnboardingDispatch(
            id=row["id"],
            tenant_id=row["tenant_id"],
            request_id=row["request_id"],
            revision=row["revision"],
            job_id=row["job_id"],
            workflow_id=row["workflow_id"],
            schema_version=row["schema_version"],
            execution_generation=row["execution_generation"],
            lease_owner=worker_id,
            lease_fencing_token=row["lease_fencing_token"],
            lease_expires_at=row["lease_expires_at"],
        )

    async def read_request(
        self, claim: ClaimedOnboardingDispatch, **kwargs: Any
    ) -> dict[str, Any] | None:
        return self.requests.get(
            (claim.tenant_id, claim.request_id, claim.revision)
        )

    async def read_request_state(
        self, tenant_id: str, request_id: UUID, revision: int
    ) -> str | None:
        row = self.requests.get((tenant_id, request_id, revision))
        return row["state"] if row is not None else None

    async def adopt_run_id(
        self, tenant_id: str, dispatch_id: UUID, run_id: str
    ) -> bool:
        row = self.dispatches.get(dispatch_id)
        if row is None or row["tenant_id"] != tenant_id:
            return False
        row["temporal_run_id"] = run_id
        return True

    async def complete_request_cancellation(
        self, tenant_id: str, request_id: UUID, revision: int
    ) -> bool:
        row = self.requests.get((tenant_id, request_id, revision))
        if row is None or row["state"] not in ("QUEUED", "CANCEL_REQUESTED"):
            return False
        row["state"] = "CANCELED"
        row["state_version"] += 1
        return True

    async def read_dispatch(
        self, tenant_id: str, dispatch_id: UUID
    ) -> dict[str, Any] | None:
        row = self.dispatches.get(dispatch_id)
        if row is None or row["tenant_id"] != tenant_id:
            return None
        return dict(row)

    async def record_workflow_accepted(
        self, tenant_id: str, dispatch_id: UUID, fencing_token: int, run_id: str
    ) -> bool:
        row = self.dispatches.get(dispatch_id)
        if (
            row is None
            or row["tenant_id"] != tenant_id
            or row["state"] != "CLAIMED"
            or row["lease_fencing_token"] != fencing_token
        ):
            return False
        row["state"] = "ACCEPTED"
        row["temporal_run_id"] = run_id
        row["lease_owner"] = None
        row["lease_expires_at"] = None
        return True

    async def release_for_retry(
        self,
        tenant_id: str,
        dispatch_id: UUID,
        worker_id: str,
        fencing_token: int,
        error_code: str,
        backoff_seconds: float,
    ) -> bool:
        row = self.dispatches.get(dispatch_id)
        if (
            row is None
            or row["tenant_id"] != tenant_id
            or row["state"] != "CLAIMED"
            or row["lease_fencing_token"] != fencing_token
        ):
            return False
        row["state"] = "RETRYABLE"
        row["lease_owner"] = None
        row["lease_expires_at"] = None
        row["last_error_code"] = error_code
        return True

    async def mark_terminal(
        self,
        tenant_id: str,
        dispatch_id: UUID,
        error_code: str,
        fencing_token: int | None = None,
        only_from: tuple[str, ...] | None = None,
    ) -> bool:
        row = self.dispatches.get(dispatch_id)
        if row is None or row["tenant_id"] != tenant_id:
            return False
        if only_from is not None and row["state"] not in only_from:
            return False
        if fencing_token is not None and row["lease_fencing_token"] != fencing_token:
            return False
        row["state"] = "TERMINAL"
        row["lease_owner"] = None
        row["lease_expires_at"] = None
        row["last_error_code"] = error_code
        return True

    async def requeue_one_expired_lease(self, *, worker_id: str) -> UUID | None:
        for row in self.dispatches.values():
            if row["state"] == "CLAIMED" and row["lease_expires_at"] is not None:
                row["state"] = "RETRYABLE"
                row["lease_owner"] = None
                row["lease_expires_at"] = None
                self.requeue_calls += 1
                return row["id"]
        return None

    async def list_by_state(self, state: str, limit: int = 25) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.dispatches.values()
            if row["state"] == state
        ][:limit]

class FakeStarter:
    def __init__(self) -> None:
        self.calls: list[PinnedWorkflowInput] = []
        self.behavior: str = "ok"
        self.run_id = "run-1"

    async def start(self, workflow_input: PinnedWorkflowInput) -> WorkflowStartReceipt:
        self.calls.append(workflow_input)
        if self.behavior == "ok":
            return WorkflowStartReceipt(
                workflow_id=workflow_input.workflow_id,
                run_id=self.run_id,
                input_reference="ref",
                input_version="v",
            )
        if self.behavior == "already-started":
            raise DispatchAlreadyStartedError("exists")
        if self.behavior == "timeout":
            raise DispatchStartTimeoutError("ambiguous")
        if self.behavior == "unreachable":
            raise DispatchUnreachableError("down")
        if self.behavior == "fatal":
            raise DispatchFatalError("bad input")
        raise AssertionError(f"unknown behavior {self.behavior}")


class FakeLookup:
    def __init__(self) -> None:
        self.by_workflow: dict[str, WorkflowStartReceipt | None] = {}

    async def lookup(self, *, workflow_id: str) -> WorkflowStartReceipt | None:
        return self.by_workflow.get(workflow_id)


def _receipt(
    workflow_id: str, run_id: str = "run-9", status: str = "unknown"
) -> WorkflowStartReceipt:
    return WorkflowStartReceipt(
        workflow_id=workflow_id,
        run_id=run_id,
        input_reference="ref",
        input_version="v",
        status=status,
    )


def _dispatcher(
    repo: FakeDispatchRepo, starter: FakeStarter, lookup: FakeLookup, **kwargs: Any
) -> OnboardingDispatcher:
    return OnboardingDispatcher(
        dispatch_repository=repo, starter=starter, lookup=lookup, **kwargs
    )


def _setup() -> tuple[FakeDispatchRepo, FakeStarter, FakeLookup, dict[str, Any]]:
    repo = FakeDispatchRepo()
    request = repo.add_request()
    dispatch = repo.add_dispatch(request)
    return repo, FakeStarter(), FakeLookup(), {"request": request, "dispatch": dispatch}


async def test_no_work_when_nothing_due() -> None:
    repo, starter, lookup, _ = _setup()
    repo.dispatches.clear()
    result = await _dispatcher(repo, starter, lookup).dispatch_once(worker_id="w1")
    assert result.outcome == DispatchOutcome.NO_WORK
    assert starter.calls == []


async def test_happy_path_starts_and_accepts() -> None:
    repo, starter, lookup, ctx = _setup()
    result = await _dispatcher(repo, starter, lookup).dispatch_once(worker_id="w1")
    assert result.outcome == DispatchOutcome.STARTED
    assert result.run_id == "run-1"
    assert len(starter.calls) == 1
    assert starter.calls[0].workflow_id == ctx["dispatch"]["workflow_id"]
    assert starter.calls[0].tenant_id == "tenant-a"
    row = repo.dispatches[ctx["dispatch"]["id"]]
    assert row["state"] == "ACCEPTED"
    assert row["temporal_run_id"] == "run-1"
    assert row["lease_owner"] is None


async def test_already_started_adopts_running_workflow() -> None:
    repo, starter, lookup, ctx = _setup()
    starter.behavior = "already-started"
    lookup.by_workflow[ctx["dispatch"]["workflow_id"]] = _receipt(
        ctx["dispatch"]["workflow_id"], run_id="run-live", status="running"
    )
    result = await _dispatcher(repo, starter, lookup).dispatch_once(worker_id="w1")
    assert result.outcome == DispatchOutcome.ADOPTED_RUNNING
    assert result.run_id == "run-live"
    assert repo.dispatches[ctx["dispatch"]["id"]]["state"] == "ACCEPTED"


async def test_already_started_mirrors_terminal_failure() -> None:
    repo, starter, lookup, ctx = _setup()
    starter.behavior = "already-started"
    lookup.by_workflow[ctx["dispatch"]["workflow_id"]] = _receipt(
        ctx["dispatch"]["workflow_id"], run_id="run-dead", status="failed"
    )
    result = await _dispatcher(repo, starter, lookup).dispatch_once(worker_id="w1")
    assert result.outcome == DispatchOutcome.TERMINAL
    assert repo.dispatches[ctx["dispatch"]["id"]]["state"] == "TERMINAL"


async def test_ambiguous_timeout_with_missing_workflow_retries() -> None:
    repo, starter, lookup, ctx = _setup()
    starter.behavior = "timeout"
    result = await _dispatcher(repo, starter, lookup).dispatch_once(worker_id="w1")
    assert result.outcome == DispatchOutcome.RETRY_LATER
    assert repo.dispatches[ctx["dispatch"]["id"]]["state"] == "RETRYABLE"


async def test_outage_releases_with_backoff() -> None:
    repo, starter, lookup, ctx = _setup()
    starter.behavior = "unreachable"
    result = await _dispatcher(repo, starter, lookup).dispatch_once(worker_id="w1")
    assert result.outcome == DispatchOutcome.RETRY_LATER
    assert repo.dispatches[ctx["dispatch"]["id"]]["state"] == "RETRYABLE"


async def test_fatal_input_goes_terminal_without_retry() -> None:
    repo, starter, lookup, ctx = _setup()
    starter.behavior = "fatal"
    result = await _dispatcher(repo, starter, lookup).dispatch_once(worker_id="w1")
    assert result.outcome == DispatchOutcome.TERMINAL
    assert repo.dispatches[ctx["dispatch"]["id"]]["state"] == "TERMINAL"


async def test_exhausted_budget_skips_start() -> None:
    repo, starter, lookup, ctx = _setup()
    ctx["dispatch"]["attempts"] = 5
    result = await _dispatcher(repo, starter, lookup).dispatch_once(worker_id="w1")
    assert result.outcome == DispatchOutcome.TERMINAL
    assert starter.calls == []
    assert repo.dispatches[ctx["dispatch"]["id"]]["state"] == "TERMINAL"


async def test_duplicate_start_calls_starter_once() -> None:
    repo, starter, lookup, ctx = _setup()
    dispatcher = _dispatcher(repo, starter, lookup)
    first = await dispatcher.dispatch_once(worker_id="w1")
    assert first.outcome == DispatchOutcome.STARTED
    second = await dispatcher.dispatch_once(worker_id="w1")
    assert second.outcome == DispatchOutcome.NO_WORK
    assert len(starter.calls) == 1


async def test_canceled_request_never_starts() -> None:
    repo, starter, lookup, ctx = _setup()
    repo.requests[
        ("tenant-a", ctx["request"]["request_id"], 1)
    ]["state"] = "CANCELED"
    result = await _dispatcher(repo, starter, lookup).dispatch_once(worker_id="w1")
    assert result.outcome == DispatchOutcome.TERMINAL
    assert starter.calls == []


async def test_reconcile_requeues_expired_and_repairs_nothing_else() -> None:
    repo, starter, lookup, ctx = _setup()
    ctx["dispatch"]["state"] = "CLAIMED"
    ctx["dispatch"]["lease_owner"] = "w-old"
    from datetime import datetime

    ctx["dispatch"]["lease_expires_at"] = datetime.now(UTC)
    report = await _dispatcher(repo, starter, lookup).reconcile_once(worker_id="w1")
    assert report.requeued_expired == 1
    assert ctx["dispatch"]["state"] == "RETRYABLE"
    assert report.no_op == 0


async def test_reconcile_escalates_missing_workflow() -> None:
    repo, starter, lookup, ctx = _setup()
    ctx["dispatch"]["state"] = "ACCEPTED"
    ctx["dispatch"]["temporal_run_id"] = "run-gone"
    report = await _dispatcher(repo, starter, lookup).reconcile_once(worker_id="w1")
    assert report.terminal_mirrored == 1
    assert ctx["dispatch"]["state"] == "TERMINAL"


async def test_reconcile_terminates_exhausted_retries() -> None:
    repo, starter, lookup, ctx = _setup()
    ctx["dispatch"]["state"] = "RETRYABLE"
    ctx["dispatch"]["attempts"] = 7
    report = await _dispatcher(repo, starter, lookup).reconcile_once(worker_id="w1")
    assert report.retry_exhausted == 1
    assert ctx["dispatch"]["state"] == "TERMINAL"


async def test_reconcile_leaves_healthy_accepted_alone() -> None:
    canceled: list[str] = []

    class FakeCanceler:
        async def cancel(self, *, workflow_id: str, reason: str) -> None:
            canceled.append(workflow_id)

    repo, starter, lookup, ctx = _setup()
    ctx["dispatch"]["state"] = "ACCEPTED"
    lookup.by_workflow[ctx["dispatch"]["workflow_id"]] = _receipt(
        ctx["dispatch"]["workflow_id"], status="running"
    )
    report = await _dispatcher(repo, starter, lookup).reconcile_once(
        worker_id="w1", canceler=FakeCanceler()
    )
    assert report.no_op == 1
    assert canceled == []
    assert ctx["dispatch"]["state"] == "ACCEPTED"


async def test_cancel_requested_without_canceler_defers_running_work() -> None:
    repo, starter, lookup, ctx = _setup()
    ctx["dispatch"]["state"] = "ACCEPTED"
    repo.requests[("tenant-a", ctx["request"]["request_id"], 1)]["state"] = (
        "CANCEL_REQUESTED"
    )
    lookup.by_workflow[ctx["dispatch"]["workflow_id"]] = _receipt(
        ctx["dispatch"]["workflow_id"]
    )
    report = await _dispatcher(repo, starter, lookup).reconcile_once(worker_id="w1")
    assert report.deferred_cancel == 1
    assert ctx["dispatch"]["state"] == "ACCEPTED"


async def test_cancel_requested_with_missing_workflow_completes_cancel() -> None:
    canceled: list[str] = []

    class FakeCanceler:
        async def cancel(self, *, workflow_id: str, reason: str) -> None:
            canceled.append(workflow_id)

    repo, starter, lookup, ctx = _setup()
    ctx["dispatch"]["state"] = "ACCEPTED"
    repo.requests[("tenant-a", ctx["request"]["request_id"], 1)]["state"] = (
        "CANCEL_REQUESTED"
    )
    report = await _dispatcher(repo, starter, lookup).reconcile_once(
        worker_id="w1", canceler=FakeCanceler()
    )
    assert report.canceled == 1
    assert ctx["dispatch"]["state"] == "TERMINAL"
    assert repo.requests[("tenant-a", ctx["request"]["request_id"], 1)]["state"] == (
        "CANCELED"
    )
    assert canceled == []


async def test_stale_fencing_token_refuses_write() -> None:
    repo, starter, lookup, ctx = _setup()
    original_accept = repo.record_workflow_accepted

    async def stale_once(tenant_id: str, dispatch_id: UUID, fencing_token: int, run_id: str) -> bool:
        assert fencing_token == ctx["dispatch"]["lease_fencing_token"]
        return False

    repo.record_workflow_accepted = stale_once  # type: ignore[method-assign]
    try:
        result = await _dispatcher(repo, starter, lookup).dispatch_once(worker_id="w1")
    finally:
        repo.record_workflow_accepted = original_accept  # type: ignore[method-assign]
    assert result.outcome == DispatchOutcome.STALE
    assert ctx["dispatch"]["state"] == "CLAIMED"


async def test_worker_restart_recovers_abandoned_claim() -> None:
    repo, starter, lookup, ctx = _setup()
    # Claim directly and drop the worker before it records the start:
    # the fencing token advanced exactly once and the lease is held.
    claim = await repo.claim_and_reconcile(worker_id="w-old")
    assert claim is not None
    first_token = claim.lease_fencing_token
    # The lease lapses with no process behind it.
    ctx["dispatch"]["lease_expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    report = await _dispatcher(repo, starter, lookup).reconcile_once(worker_id="w-new")
    assert report.requeued_expired == 1
    assert ctx["dispatch"]["state"] == "RETRYABLE"
    # A new worker picks the same dispatch up and starts it exactly once.
    started = await _dispatcher(repo, starter, lookup).dispatch_once(worker_id="w-new")
    assert started.outcome == DispatchOutcome.STARTED
    assert len(starter.calls) == 1
    assert starter.calls[0].workflow_id == ctx["dispatch"]["workflow_id"]
    assert ctx["dispatch"]["lease_fencing_token"] == first_token + 1


async def test_reconcile_adopts_new_run_id_on_drift() -> None:
    repo, starter, lookup, ctx = _setup()
    ctx["dispatch"]["state"] = "ACCEPTED"
    ctx["dispatch"]["temporal_run_id"] = "run-old"
    lookup.by_workflow[ctx["dispatch"]["workflow_id"]] = _receipt(
        ctx["dispatch"]["workflow_id"], run_id="run-new", status="running"
    )
    report = await _dispatcher(repo, starter, lookup).reconcile_once(worker_id="w1")
    assert ctx["dispatch"]["temporal_run_id"] == "run-new"
    assert any("run-new" in note for note in report.notes)


def test_retry_backoff_grows_then_caps() -> None:
    assert retry_backoff_seconds(0) == 60
    assert retry_backoff_seconds(1) == 120
    assert retry_backoff_seconds(10) == 3600
    with pytest.raises(ValueError):
        retry_backoff_seconds(-1)


def test_terminal_for_mirror_mapping() -> None:
    assert terminal_for_mirror("completed") == (True, "")
    assert terminal_for_mirror("FAILED") == (True, "workflow_failed")
    assert terminal_for_mirror("canceled") == (True, "workflow_canceled")
    assert terminal_for_mirror("running") == (False, "")
    assert terminal_for_mirror("unknown-status-xyz") == (False, "")
