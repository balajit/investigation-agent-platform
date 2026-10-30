"""DRAFT-only request persistence: idempotency, audit, and no side effects."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Self, cast
from uuid import UUID

import pytest
from sqlalchemy.sql import visitors

from investigation_agent_platform.application.onboarding.intake import (
    RepositoryReference,
    TenantIntakeEnvelope,
    TenantIntakeV1,
)
from investigation_agent_platform.infrastructure.persistence.onboarding_request_repository import (
    DraftIdempotencyConflictError,
    DraftRevisionConflictError,
    SqlAlchemyOnboardingRequestRepository,
)


class FakeRow:
    tenant_id: str
    idempotency_key: str
    request_id: UUID
    revision: int
    content_hash: str
    id: UUID
    state: str
    job_id: UUID | None
    actor_id: str

    def __init__(self, values: dict[str, Any]) -> None:
        self.__dict__.update(values)


class FakeSession:
    def __init__(self, owner: FakeSessionFactory) -> None:
        self.owner = owner

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> None:
        sql = str(statement)
        self.owner.statements.append((sql, params or {}))
        if "record_onboarding_request_audit" in sql:
            bound = dict(params or {})
            bound.setdefault("p_actor_sub", bound.get("actor_sub"))
            bound.setdefault("p_action", "CREATE")
            self.owner.audits.append(bound)
            self.owner.statements.append(("INSERT INTO onboarding_request_audits", bound))

    def begin_nested(self) -> FakeNestedTransaction:
        return FakeNestedTransaction()

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    async def scalar(self, statement: Any) -> Any:
        if getattr(statement, "is_insert", False):
            self.owner.statements.append((str(statement), {}))
            values = {
                column.key: value.value if hasattr(value, "value") else value
                for column, value in statement._values.items()
            }
            if any(
                row.tenant_id == values["tenant_id"]
                and row.idempotency_key == values["idempotency_key"]
                for row in self.owner.rows
            ):
                return None
            row = FakeRow(values)
            self.owner.rows.append(row)
            return row.id

        predicates = {
            cast(Any, node).left.key: cast(Any, node).right.value
            for node in visitors.iterate(statement.whereclause)
            if hasattr(node, "left")
            and hasattr(cast(Any, node).left, "key")
            and hasattr(cast(Any, node).right, "value")
        }
        matches = [
            row
            for row in self.owner.rows
            if all(getattr(row, key, None) == value for key, value in predicates.items())
        ]
        if "request_id" in predicates:
            return max(matches, key=lambda row: row.revision, default=None)
        return matches[0] if matches else None


class FakeSessionFactory:
    def __init__(self) -> None:
        self.rows: list[FakeRow] = []
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self.audits: list[dict[str, Any]] = []

    def __call__(self) -> FakeSession:
        return FakeSession(self)


class FakeNestedTransaction:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


@asynccontextmanager
async def fake_rls_session(factory: FakeSessionFactory, tenant_id: str) -> Any:
    if not tenant_id:
        raise ValueError("tenant scope required")
    async with factory() as session:
        yield session
        await session.commit()


@pytest.fixture
def draft_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[SqlAlchemyOnboardingRequestRepository, FakeSessionFactory]:
    import investigation_agent_platform.infrastructure.persistence.onboarding_request_repository as module

    factory = FakeSessionFactory()
    monkeypatch.setattr(module, "rls_session", fake_rls_session)
    return SqlAlchemyOnboardingRequestRepository(factory), factory


def draft_intake(environment: str = "test") -> TenantIntakeEnvelope:
    return TenantIntakeEnvelope(
        intake=TenantIntakeV1(
            application_id="example-application",
            environment=environment,
            repositories=(
                RepositoryReference(
                    provider_key="sample-git-host",
                    repository_id="example-team/example-service",
                ),
            ),
        )
    )


@pytest.mark.asyncio
async def test_draft_create_and_same_hash_replay_write_one_audit_and_no_jobs(
    draft_repository: tuple[SqlAlchemyOnboardingRequestRepository, FakeSessionFactory],
) -> None:
    repository, factory = draft_repository
    first = await repository.create_draft(
        tenant_id="tenant-a",
        actor_sub="verified-subject-a",
        idempotency_key="draft-create-1",
        intake=draft_intake(),
    )
    replay = await repository.create_draft(
        tenant_id="tenant-a",
        actor_sub="verified-subject-a",
        idempotency_key="draft-create-1",
        intake=draft_intake(),
    )

    assert first.id == replay.id
    assert first.state == "DRAFT" and first.job_id is None
    assert first.actor_id == "verified-subject-a"
    assert first.content_hash == draft_intake().intake_content_hash
    assert len(factory.rows) == 1
    assert len(factory.audits) == 1
    assert factory.audits[0]["p_actor_sub"] == "verified-subject-a"
    assert factory.audits[0]["p_action"] == "CREATE"
    inserts = [sql.lower() for sql, _params in factory.statements if "insert into" in sql.lower()]
    assert len(inserts) == 2  # one request row and its audit function call
    assert all(
        "background_jobs" not in sql and "onboarding_dispatches" not in sql for sql in inserts
    )


@pytest.mark.asyncio
async def test_same_key_different_hash_conflicts_without_another_audit(
    draft_repository: tuple[SqlAlchemyOnboardingRequestRepository, FakeSessionFactory],
) -> None:
    repository, factory = draft_repository
    await repository.create_draft(
        tenant_id="tenant-a",
        actor_sub="verified-subject-a",
        idempotency_key="draft-key-conflict",
        intake=draft_intake(),
    )
    with pytest.raises(DraftIdempotencyConflictError):
        await repository.create_draft(
            tenant_id="tenant-a",
            actor_sub="verified-subject-a",
            idempotency_key="draft-key-conflict",
            intake=draft_intake("production"),
        )
    assert len(factory.rows) == 1
    assert len(factory.audits) == 1


@pytest.mark.asyncio
async def test_draft_revision_cas_and_no_transition_beyond_draft(
    draft_repository: tuple[SqlAlchemyOnboardingRequestRepository, FakeSessionFactory],
) -> None:
    repository, factory = draft_repository
    first = await repository.create_draft(
        tenant_id="tenant-a",
        actor_sub="verified-subject-a",
        idempotency_key="draft-create-cas",
        intake=draft_intake(),
    )
    factory.rows[0].idempotency_key = "old-idempotency-key"
    second = await repository.create_draft(
        tenant_id="tenant-a",
        actor_sub="verified-subject-a",
        idempotency_key="draft-edit-cas",
        intake=draft_intake("staging"),
        request_id=UUID(str(first.request_id)),
        expected_revision=1,
    )
    assert second.revision == 2
    assert second.state == "DRAFT" and second.job_id is None

    factory.rows[-1].idempotency_key = "old-edit-key"
    factory.rows[-1].state = "QUEUED"
    before = len(factory.rows)
    with pytest.raises(DraftRevisionConflictError, match="only DRAFT"):
        await repository.create_draft(
            tenant_id="tenant-a",
            actor_sub="verified-subject-a",
            idempotency_key="draft-edit-after-queued",
            intake=draft_intake("production"),
            request_id=UUID(str(first.request_id)),
            expected_revision=2,
        )
    assert len(factory.rows) == before
    assert not any(
        "insert into background_jobs" in sql.lower() for sql, _params in factory.statements
    )
    assert not any(
        "insert into onboarding_dispatches" in sql.lower() for sql, _params in factory.statements
    )
