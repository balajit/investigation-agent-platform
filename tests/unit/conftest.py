# tests/unit/conftest.py
"""Shared test doubles (H14-D3: the conclude path's unit of work)."""

from __future__ import annotations

from typing import Any
from uuid import UUID


class FakeConclusionUnitOfWork:
    """Test double for the conclude path's unit of work.

    Records the commit call and delegates to the repos' own single-write
    methods (the same three writes, no shared session). Repos resolve from
    the context at call time so tests may attach finding repos after the
    context is built. Tests that need a faulting unit replace or wrap
    ``commit_verdict``.
    """

    def __init__(
        self,
        hypothesis_repo: Any = None,
        finding_repo: Any = None,
        ctx: Any = None,
    ) -> None:
        self._hypothesis_repo = hypothesis_repo
        self._finding_repo = finding_repo
        self._ctx = ctx
        self.calls: list[dict[str, Any]] = []

    def _repos(self) -> tuple[Any, Any]:
        ctx = self._ctx
        hyp_repo = getattr(ctx, "hypothesis_repo", None) if ctx is not None else None
        find_repo = getattr(ctx, "finding_repo", None) if ctx is not None else None
        return hyp_repo or self._hypothesis_repo, find_repo or self._finding_repo

    async def commit_verdict(
        self,
        tenant_id: str,
        *,
        investigation_id: UUID,
        hypothesis: Any | None,
        expected_version: int,
        finding: Any,
        conclusion: Any,
    ) -> None:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "investigation_id": investigation_id,
                "hypothesis": hypothesis,
                "expected_version": expected_version,
                "finding": finding,
                "conclusion": conclusion,
            }
        )
        hyp_repo, find_repo = self._repos()
        if hypothesis is not None and hyp_repo is not None:
            await hyp_repo.save(tenant_id, hypothesis, investigation_id, expected_version)
        if find_repo is not None:
            await find_repo.save_finding_record(tenant_id, finding)
            await find_repo.save_conclusion(tenant_id, conclusion)
