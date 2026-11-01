"""T6-kernel: the kernel gate contradicts "shadow never blocks" (D14).

Shadow and off always allow, including on an internal fault; enforcing
fails closed on a fault (infrastructure blocker) and denies on an
evaluated DENY. An enforcing deny is applied at the approval point as an
abstention; an infrastructure fault fails FAILED with KERNEL_GATE_ERROR.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from investigation_agent_platform.application.proof import kernel_conclude_wiring as wiring


def _run(coro: Any) -> Any:
    return asyncio.new_event_loop().run_until_complete(coro)


class _Env:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
        self._monkeypatch = monkeypatch
        for key in (
            "IAP_KERNEL_WIRING",
            "IAP_KERNEL_POLICY_REPOSITORIES",
            "IAP_KERNEL_POLICY_AUTHORITIES",
        ):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)


async def _boom_rows(tenant_id: str, inv_id: object) -> Any:
    raise RuntimeError("repo down")


def test_t6k01_shadow_never_blocks_on_a_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    _Env(monkeypatch, IAP_KERNEL_WIRING="shadow")
    monkeypatch.setattr(
        wiring, "select_policy", lambda tenant_id: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    ctx = SimpleNamespace(
        evidence_repo=SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[]))
    )
    assert _run(wiring.kernel_gate_for_conclusion("t", uuid4(), ctx)) == (True, [])


def test_t6k02_off_never_blocks_on_a_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    _Env(monkeypatch)
    ctx = SimpleNamespace(evidence_repo=SimpleNamespace(find_by_investigation_id=_boom_rows))
    assert _run(wiring.kernel_gate_for_conclusion("t", uuid4(), ctx)) == (True, [])


def test_t6k03_enforcing_fails_closed_on_a_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    _Env(monkeypatch, IAP_KERNEL_WIRING="enforcing")
    monkeypatch.setattr(
        wiring, "select_policy", lambda tenant_id: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    ctx = SimpleNamespace(
        evidence_repo=SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[]))
    )
    allowed, blockers = _run(wiring.kernel_gate_for_conclusion("t", uuid4(), ctx))
    assert allowed is False
    assert blockers == ["kernel-gate-failure:RuntimeError"]


def test_t6k04_enforcing_evidence_load_failure_is_infrastructure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Env(monkeypatch, IAP_KERNEL_WIRING="enforcing")
    ctx = SimpleNamespace(evidence_repo=SimpleNamespace(find_by_investigation_id=_boom_rows))
    allowed, blockers = _run(wiring.kernel_gate_for_conclusion("t", uuid4(), ctx))
    assert allowed is False
    assert blockers == ["kernel-evidence-unavailable:RuntimeError"]
    assert wiring.kernel_blockers_are_infrastructure(blockers) is True


def test_t6k05_classifier_table() -> None:
    assert wiring.kernel_blockers_are_infrastructure(["kernel-denied:foo:bar"]) is False
    assert wiring.kernel_blockers_are_infrastructure(["kernel-no-policy"]) is True
    assert (
        wiring.kernel_blockers_are_infrastructure(["kernel-no-policy", "kernel-denied:x"])
        is False
    )
    assert wiring.kernel_blockers_are_infrastructure([]) is False


def test_t6k09_shadow_evaluates_and_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import test_kernel_conclude_wiring as kw

    _Env(
        monkeypatch,
        IAP_KERNEL_WIRING="shadow",
        IAP_KERNEL_POLICY_REPOSITORIES="tenant-repo",
    )
    row = kw._row()
    object.__setattr__(
        row,
        "attributes",
        {
            "code_fact_resolution_v2": {
                "status": "RESOLVED",
                "subject_identity": "handle",
                "revision": kw.REV,
                "declaration_location": {
                    "file_path": "src/handler.py",
                    "line_start": 10,
                    "line_end": 5,
                },
            }
        },
    )
    ctx = SimpleNamespace(
        evidence_repo=SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[row]))
    )
    with caplog.at_level("INFO", logger="investigation_agent_platform.application.proof.kernel_conclude_wiring"):
        allowed, blockers = _run(wiring.kernel_gate_for_conclusion("t", uuid4(), ctx))
    assert (allowed, blockers) == (True, [])
    assert "kernel wiring shadow:" in caplog.text


def _approvable_ctx() -> tuple[Any, dict[str, Any], Any]:
    from datetime import UTC, datetime

    from test_claim_binding_phase3 import _def_hyp, _def_read

    from investigation_agent_platform.api.dependencies import AppContext, set_app_context
    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationContext,
        InvestigationRequest,
        InvestigationStatus,
    )

    read = _def_read()
    hyp = _def_hyp(read).model_copy(update={"tenant_id": "t"})
    inv_id = uuid4()
    hyp = hyp.model_copy(update={"investigation_id": inv_id})
    saved: dict[str, Any] = {}
    found: dict[str, Any] = {"n": 0}

    async def _save_conclusion(tenant_id: str, conclusion: Any) -> Any:
        saved["conclusion"] = conclusion
        return conclusion

    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    async def _save_finding_record(tenant_id: str, finding: Any) -> None:
        found["n"] += 1

    now = datetime.now(UTC)
    inv = Investigation(
        session_id="sess-t6k",
        application_id="app",
        tenant_id="t",
        request=InvestigationRequest(
            application_id="app",
            problem_description="t6k",
            session_id="sess-t6k",
            requested_by="tester",
        ),
        context=InvestigationContext(environment="dev", time_window=(now, now)),
        status=InvestigationStatus.INVESTIGATING,
        metadata={"claim_binding_version": "cbv1", "verifier_version": "v2"},
    )
    inv = inv.model_copy(update={"id": inv_id})
    ctx = AppContext()
    ctx.investigation_repo = SimpleNamespace(
        get_by_id=AsyncMock(return_value=inv), save=AsyncMock()
    )
    ctx.evidence_repo = SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[read]))
    ctx.hypothesis_repo = SimpleNamespace(
        find_by_investigation_id=AsyncMock(return_value=[hyp]),
        save=AsyncMock(),
    )
    ctx.timeline_repo = SimpleNamespace(
        find_by_investigation_id=AsyncMock(return_value=[]),
        append=AsyncMock(return_value=None),
    )
    ctx.finding_repo = SimpleNamespace(
        save_conclusion=_save_conclusion,
        save=_noop,
        save_finding=_noop,
        save_finding_record=_save_finding_record,
    )
    set_app_context(ctx)
    return ctx, saved, found


def test_t6k06_enforcing_deny_becomes_an_abstention(monkeypatch: pytest.MonkeyPatch) -> None:
    from investigation_agent_platform.application.worker.activities import lifecycle
    from investigation_agent_platform.application.worker.activities.inputs import (
        ConcludeInvestigationInput,
    )
    from investigation_agent_platform.application.worker.activities.lifecycle import (
        conclude_investigation_activity,
    )
    from investigation_agent_platform.domain.finding.models import ConclusionStatus
    from investigation_agent_platform.domain.investigation.models import InvestigationStatus

    ctx, saved, found = _approvable_ctx()

    async def _deny(tenant_id: str, investigation_id: Any, ctx: Any) -> tuple[bool, list[str]]:
        return False, ["kernel-denied:SampleOrderFilter:no-commit"]

    monkeypatch.setattr(lifecycle, "kernel_gate_for_conclusion", _deny)
    inv_id = uuid4()
    result = _run(
        conclude_investigation_activity(
            ConcludeInvestigationInput(
                tenant_id="t", investigation_id=str(inv_id), status="COMPLETED"
            )
        )
    )
    assert result.success is True
    assert result.data.get("status") == InvestigationStatus.COMPLETED.value
    conclusion = saved.get("conclusion")
    assert conclusion is not None
    assert conclusion.status == ConclusionStatus.INSUFFICIENT_EVIDENCE
    assert any("KERNEL_DENIED:" in str(line) for line in (conclusion.limitations or []))
    assert found["n"] == 0


def test_t6k07_deny_does_not_hide_gate_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    import test_tranche6_verdict_semantics as t6v
    from test_claim_binding_phase3 import _def_read

    from investigation_agent_platform.application.worker.activities import lifecycle
    from investigation_agent_platform.application.worker.activities.inputs import (
        ConcludeInvestigationInput,
    )
    from investigation_agent_platform.application.worker.activities.lifecycle import (
        conclude_investigation_activity,
    )
    from investigation_agent_platform.domain.investigation.models import InvestigationStatus

    read = _def_read()
    hyp = t6v._mixed(read)
    inv_id = uuid4()
    hyp = hyp.model_copy(update={"tenant_id": "t", "investigation_id": inv_id})
    saved: dict[str, Any] = {}

    async def _save_conclusion(tenant_id: str, conclusion: Any) -> Any:
        saved["conclusion"] = conclusion
        return conclusion

    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    from datetime import UTC, datetime

    from investigation_agent_platform.api.dependencies import AppContext, set_app_context
    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationContext,
        InvestigationRequest,
    )

    now = datetime.now(UTC)
    inv = Investigation(
        session_id="sess-t6k7",
        application_id="app",
        tenant_id="t",
        request=InvestigationRequest(
            application_id="app",
            problem_description="t6k7",
            session_id="sess-t6k7",
            requested_by="tester",
        ),
        context=InvestigationContext(environment="dev", time_window=(now, now)),
        status=InvestigationStatus.INVESTIGATING,
        metadata={"claim_binding_version": "cbv1", "verifier_version": "v2"},
    )
    inv = inv.model_copy(update={"id": inv_id})
    ctx = AppContext()
    ctx.investigation_repo = SimpleNamespace(
        get_by_id=AsyncMock(return_value=inv), save=AsyncMock()
    )
    ctx.evidence_repo = SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[read]))
    ctx.hypothesis_repo = SimpleNamespace(
        find_by_investigation_id=AsyncMock(return_value=[hyp]),
        save=AsyncMock(),
    )
    ctx.timeline_repo = SimpleNamespace(
        find_by_investigation_id=AsyncMock(return_value=[]),
        append=AsyncMock(return_value=None),
    )
    ctx.finding_repo = SimpleNamespace(
        save_conclusion=_save_conclusion,
        save=_noop,
        save_finding=_noop,
        save_finding_record=_noop,
    )
    set_app_context(ctx)

    async def _deny(tenant_id: str, investigation_id: Any, ctx: Any) -> tuple[bool, list[str]]:
        return False, ["kernel-denied:SampleOrderFilter:no-commit"]

    monkeypatch.setattr(lifecycle, "kernel_gate_for_conclusion", _deny)
    result = _run(
        conclude_investigation_activity(
            ConcludeInvestigationInput(
                tenant_id="t", investigation_id=str(inv_id), status="COMPLETED"
            )
        )
    )
    assert result.success is True
    assert result.data.get("status") == InvestigationStatus.COMPLETED.value
    conclusion = saved.get("conclusion")
    assert conclusion is not None
    text = " ".join(str(line) for line in (conclusion.limitations or []))
    assert "KERNEL_DENIED" not in text
    assert text != ""


def test_t6k08_enforcing_infrastructure_fault_fails_with_distinct_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from investigation_agent_platform.api.dependencies import AppContext, set_app_context
    from investigation_agent_platform.application.worker.activities import lifecycle
    from investigation_agent_platform.application.worker.activities.inputs import (
        ConcludeInvestigationInput,
        _failure_code_for,
    )
    from investigation_agent_platform.application.worker.activities.lifecycle import (
        conclude_investigation_activity,
    )
    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationContext,
        InvestigationRequest,
        InvestigationStatus,
    )

    assert _failure_code_for("kernel_gate_unavailable: kernel-no-policy", True) == (
        "KERNEL_GATE_ERROR"
    )

    stamped: dict[str, Any] = {}
    saved_conclusions: list[Any] = []

    async def _save(tenant_id: str, inv: Any, expected_version: Any = 0, **kw: Any) -> Any:
        stamped["inv"] = inv
        return inv

    async def _save_conclusion(tenant_id: str, conclusion: Any) -> Any:
        saved_conclusions.append(conclusion)
        return conclusion

    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    now = datetime.now(UTC)
    inv = Investigation(
        session_id="sess-t6k",
        application_id="app",
        tenant_id="t",
        request=InvestigationRequest(
            application_id="app",
            problem_description="t6k",
            session_id="sess-t6k",
            requested_by="tester",
        ),
        context=InvestigationContext(environment="dev", time_window=(now, now)),
        status=InvestigationStatus.INVESTIGATING,
        metadata={},
    )
    ctx = AppContext()
    ctx.investigation_repo = SimpleNamespace(
        get_by_id=AsyncMock(return_value=inv), save=_save
    )
    ctx.evidence_repo = SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[]))
    ctx.hypothesis_repo = SimpleNamespace(
        find_by_investigation_id=AsyncMock(return_value=[]), save=AsyncMock()
    )
    ctx.finding_repo = SimpleNamespace(
        save_conclusion=AsyncMock(side_effect=_save_conclusion),
        save_finding=AsyncMock(return_value=None),
        save_finding_record=AsyncMock(return_value=None),
    )
    ctx.timeline_repo = SimpleNamespace(
        find_by_investigation_id=AsyncMock(return_value=[]),
        append=AsyncMock(return_value=None),
    )
    set_app_context(ctx)

    async def _infra(tenant_id: str, investigation_id: Any, ctx: Any) -> tuple[bool, list[str]]:
        return False, ["kernel-no-policy"]

    monkeypatch.setattr(lifecycle, "kernel_gate_for_conclusion", _infra)
    result = _run(
        conclude_investigation_activity(
            ConcludeInvestigationInput(
                tenant_id="t", investigation_id=str(inv.id), status="COMPLETED"
            )
        )
    )
    assert result.success is True
    assert result.data.get("status") == InvestigationStatus.FAILED.value
    assert saved_conclusions == []
    failure = (stamped.get("inv") is not None and stamped["inv"].metadata.get("failure")) or {}
    assert failure.get("code") == "KERNEL_GATE_ERROR"


def test_t6k12_no_in_function_kernel_import() -> None:
    from pathlib import Path

    from investigation_agent_platform.application.worker.activities import lifecycle

    src = Path(lifecycle.__file__).read_text(encoding="utf-8")
    assert "kernel-gate-import-failed" not in src
