"""Phase 2 acceptance: atomic DRAFT→QUEUED with job, dispatch, and audit."""

from __future__ import annotations

from typing import Any, Self
from uuid import uuid4

import pytest

from investigation_agent_platform.infrastructure.persistence.onboarding_acceptance_repository import (
    AcceptanceGenerationError,
    AcceptanceStaleError,
    SqlAlchemyOnboardingAcceptanceRepository,
    deterministic_workflow_id,
)


class FakeResult:
    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        rowcount: int = 0,
    ) -> None:
        self._rows = rows or []
        self.rowcount = rowcount

    def mappings(self) -> Self:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self) -> Any:
        if not self._rows:
            return None
        first = self._rows[0]
        if isinstance(first, dict):
            return next(iter(first.values()), None)
        return first


class FakeSession:
    """In-memory request/job/dispatch tables with unique-constraint semantics."""

    def __init__(self, owner: FakeSessionFactory) -> None:
        self.owner = owner

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def begin_nested(self) -> FakeNested:
        return FakeNested()

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> FakeResult:
        sql = str(statement)
        bound = dict(params or {})
        self.owner.statements.append(sql)
        if "set_config" in sql or sql.startswith("SET LOCAL"):
            return FakeResult()
        if "FROM onboarding_requests" in sql:
            rows = [
                row
                for row in self.owner.requests
                if all(row.get(key) == value for key, value in bound.items())
            ]
            return FakeResult(rows)
        if "FROM onboarding_dispatches" in sql and "ORDER BY" in sql:
            rows = sorted(
                (
                    row
                    for row in self.owner.dispatches
                    if all(row.get(key) == value for key, value in bound.items())
                ),
                key=lambda row: row["execution_generation"],
                reverse=True,
            )
            return FakeResult(rows)
        if sql.startswith("UPDATE onboarding_requests"):
            if self.owner.inject_race:
                # Simulate a concurrent winner committing between our
                # pre-check and our CAS: request flips, dispatch lands.
                self.owner.inject_race = False
                for row in self.owner.requests:
                    if (
                        row["tenant_id"] == bound["tenant_id"]
                        and row["request_id"] == bound["request_id"]
                    ):
                        row["state"] = "QUEUED"
                        row["state_version"] += 1
                        row["job_id"] = uuid4()
                self.owner.dispatches.append(
                    {
                        "id": uuid4(),
                        "tenant_id": bound["tenant_id"],
                        "request_id": bound["request_id"],
                        "revision": bound["revision"],
                        "execution_generation": 1,
                        "job_id": uuid4(),
                        "workflow_id": "onboarding-winner",
                        "state": "PENDING",
                    }
                )
            matches = [
                row
                for row in self.owner.requests
                if row["tenant_id"] == bound["tenant_id"]
                and row["request_id"] == bound["request_id"]
                and row["revision"] == bound["revision"]
                and row["state"] == "DRAFT"
                and (
                    "expected_state_version" not in bound
                    or row["state_version"] == bound["expected_state_version"]
                )
            ]
            if len(matches) != 1:
                return FakeResult([])
            matches[0]["state"] = "QUEUED"
            matches[0]["state_version"] += 1
            matches[0]["job_id"] = bound["job_id"]
            return FakeResult([{"id": matches[0]["id"]}])
        if sql.startswith("INSERT INTO background_jobs"):
            self.owner.jobs.append(dict(bound))
            return FakeResult(rowcount=1)
        if sql.startswith("DELETE FROM background_jobs"):
            self.owner.jobs = [
                job for job in self.owner.jobs if job.get("id") != bound.get("job_id")
            ]
            return FakeResult([])
        if sql.startswith("INSERT INTO onboarding_dispatches"):
            if self.owner.inject_insert_race:
                self.owner.inject_insert_race = False
                self.owner.dispatches.append(
                    {
                        "id": uuid4(),
                        "tenant_id": bound["tenant_id"],
                        "request_id": bound["request_id"],
                        "revision": bound["revision"],
                        "execution_generation": bound["generation"],
                        "job_id": uuid4(),
                        "workflow_id": "onboarding-insert-winner",
                        "state": "PENDING",
                    }
                )
            clash = any(
                row["workflow_id"] == bound["workflow_id"]
                or (
                    row["tenant_id"] == bound["tenant_id"]
                    and row["request_id"] == bound["request_id"]
                    and row["revision"] == bound["revision"]
                    and row["execution_generation"] == bound["generation"]
                )
                for row in self.owner.dispatches
            )
            if clash:
                # ON CONFLICT DO NOTHING: no row, no error.
                return FakeResult([])
            row = {
                "id": bound["id"],
                "tenant_id": bound["tenant_id"],
                "request_id": bound["request_id"],
                "revision": bound["revision"],
                "execution_generation": bound["generation"],
                "job_id": bound["job_id"],
                "workflow_id": bound["workflow_id"],
                "state": "PENDING",
            }
            self.owner.dispatches.append(row)
            return FakeResult([{"id": row["id"]}])
        if "record_onboarding_request_audit" in sql:
            self.owner.audits.append({**bound, "action": "ACCEPT", "database_role": "iap_app"})
            return FakeResult()
        if "FROM onboarding_dispatches WHERE" in sql:
            normalized = {
                ("execution_generation" if key == "generation" else key): value
                for key, value in bound.items()
            }
            rows = [
                row
                for row in self.owner.dispatches
                if all(row.get(key) == value for key, value in normalized.items())
            ]
            return FakeResult(rows)
        raise AssertionError(f"unhandled statement in fake: {sql[:80]}")


