# tests/unit/test_execute_budget.py
"""Execute-action evidence budgeting: per-action persistence honors the
remaining investigation budget instead of banking whole provider pages.

Regression: one broad SEARCH_LOGS (limit 100) banked 40+ rows against the
25-item default budget, so the very next iteration tripped BUDGET_EXCEEDED
and the investigation failed without ever hypothesizing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from investigation_agent_platform.api.dependencies import (
    AppContext,
    set_app_context,
)
from investigation_agent_platform.application.worker.activities import (
    ExecuteActionInput,
    execute_action_activity,
)


def _ctx_with(*, preexisting: int, returned: int):  # type: ignore[no-untyped-def]
    ctx = AppContext()
    saved: list = []
    seen_limits: list = []

    async def _find(tenant_id, investigation_id):  # type: ignore[no-untyped-def]
        return [SimpleNamespace(id=i) for i in range(preexisting)]

    async def _save_batch(tenant_id, items, investigation_id):  # type: ignore[no-untyped-def]
        saved.extend(items)

    async def _count(tenant_id, investigation_id):  # type: ignore[no-untyped-def]
        return 0

    async def _search(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
        seen_limits.append(req.limit)
        # Mirror provider behavior: honor the requested page.
        return SimpleNamespace(items=[SimpleNamespace(id=i) for i in range(returned)][: req.limit])

    service = SimpleNamespace(
        authorize=AsyncMock(return_value=None),
        execute=AsyncMock(return_value=None),
    )
    ctx.execute_action_service = lambda: service  # type: ignore[method-assign]
    ctx.action_repo = SimpleNamespace(count_by_investigation=_count)
    ctx.evidence_repo = SimpleNamespace(find_by_investigation_id=_find, save_batch=_save_batch)
    ctx.profile_repo = SimpleNamespace(get_by_application_id=AsyncMock(return_value=None))
    ctx.evidence_gateway = SimpleNamespace(search_runtime_evidence=_search)
    set_app_context(ctx)
    return ctx, saved, seen_limits


def _input(**overrides):  # type: ignore[no-untyped-def]
    base = {
        "tenant_id": "tenant-a",
        "investigation_id": str(uuid4()),
        "action": "SEARCH_LOGS",
        "parameters": {"keywords": [f"kw-{uuid4().hex[:8]}"], "limit": 100},
    }
    base.update(overrides)
    return ExecuteActionInput(**base)


class TestEvidenceRoom:
    @pytest.mark.asyncio
    async def test_truncates_to_remaining_budget(self) -> None:
        _, saved, _ = _ctx_with(preexisting=20, returned=40)
        result = await execute_action_activity(_input())
        assert result.success is True
        # 25-item default budget minus 20 existing = room for 5.
        assert result.data["items_count"] == 5
        assert len(saved) == 5

    @pytest.mark.asyncio
    async def test_no_truncation_within_budget(self) -> None:
        _, saved, _ = _ctx_with(preexisting=0, returned=10)
        result = await execute_action_activity(_input())
        assert result.success is True
        assert result.data["items_count"] == 10
        assert len(saved) == 10

    @pytest.mark.asyncio
    async def test_default_page_leaves_room_for_later_iterations(self) -> None:
        # No explicit limit: the provider page defaults to 10 so one broad
        # action cannot bank the whole 25-item budget in a single iteration.
        _, saved, seen_limits = _ctx_with(preexisting=0, returned=100)
        params = dict(_input().parameters)
        params.pop("limit", None)
        result = await execute_action_activity(_input(parameters=params))
        assert seen_limits == [10]
        assert result.data["items_count"] == 10
        assert len(saved) == 10

    @pytest.mark.asyncio
    async def test_explicit_limit_still_clamped_to_200(self) -> None:
        _, _, saved_limits = _ctx_with(preexisting=0, returned=5)
        await execute_action_activity(_input(parameters={"keywords": ["k"], "limit": 500}))
        assert saved_limits == [200]

    @pytest.mark.asyncio
    async def test_query_state_default_page_bounded(self) -> None:
        ctx = AppContext()
        seen: list = []
        saved: list = []
        service = SimpleNamespace(
            authorize=AsyncMock(return_value=None),
            execute=AsyncMock(return_value=None),
        )
        ctx.execute_action_service = lambda: service  # type: ignore[method-assign]
        ctx.action_repo = SimpleNamespace(count_by_investigation=AsyncMock(return_value=0))
        ctx.evidence_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[]),
            save_batch=AsyncMock(side_effect=lambda t, i, v: saved.extend(i)),
        )
        ctx.profile_repo = SimpleNamespace(get_by_application_id=AsyncMock(return_value=None))

        async def _state(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            seen.append(req.limit)
            return SimpleNamespace(items=[SimpleNamespace(id=i) for i in range(50)][: req.limit])

        ctx.evidence_gateway = SimpleNamespace(get_application_state=_state)
        set_app_context(ctx)
        result = await execute_action_activity(
            ExecuteActionInput(
                tenant_id="tenant-a",
                investigation_id=str(uuid4()),
                action="QUERY_STATE",
                parameters={"template_id": "primary.evidence_recent"},
            )
        )
        assert result.success is True
        assert seen == [10]
        assert result.data["items_count"] == 10
        assert len(saved) == 10


class TestBudgetAssignment:
    def test_tool_call_default_matches_operator_knob(self) -> None:
        from investigation_agent_platform.domain.investigation.models import (
            InvestigationLimits,
        )

        # The enforced default must agree with BudgetConfig/IAP_MAX_TOOL_CALLS
        # (50); previously the domain defaulted to 100 while the documented
        # knob said 50, so the knob did nothing.
        assert InvestigationLimits().max_tool_calls == 50

    def test_overlay_applies_maximum_hypotheses(self) -> None:
        from types import SimpleNamespace

        from investigation_agent_platform.application.worker.activities import (
            overlay_evidence_budget,
        )
        from investigation_agent_platform.domain.investigation.models import (
            InvestigationLimits,
        )

        profile = SimpleNamespace(
            investigation=SimpleNamespace(evidence_budget=None, max_hypotheses=3)
        )
        assert overlay_evidence_budget(InvestigationLimits(), profile).max_hypotheses == 3

    def test_overlay_evidence_budget_dict_wins(self) -> None:
        from types import SimpleNamespace

        from investigation_agent_platform.application.worker.activities import (
            overlay_evidence_budget,
        )
        from investigation_agent_platform.domain.investigation.models import (
            InvestigationLimits,
        )

        profile = SimpleNamespace(
            investigation=SimpleNamespace(evidence_budget={"max_hypotheses": 7}, max_hypotheses=3)
        )
        assert overlay_evidence_budget(InvestigationLimits(), profile).max_hypotheses == 7

    def test_overlay_never_raises(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            overlay_evidence_budget,
        )
        from investigation_agent_platform.domain.investigation.models import (
            InvestigationLimits,
        )

        assert overlay_evidence_budget(InvestigationLimits(), None).max_hypotheses == 10
        assert overlay_evidence_budget(InvestigationLimits(), object()).max_hypotheses == 10

    @pytest.mark.asyncio
    async def test_hypothesis_total_capped(self) -> None:
        ctx = AppContext()
        existing = [SimpleNamespace(statement=f"old-{i}") for i in range(4)]
        saved: list = []
        service = SimpleNamespace(
            authorize=AsyncMock(return_value=None),
            execute=AsyncMock(return_value=None),
        )
        ctx.execute_action_service = lambda: service  # type: ignore[method-assign]
        ctx.action_repo = SimpleNamespace(count_by_investigation=AsyncMock(return_value=0))
        ctx.evidence_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[]),
            save_batch=AsyncMock(return_value=None),
        )
        ctx.profile_repo = SimpleNamespace(get_by_application_id=AsyncMock(return_value=None))
        ctx.hypothesis_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=existing),
            save=AsyncMock(side_effect=lambda t, h, i, v: saved.append(h)),
        )
        set_app_context(ctx)
        # Default limits cap at 10 total; 4 exist so only 6 of 20 proposed persist.
        result = await execute_action_activity(
            ExecuteActionInput(
                tenant_id="tenant-a",
                investigation_id=str(uuid4()),
                action="FORMULATE_HYPOTHESIS",
                hypotheses=[f"new-{i}" for i in range(20)],
            )
        )
        assert result.success is True
        assert len(saved) == 6

    @pytest.mark.asyncio
    async def test_search_code_persists_attached_hypotheses(self) -> None:
        # Regression (live trace 3c80875c): the reasoner attached a
        # hypothesis to a SEARCH_CODE action; the dispatch log counted it
        # (hypotheses_count=1) but it was never persisted — persistence only
        # ran for FORMULATE_HYPOTHESIS / VERIFY_HYPOTHESIS / CONCLUDE, so the
        # coordinator kept seeing hypotheses=0 and the run died with
        # INSUFFICIENT_EVIDENCE. Persistence now runs for every action type.
        ctx = AppContext()
        saved_hypos: list = []
        service = SimpleNamespace(
            authorize=AsyncMock(return_value=None),
            execute=AsyncMock(return_value=None),
        )
        ctx.execute_action_service = lambda: service  # type: ignore[method-assign]
        ctx.action_repo = SimpleNamespace(count_by_investigation=AsyncMock(return_value=0))
        ctx.evidence_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[]),
            save_batch=AsyncMock(return_value=None),
            save=AsyncMock(return_value=None),
        )
        ctx.profile_repo = SimpleNamespace(get_by_application_id=AsyncMock(return_value=None))
        ctx.evidence_gateway = SimpleNamespace(
            search_code=AsyncMock(return_value=SimpleNamespace(items=[], provider_metadata={}))
        )
        ctx.hypothesis_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[]),
            save=AsyncMock(side_effect=lambda t, h, i, v: saved_hypos.append(h)),
        )
        set_app_context(ctx)
        result = await execute_action_activity(
            ExecuteActionInput(
                tenant_id="tenant-a",
                investigation_id=str(uuid4()),
                action="SEARCH_CODE",
                parameters={"query": "OrderFilter"},
                hypotheses=["order filter drops nulls"],
            )
        )
        assert result.success is True
        assert len(saved_hypos) == 1
        assert saved_hypos[0].statement == "order filter drops nulls"

    @pytest.mark.asyncio
    async def test_get_code_degrades_when_provider_absent(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import (
            ExecutionError,
            ProviderNotConfiguredError,
        )

        ctx = AppContext()
        service = SimpleNamespace(
            authorize=AsyncMock(return_value=None),
            execute=AsyncMock(return_value=None),
        )
        ctx.execute_action_service = lambda: service  # type: ignore[method-assign]
        ctx.action_repo = SimpleNamespace(count_by_investigation=AsyncMock(return_value=0))
        ctx.evidence_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[]),
            save_batch=AsyncMock(return_value=None),
        )
        ctx.profile_repo = SimpleNamespace(get_by_application_id=AsyncMock(return_value=None))

        async def _missing(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            raise ProviderNotConfiguredError("No code provider configured for tenant context 't:a'")

        ctx.evidence_gateway = SimpleNamespace(get_source=_missing)
        # evidence_repo.save must exist for the handler shape; it must not be called.
        ctx.evidence_repo.save = AsyncMock(return_value=None)  # type: ignore[attr-defined]
        set_app_context(ctx)
        result = await execute_action_activity(
            ExecuteActionInput(
                tenant_id="tenant-a",
                investigation_id=str(uuid4()),
                action="GET_CODE",
                parameters={"file_path": "x.py"},
            )
        )
        assert result.success is True
        assert result.data["items_count"] == 0
        ctx.evidence_repo.save.assert_not_awaited()
        # Backward compatible: still an ExecutionError for existing handlers.
        assert issubclass(ProviderNotConfiguredError, ExecutionError)


class TestProviderNotConfigured:
    @pytest.mark.asyncio
    async def test_selector_absence_is_typed(self) -> None:
        from investigation_agent_platform.application.evidence.selector import (
            EvidenceProviderSelector,
        )
        from investigation_agent_platform.domain.common.exceptions import (
            ExecutionError,
            ProviderNotConfiguredError,
        )

        selector = EvidenceProviderSelector()
        with pytest.raises(ProviderNotConfiguredError):
            await selector.get_code_provider("t", "a")
        with pytest.raises(ExecutionError):
            await selector.get_code_provider("t", "a")

    @pytest.mark.asyncio
    async def test_sole_provider_fallback(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from investigation_agent_platform.application.evidence.selector import (
            EvidenceProviderSelector,
        )
        from investigation_agent_platform.infrastructure.evidence.code.git import (
            PyGit2Adapter,
        )

        selector = EvidenceProviderSelector()
        sole = PyGit2Adapter(repo_base_path=str(tmp_path))
        selector.register_code_provider("pygit2-local", sole)
        # Tenant lookup misses the global key but resolves the sole provider.
        assert await selector.get_code_provider("n-a", "investigation-agent-platform") is sole

    @pytest.mark.asyncio
    async def test_exact_key_wins_over_fallback(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from investigation_agent_platform.application.evidence.selector import (
            EvidenceProviderSelector,
        )
        from investigation_agent_platform.infrastructure.evidence.code.git import (
            PyGit2Adapter,
        )

        selector = EvidenceProviderSelector()
        global_one = PyGit2Adapter(repo_base_path=str(tmp_path))
        tenant_one = PyGit2Adapter(repo_base_path=str(tmp_path))
        selector.register_code_provider("pygit2-local", global_one)
        selector.register_code_provider("t:a", tenant_one)
        assert await selector.get_code_provider("t", "a") is tenant_one

    @pytest.mark.asyncio
    async def test_multiple_providers_stay_fail_closed(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from investigation_agent_platform.application.evidence.selector import (
            EvidenceProviderSelector,
        )
        from investigation_agent_platform.domain.common.exceptions import (
            ProviderNotConfiguredError,
        )
        from investigation_agent_platform.infrastructure.evidence.code.git import (
            PyGit2Adapter,
        )

        selector = EvidenceProviderSelector()
        selector.register_code_provider("one", PyGit2Adapter(repo_base_path=str(tmp_path)))
        selector.register_code_provider("two", PyGit2Adapter(repo_base_path=str(tmp_path)))
        with pytest.raises(ProviderNotConfiguredError):
            await selector.get_code_provider("t", "a")

    @pytest.mark.asyncio
    async def test_get_code_empty_path_degrades(self) -> None:
        ctx = AppContext()
        gateway_called: list = []
        service = SimpleNamespace(
            authorize=AsyncMock(return_value=None),
            execute=AsyncMock(return_value=None),
        )
        ctx.execute_action_service = lambda: service  # type: ignore[method-assign]
        ctx.action_repo = SimpleNamespace(count_by_investigation=AsyncMock(return_value=0))
        ctx.evidence_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[]),
            save_batch=AsyncMock(return_value=None),
        )
        ctx.profile_repo = SimpleNamespace(get_by_application_id=AsyncMock(return_value=None))

        async def _never(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            gateway_called.append(req)
            raise AssertionError("gateway must not be called without file_path")

        ctx.evidence_gateway = SimpleNamespace(get_source=_never)
        set_app_context(ctx)
        result = await execute_action_activity(
            ExecuteActionInput(
                tenant_id="tenant-a",
                investigation_id=str(uuid4()),
                action="GET_CODE",
                parameters={},
            )
        )
        assert result.success is True
        assert result.data["items_count"] == 0
        assert gateway_called == []

    @pytest.mark.asyncio
    async def test_get_code_path_miss_degrades(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import (
            CodePathNotFoundError,
        )

        ctx = AppContext()
        service = SimpleNamespace(
            authorize=AsyncMock(return_value=None),
            execute=AsyncMock(return_value=None),
        )
        ctx.execute_action_service = lambda: service  # type: ignore[method-assign]
        ctx.action_repo = SimpleNamespace(count_by_investigation=AsyncMock(return_value=0))
        ctx.evidence_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[]),
            save_batch=AsyncMock(return_value=None),
        )
        ctx.profile_repo = SimpleNamespace(get_by_application_id=AsyncMock(return_value=None))

        async def _miss(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            raise CodePathNotFoundError("Path not found in o/r@main: nope.py")

        ctx.evidence_gateway = SimpleNamespace(get_source=_miss)
        set_app_context(ctx)
        result = await execute_action_activity(
            ExecuteActionInput(
                tenant_id="tenant-a",
                investigation_id=str(uuid4()),
                action="GET_CODE",
                parameters={"file_path": "nope.py"},
            )
        )
        assert result.success is True
        assert result.data["items_count"] == 0

    def test_overlay_applies_max_iterations(self) -> None:
        from types import SimpleNamespace

        from investigation_agent_platform.application.worker.activities import (
            overlay_evidence_budget,
        )
        from investigation_agent_platform.domain.investigation.models import (
            InvestigationLimits,
        )

        assert InvestigationLimits().max_iterations == 10
        profile = SimpleNamespace(
            investigation=SimpleNamespace(evidence_budget={"max_iterations": 20})
        )
        assert overlay_evidence_budget(InvestigationLimits(), profile).max_iterations == 20


class TestResolveMaxIterations:
    def test_defaults_and_clamps(self) -> None:
        from investigation_agent_platform.application.worker.workflows import (
            _resolve_max_iterations,
        )

        assert _resolve_max_iterations(None) == 10
        assert _resolve_max_iterations(20) == 20
        assert _resolve_max_iterations(1) == 1
        assert _resolve_max_iterations(0) == 1
        assert _resolve_max_iterations(200) == 50


class TestTokenLedger:
    def test_decision_llm_usage_mapping(self) -> None:
        from types import SimpleNamespace

        from investigation_agent_platform.application.worker.activities import (
            _decision_llm_usage,
        )

        full = SimpleNamespace(
            metadata={
                "llm_usage": {"prompt_tokens": 100, "completion_tokens": 50, "cost_usd": 0.02}
            }
        )
        assert _decision_llm_usage(full) == {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "cost_usd": 0.02,
        }
        assert _decision_llm_usage(SimpleNamespace(metadata={})) == {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_usd": 0.0,
        }
        assert _decision_llm_usage(SimpleNamespace()) == {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_usd": 0.0,
        }
        garbage = SimpleNamespace(metadata={"llm_usage": {"prompt_tokens": "NaN!"}})
        assert _decision_llm_usage(garbage)["prompt_tokens"] == 0

    def test_overlay_applies_max_llm_tokens(self) -> None:
        from types import SimpleNamespace

        from investigation_agent_platform.application.worker.activities import (
            overlay_evidence_budget,
        )
        from investigation_agent_platform.domain.investigation.models import (
            InvestigationLimits,
        )

        assert InvestigationLimits().max_llm_tokens == 1000000
        profile = SimpleNamespace(
            investigation=SimpleNamespace(evidence_budget={"max_llm_tokens": 5000})
        )
        assert overlay_evidence_budget(InvestigationLimits(), profile).max_llm_tokens == 5000

    @pytest.mark.asyncio
    async def test_reason_budget_exceeded_routes_to_conclude(self) -> None:
        from datetime import UTC, datetime, timedelta
        from types import SimpleNamespace

        from investigation_agent_platform.api.dependencies import (
            AppContext,
            set_app_context,
        )
        from investigation_agent_platform.application.worker.activities import (
            ReasonInput,
            reason_activity,
        )
        from investigation_agent_platform.domain.investigation.models import (
            Investigation,
            InvestigationContext,
            InvestigationRequest,
            InvestigationStatus,
        )

        ctx = AppContext()
        now = datetime.now(UTC)
        inv = Investigation(
            session_id="s",
            application_id="a",
            tenant_id="tenant-a",
            request=InvestigationRequest(
                application_id="a",
                problem_description="p",
                session_id="s",
                requested_by="t",
            ),
            context=InvestigationContext(
                environment="dev", time_window=(now - timedelta(hours=1), now)
            ),
            status=InvestigationStatus.INVESTIGATING,
            created_at=now,
            updated_at=now,
            version=1,
        )
        await ctx.investigation_repo.create("tenant-a", inv)
        decision = SimpleNamespace(
            action=None,
            conclusion_readiness=0.1,
            observations=[],
            hypotheses_evaluated=[],
            metadata={
                "llm_usage": {"prompt_tokens": 900000, "completion_tokens": 200000, "cost_usd": 1.0}
            },
        )
        ctx.reasoning_coordinator = SimpleNamespace(
            reason=lambda *a, **k: _await(decision),
            attach_profile_repo=lambda r: None,
        )
        set_app_context(ctx)
        result = await reason_activity(
            ReasonInput(tenant_id="tenant-a", investigation_id=str(inv.id), iteration=0)
        )
        assert result.success is True
        assert result.data.get("budget_exceeded") is True

    @pytest.mark.asyncio
    async def test_checkpoint_merges_usage_totals(self) -> None:
        from datetime import UTC, datetime, timedelta

        from investigation_agent_platform.api.dependencies import (
            AppContext,
            set_app_context,
        )
        from investigation_agent_platform.application.worker.activities import (
            CheckpointInput,
            checkpoint_activity,
        )
        from investigation_agent_platform.domain.investigation.models import (
            Investigation,
            InvestigationContext,
            InvestigationRequest,
            InvestigationStatus,
        )

        ctx = AppContext()
        set_app_context(ctx)
        now = datetime.now(UTC)
        inv = Investigation(
            session_id="s",
            application_id="a",
            tenant_id="tenant-a",
            request=InvestigationRequest(
                application_id="a",
                problem_description="p",
                session_id="s",
                requested_by="t",
            ),
            context=InvestigationContext(
                environment="dev", time_window=(now - timedelta(hours=1), now)
            ),
            status=InvestigationStatus.CREATED,
            created_at=now,
            updated_at=now,
            version=1,
        )
        await ctx.investigation_repo.create("tenant-a", inv)
        await checkpoint_activity(
            CheckpointInput(
                tenant_id="tenant-a",
                investigation_id=str(inv.id),
                step=0,
                usage_totals={"llm_tokens": 1234, "llm_cost": 0.05},
            )
        )
        snap = await ctx.checkpoint_repo.get_latest_checkpoint("tenant-a", inv.id)
        assert snap["llm_tokens_used"] == 1234
        assert snap["llm_cost_usd"] == 0.05


def _await(value):  # type: ignore[no-untyped-def]
    async def _inner():
        return value

    return _inner()
