# tests/unit/test_correlation_repository.py
"""Correlation relationship repository: SQL must match the real schema.

Regression: the traversal filtered on evidence_relationships.application_id,
a column that does not exist (the table carries tenant_id +
investigation_id), so every CORRELATE died with UndefinedColumnError.
Application scoping resolves through a JOIN on investigations; node IDs
bind as text[] (uuid[] fails the varchar comparison).
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from investigation_agent_platform.infrastructure.correlation.repository import (
    PostgresRelationshipRepository,
)


def _repo(captured: list):  # type: ignore[no-untyped-def]
    async def _execute(sql, params):  # type: ignore[no-untyped-def]
        captured.append((str(sql), dict(params)))

        def _empty():  # type: ignore[no-untyped-def]
            return SimpleNamespace(all=lambda rows=[]: rows)

        return SimpleNamespace(mappings=_empty)

    session = SimpleNamespace(execute=_execute)

    class _CM:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return session

        async def __aexit__(self, *args):  # type: ignore[no-untyped-def]
            return False

    return PostgresRelationshipRepository(lambda: _CM())


class TestRelationshipQueryShape:
    @pytest.mark.asyncio
    async def test_app_scoping_joins_investigations(self) -> None:
        captured: list = []
        repo = _repo(captured)
        result = await repo.fetch_relationships_for_evidence(
            "tenant-a", "my-app", [str(uuid4())], 2
        )
        assert result == []
        assert len(captured) == 1
        sql, params = captured[0]
        assert "JOIN investigations" in sql
        assert "evidence_relationships.application_id" not in sql
        assert "r.application_id" not in sql
        assert params["tenant_id"] == "tenant-a"
        assert params["application_id"] == "my-app"
        assert params["max_depth"] == 2

    @pytest.mark.asyncio
    async def test_invalid_uuid_rejected_before_io(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import ExecutionError

        captured: list = []
        repo = _repo(captured)
        with pytest.raises(ExecutionError):
            await repo.fetch_relationships_for_evidence("tenant-a", "my-app", ["not-a-uuid"], 2)
        assert captured == []


class TestRecordEdges:
    def _repo_with(self, existing, added):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.infrastructure.correlation.repository import (
            PostgresRelationshipRepository,
        )

        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return list(self._rows)

        async def _scalars(stmt):  # type: ignore[no-untyped-def]
            return _Result(existing)

        async def _commit():  # type: ignore[no-untyped-def]
            committed.append(True)

        committed: list = []
        session = SimpleNamespace(scalars=_scalars, add=added.append, commit=_commit)

        class _CM:
            async def __aenter__(self):  # type: ignore[no-untyped-def]
                return session

            async def __aexit__(self, *args):  # type: ignore[no-untyped-def]
                return False

        return PostgresRelationshipRepository(lambda: _CM())

    def _edge(self, src="s1", tgt="e1"):  # type: ignore[no-untyped-def]
        from investigation_agent_platform.ports.persistence.repositories import (
            EvidenceRelationshipRecord,
        )

        return EvidenceRelationshipRecord(
            tenant_id="tenant-a",
            application_id="app-1",
            source_id=src,
            target_id=tgt,
        )

    @pytest.mark.asyncio
    async def test_inserts_missing(self) -> None:
        from uuid import uuid4

        added: list = []
        repo = self._repo_with([], added)
        n = await repo.record_edges("tenant-a", uuid4(), "app-1", [self._edge()])
        assert n == 1
        assert len(added) == 1

    @pytest.mark.asyncio
    async def test_skips_existing(self) -> None:
        from uuid import uuid4

        added: list = []
        repo = self._repo_with([("s1", "e1", "CORRELATES_WITH")], added)
        n = await repo.record_edges("tenant-a", uuid4(), "app-1", [self._edge()])
        assert n == 0
        assert added == []

    @pytest.mark.asyncio
    async def test_wrong_tenant_or_app_dropped(self) -> None:
        from uuid import uuid4

        added: list = []
        repo = self._repo_with([], added)
        other = self._edge()
        other = other.model_copy(update={"tenant_id": "tenant-b"})
        n = await repo.record_edges("tenant-a", uuid4(), "app-1", [other])
        assert n == 0
        assert added == []