class FakeNested:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class FakeSessionFactory:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.dispatches: list[dict[str, Any]] = []
        self.jobs: list[dict[str, Any]] = []
        self.audits: list[dict[str, Any]] = []
        self.statements: list[str] = []
        self.inject_race = False
        self.inject_insert_race = False

    def __call__(self) -> FakeSession:
        return FakeSession(self)


def _draft_request(factory: FakeSessionFactory, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid4(),
        "tenant_id": "tenant-a",
        "request_id": uuid4(),
        "revision": 1,
        "idempotency_key": "intake-key-1",
        "content_hash": "c" * 64,
        "schema_version": "tenant-intake/v1",
        "policy_version": "policy-v1",
        "catalog_version": "catalog-v1",
        "state": "DRAFT",
        "state_version": 1,
        "actor_id": "sub-a",
        "job_id": None,
    }
    row.update(overrides)
    factory.requests.append(row)
    return row


def _repository(factory: FakeSessionFactory) -> SqlAlchemyOnboardingAcceptanceRepository:
    return SqlAlchemyOnboardingAcceptanceRepository(factory)


async def test_accept_happy_path_persists_all_four_records() -> None:
    factory = FakeSessionFactory()
    draft = _draft_request(factory)
    accepted = await _repository(factory).accept_draft(
        tenant_id="tenant-a",
        actor_sub="sub-a",
        request_id=draft["request_id"],
        expected_revision=1,
    )
    assert accepted.state == "PENDING"
    assert accepted.execution_generation == 1
    assert accepted.workflow_id == deterministic_workflow_id("tenant-a", draft["request_id"], 1, 1)
    assert draft["state"] == "QUEUED"
    assert len(factory.jobs) == 1
    assert factory.jobs[0]["workflow_id"] == accepted.workflow_id
    assert len(factory.dispatches) == 1
    assert len(factory.audits) == 1
    assert factory.audits[0]["action"] == "ACCEPT"
    assert factory.audits[0]["actor_sub"] == "sub-a"


async def test_accept_is_idempotent_on_repeat_calls() -> None:
    factory = FakeSessionFactory()
    draft = _draft_request(factory)
    repo = _repository(factory)
    kwargs = {
        "tenant_id": "tenant-a",
        "actor_sub": "sub-a",
        "request_id": draft["request_id"],
        "expected_revision": 1,
    }
    first = await repo.accept_draft(**kwargs)
    second = await repo.accept_draft(**kwargs)
    assert first.id == second.id
    assert len(factory.dispatches) == 1
    assert len(factory.jobs) == 1
    assert len(factory.audits) == 1


async def test_accept_rejects_missing_and_non_draft_revisions() -> None:
    factory = FakeSessionFactory()
    repo = _repository(factory)
    with pytest.raises(AcceptanceStaleError, match="no such"):
        await repo.accept_draft(
            tenant_id="tenant-a",
            actor_sub="sub-a",
            request_id=uuid4(),
            expected_revision=1,
        )
    draft = _draft_request(factory, state="QUEUED")
    with pytest.raises(AcceptanceStaleError, match="only DRAFT"):
        await repo.accept_draft(
            tenant_id="tenant-a",
            actor_sub="sub-a",
            request_id=draft["request_id"],
            expected_revision=1,
        )
    assert factory.dispatches == []
    assert factory.jobs == []
    assert factory.audits == []


async def test_second_generation_requires_terminal_predecessor() -> None:
    factory = FakeSessionFactory()
    draft = _draft_request(factory)
    repo = _repository(factory)
    base = {
        "tenant_id": "tenant-a",
        "actor_sub": "sub-a",
        "request_id": draft["request_id"],
        "expected_revision": 1,
    }
    first = await repo.accept_draft(**base)
    with pytest.raises(AcceptanceGenerationError, match="terminal"):
        await repo.accept_draft(**base, execution_generation=2)
    factory.dispatches[0]["state"] = "TERMINAL"
    second = await repo.accept_draft(**base, execution_generation=2)
    assert second.execution_generation == 2
    assert second.workflow_id != first.workflow_id
    assert len(factory.audits) == 2
    with pytest.raises(AcceptanceGenerationError, match="exactly one"):
        await repo.accept_draft(**base, execution_generation=4)


