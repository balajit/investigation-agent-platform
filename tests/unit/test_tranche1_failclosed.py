# tests/unit/test_tranche1_failclosed.py
"""Tranche 1 fail-closed fixes (review H9, H10, T1, R7).

Each test pins the behavior that was broken before the fix.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from investigation_agent_platform.domain.hypothesis.models import Hypothesis, HypothesisStatus


def _hyp() -> Hypothesis:
    return Hypothesis(
        tenant_id="t",
        investigation_id=uuid4(),
        title="h",
        description="h",
        statement="where is Foo defined",
        status=HypothesisStatus.PROPOSED,
    )


def _ctx(investigation_repo, saved: dict):
    from investigation_agent_platform.api.dependencies import AppContext, set_app_context

    ctx = AppContext()
    ctx.investigation_repo = investigation_repo  # type: ignore[attr-defined]
    ctx.evidence_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=[])
    )
    ctx.hypothesis_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=[_hyp()]),
        save=AsyncMock(),
    )

    async def _save(tenant_id, conclusion):
        saved["conclusion"] = conclusion

    ctx.finding_repo = SimpleNamespace(save_conclusion=_save)  # type: ignore[attr-defined]
    set_app_context(ctx)
    return ctx


async def _run_gate(ctx):
    from investigation_agent_platform.application.worker.activities.lifecycle import (
        _gate_conclusion_or_fail,
    )

    return await _gate_conclusion_or_fail(
        "t", uuid4(), ctx, shadow_notes=None, triage_route=""
    )


class TestH9AbstentionBeforeCandidate:
    """_persist_abstention used to close over `candidate` before it was bound."""

    @pytest.mark.asyncio
    async def test_repo_read_failure_persists_insufficient_not_nameerror(self):
        saved: dict = {}
        repo = SimpleNamespace(get_by_id=AsyncMock(side_effect=RuntimeError("db down")))
        status = await _run_gate(_ctx(repo, saved))
        assert str(getattr(status, "value", status)) == "FAILED"
        conclusion = saved["conclusion"]
        # A repository fault is NOT a refutation of the claim.
        assert str(conclusion.status) == "INSUFFICIENT_EVIDENCE"
        assert any("repository read failed" in lim for lim in conclusion.limitations)

    @pytest.mark.asyncio
    async def test_investigation_not_found_persists_insufficient(self):
        saved: dict = {}
        repo = SimpleNamespace(get_by_id=AsyncMock(return_value=None))
        await _run_gate(_ctx(repo, saved))
        assert str(saved["conclusion"].status) == "INSUFFICIENT_EVIDENCE"

    @pytest.mark.asyncio
    async def test_unknown_pin_persists_insufficient(self):
        saved: dict = {}
        repo = SimpleNamespace(
            get_by_id=AsyncMock(
                return_value=SimpleNamespace(
                    metadata={"claim_binding_version": "cbv99"}, application_id="app"
                )
            )
        )
        await _run_gate(_ctx(repo, saved))
        assert str(saved["conclusion"].status) == "INSUFFICIENT_EVIDENCE"
        assert any("claim binding version unavailable" in x for x in saved["conclusion"].limitations)


class TestH10EnforcedInconclusiveClearsFastPathFlags:
    """The not-VERIFIED flag reset sat inside the `except` body and never ran."""

    @pytest.mark.asyncio
    async def test_enforced_inconclusive_reaches_gate_with_flags_cleared(self, monkeypatch):
        import test_claim_binding_phase1 as fixtures

        from investigation_agent_platform.application.investigation.verification import (
            ConclusionGate,
            VerificationEngine,
        )

        row_a, row_b = fixtures._two_caller_rows()
        hyp = fixtures._two_caller_hyp(row_a, row_b)
        inv_id = uuid4()
        hyp = hyp.model_copy(update={"investigation_id": inv_id, "tenant_id": "t"})
        ctx = fixtures._activity_ctx(
            {"claim_binding_version": "cbv2", "verifier_version": "v2"},
            hyp,
            [row_a, row_b],
            [fixtures._attempt_event([str(row_a.evidence_id), str(row_b.evidence_id)])],
        )

        async def _save(tenant_id, conclusion):
            return None

        ctx.finding_repo = SimpleNamespace(save_conclusion=_save)

        def _enforce_inconclusive(self, hypothesis, result, enforcement):
            return self._fault_inconclusive(hypothesis)

        monkeypatch.setattr(VerificationEngine, "enforce_claim_binding", _enforce_inconclusive)

        seen: dict = {}
        real_evaluate = ConclusionGate.evaluate

        async def _spy(self, *args, **kwargs):
            seen.update(kwargs)
            return await real_evaluate(self, *args, **kwargs)

        monkeypatch.setattr(ConclusionGate, "evaluate", _spy)

        await _run_gate(ctx)
        flags = {k: v for k, v in seen.items() if k.endswith("_validated") or k.endswith("_validated")}
        assert flags, "gate was not evaluated"
        assert not any(flags.values()), f"stale fast-path flags reached the gate: {flags}"


class TestT1PostConcludeNeverRewritesVerdict:
    """Capture/publish failures after a durable conclusion must not FAIL the run."""

    @pytest.fixture(autouse=True)
    def _plain_logger(self, monkeypatch):
        import logging

        from investigation_agent_platform.application.worker.workflows import (
            run_investigation as mod,
        )

        # workflow.logger needs a workflow event loop; use a stdlib logger.
        monkeypatch.setattr(mod.workflow, "logger", logging.getLogger("t1-test"))

    @staticmethod
    def _wf():
        from investigation_agent_platform.application.worker.workflows import (
            run_investigation as mod,
        )

        return mod, mod.RunInvestigationWorkflow()

    @pytest.mark.asyncio
    async def test_post_conclude_step_failure_is_swallowed(self, monkeypatch):
        mod, wf = self._wf()
        calls: list[str] = []

        async def _boom(fn, inp, **kwargs):
            calls.append(getattr(fn, "__name__", "?"))
            raise RuntimeError("outbox down")

        monkeypatch.setattr(mod.workflow, "execute_activity", _boom)
        ok = await wf._post_conclude_best_effort(
            lambda: None, object(), mod.timedelta(seconds=1), mod.RetryPolicy(), "publish_event"
        )
        assert ok is False
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_cancellation_still_propagates(self, monkeypatch):
        import asyncio

        mod, wf = self._wf()

        async def _cancel(fn, inp, **kwargs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(mod.workflow, "execute_activity", _cancel)
        with pytest.raises(asyncio.CancelledError):
            await wf._post_conclude_best_effort(
                lambda: None, object(), mod.timedelta(seconds=1), mod.RetryPolicy(), "capture"
            )

    @pytest.mark.asyncio
    async def test_reconciler_skips_failed_transition_after_durable_conclusion(self, monkeypatch):
        mod, wf = self._wf()
        wf._conclusion_persisted = True
        wf._requested_final_status = "COMPLETED"
        transitions: list[str] = []

        async def _inner(self_, input_data):
            raise RuntimeError("late failure")

        async def _exec(fn, inp, **kwargs):
            transitions.append(getattr(inp, "target_status", "?"))

        monkeypatch.setattr(mod.RunInvestigationWorkflow, "_run_inner", _inner)
        monkeypatch.setattr(mod.workflow, "execute_activity", _exec)
        result = await wf.run(
            mod.RunInvestigationInput(
                tenant_id="t", investigation_id=str(uuid4()), application_id="app"
            )
        )
        assert transitions == [], "a published verdict was transitioned"
        assert result.status == "COMPLETED"
        assert result.error == "post_conclude_error:RuntimeError"

    @pytest.mark.asyncio
    async def test_reconciler_still_fails_run_before_conclusion(self, monkeypatch):
        mod, wf = self._wf()
        transitions: list[str] = []

        async def _inner(self_, input_data):
            raise RuntimeError("mid-run failure")

        async def _exec(fn, inp, **kwargs):
            transitions.append(getattr(inp, "target_status", "?"))

        monkeypatch.setattr(mod.RunInvestigationWorkflow, "_run_inner", _inner)
        monkeypatch.setattr(mod.workflow, "execute_activity", _exec)
        result = await wf.run(
            mod.RunInvestigationInput(
                tenant_id="t", investigation_id=str(uuid4()), application_id="app"
            )
        )
        assert transitions == ["FAILED"]
        assert result.status == "FAILED"


class TestR7AuthorizationFaultIsNotADenial:
    """Only a policy denial degrades to BLOCKED; infra faults raise for retry."""

    @staticmethod
    def _ctx(authorize):
        from investigation_agent_platform.api.dependencies import AppContext, set_app_context

        ctx = AppContext()

        async def _find(tenant_id, investigation_id):
            return []

        async def _count(tenant_id, investigation_id):
            return 0

        service = SimpleNamespace(authorize=authorize, execute=AsyncMock(return_value=None))
        ctx.execute_action_service = lambda: service  # type: ignore[method-assign]
        ctx.action_repo = SimpleNamespace(count_by_investigation=_count)
        ctx.evidence_repo = SimpleNamespace(
            find_by_investigation_id=_find, save_batch=AsyncMock(return_value=None)
        )
        ctx.profile_repo = SimpleNamespace(get_by_application_id=AsyncMock(return_value=None))
        ctx.evidence_gateway = SimpleNamespace()
        set_app_context(ctx)

    @staticmethod
    def _input():
        from investigation_agent_platform.application.worker.activities import ExecuteActionInput

        return ExecuteActionInput(
            tenant_id="tenant-a",
            investigation_id=str(uuid4()),
            action="SEARCH_LOGS",
            parameters={"keywords": [f"kw-{uuid4().hex[:8]}"], "limit": 5},
        )

    @pytest.mark.asyncio
    async def test_policy_denial_still_degrades_to_blocked(self):
        from investigation_agent_platform.application.worker.activities import (
            execute_action_activity,
        )
        from investigation_agent_platform.domain.common.exceptions import (
            SecurityPolicyViolationException,
        )

        self._ctx(AsyncMock(side_effect=SecurityPolicyViolationException("denied")))
        result = await execute_action_activity(self._input())
        assert result.success is True
        assert result.data["status"] == "BLOCKED"
        assert result.data["action_outcome"]["miss_reason"] == "authorization_denied"

    @pytest.mark.asyncio
    async def test_connection_fault_raises_retryable(self):
        from temporalio.exceptions import ApplicationError

        from investigation_agent_platform.application.worker.activities import (
            execute_action_activity,
        )

        self._ctx(AsyncMock(side_effect=ConnectionError("profile store unreachable")))
        with pytest.raises(ApplicationError) as info:
            await execute_action_activity(self._input())
        assert info.value.non_retryable is False

    @pytest.mark.asyncio
    async def test_programming_error_raises_non_retryable(self):
        from temporalio.exceptions import ApplicationError

        from investigation_agent_platform.application.worker.activities import (
            execute_action_activity,
        )

        self._ctx(AsyncMock(side_effect=TypeError("bad arg")))
        with pytest.raises(ApplicationError) as info:
            await execute_action_activity(self._input())
        assert info.value.non_retryable is True