async def test_explicit_generation_replays_existing_row() -> None:
    factory = FakeSessionFactory()
    draft = _draft_request(factory)
    repo = _repository(factory)
    base = {
        "tenant_id": "tenant-a",
        "actor_sub": "sub-a",
        "request_id": draft["request_id"],
        "expected_revision": 1,
    }
    first = await repo.accept_draft(**base)
    replay = await repo.accept_draft(**base, execution_generation=1)
    assert replay.id == first.id
    assert len(factory.dispatches) == 1


async def test_canceled_requests_cannot_be_accepted() -> None:
    factory = FakeSessionFactory()
    for state in ("CANCEL_REQUESTED", "CANCELED"):
        draft = _draft_request(factory, state=state)
        with pytest.raises(AcceptanceStaleError, match="canceled"):
            await _repository(factory).accept_draft(
                tenant_id="tenant-a",
                actor_sub="sub-a",
                request_id=draft["request_id"],
                expected_revision=1,
            )
    assert factory.dispatches == []
    assert factory.audits == []


async def test_expected_state_version_enforced() -> None:
    factory = FakeSessionFactory()
    draft = _draft_request(factory, state_version=3)
    with pytest.raises(AcceptanceStaleError, match="changed before acceptance"):
        await _repository(factory).accept_draft(
            tenant_id="tenant-a",
            actor_sub="sub-a",
            request_id=draft["request_id"],
            expected_revision=1,
            expected_state_version=2,
        )
    assert draft["state"] == "DRAFT"
    assert draft["state_version"] == 3
    assert factory.dispatches == []
    assert factory.jobs == []
    assert factory.audits == []
    accepted = await _repository(factory).accept_draft(
        tenant_id="tenant-a",
        actor_sub="sub-a",
        request_id=draft["request_id"],
        expected_revision=1,
        expected_state_version=3,
    )
    assert accepted.state == "PENDING"


async def test_concurrent_duplicate_returns_winner_without_side_effects() -> None:
    factory = FakeSessionFactory()
    draft = _draft_request(factory)
    factory.inject_race = True
    result = await _repository(factory).accept_draft(
        tenant_id="tenant-a",
        actor_sub="sub-a",
        request_id=draft["request_id"],
        expected_revision=1,
    )
    assert result.workflow_id == "onboarding-winner"
    assert result.state == "PENDING"
    assert factory.audits == []
    assert factory.jobs == []
    assert len(factory.dispatches) == 1


async def test_concurrent_insert_returns_winner_and_cleans_up() -> None:
    factory = FakeSessionFactory()
    draft = _draft_request(factory)
    factory.inject_insert_race = True
    result = await _repository(factory).accept_draft(
        tenant_id="tenant-a",
        actor_sub="sub-a",
        request_id=draft["request_id"],
        expected_revision=1,
    )
    assert result.workflow_id == "onboarding-insert-winner"
    assert result.state == "PENDING"
    assert factory.audits == []
    assert factory.jobs == []
    assert len(factory.dispatches) == 1


async def test_workflow_id_is_deterministic_and_pinned() -> None:
    request_id = uuid4()
    first = deterministic_workflow_id("tenant-a", request_id, 2, 3)
    assert first == deterministic_workflow_id("tenant-a", request_id, 2, 3)
    assert first != deterministic_workflow_id("tenant-a", request_id, 2, 4)
    assert first.startswith("onboarding/tenant-a/")
    with pytest.raises(ValueError):
        deterministic_workflow_id("tenant-a", request_id, 0, 1)


async def test_validation_rejects_bad_inputs() -> None:
    factory = FakeSessionFactory()
    repo = _repository(factory)
    with pytest.raises(ValueError, match="tenant_id"):
        await repo.accept_draft(
            tenant_id="", actor_sub="s", request_id=uuid4(), expected_revision=1
        )
    with pytest.raises(ValueError, match="actor_sub"):
        await repo.accept_draft(
            tenant_id="t", actor_sub="", request_id=uuid4(), expected_revision=1
        )
    with pytest.raises(ValueError, match="revision"):
        await repo.accept_draft(
            tenant_id="t", actor_sub="s", request_id=uuid4(), expected_revision=0
        )
    with pytest.raises(ValueError, match="generation"):
        await repo.accept_draft(
            tenant_id="t",
            actor_sub="s",
            request_id=uuid4(),
            expected_revision=1,
            execution_generation=0,
        )
