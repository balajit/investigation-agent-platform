"""T4: stall exit — pre-override overconfidence read + two-turn break (LV_T)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from investigation_agent_platform.application.worker.workflows.forcing import (
    PostReasonCascadeState,
    apply_post_reason_cascade,
    dispatch_fingerprint,
    overconfident_turn,
    stall_step,
)
from investigation_agent_platform.domain.investigation.models import (
    InvestigationStatus,
)


def _state(**over):  # type: ignore[no-untyped-def]
    base = PostReasonCascadeState(
        reason_data={
            "next_action": "GET_CODE",
            "action_parameters": {"file_path": "svc/new.py"},
            "conclusion_readiness": 0.5,
        },
        action_history=[],
        evidence_banked_total=0,
        triage_route="BUG",
        description="where is the order filter implemented in svc/orders.py",
        knowledge_seed={},
        max_iterations=10,
        iteration=0,
        prev_focus=None,
        is_defer=False,
        d6_fired=False,
        absence_forced=False,
        counts={},
        formulated_count=0,
        tenant_id="tenant-a",
        investigation_id="inv-1",
    )
    for key, value in over.items():
        setattr(base, key, value)
    return base


def _shipped_false(reason_data):  # type: ignore[no-untyped-def]
    proposed = str((reason_data or {}).get("next_action", "") or "").upper()
    try:
        value = float((reason_data or {}).get("conclusion_readiness", 0.0) or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    return not (proposed in ("VERIFY_HYPOTHESIS", "CONCLUDE") or value >= 0.8)


def test_t401_pre_override_read_sees_a_forced_override() -> None:
    # Floor probe: CONCLUDE at 0.5 with nothing banked on a code-shaped
    # incident is floor-forced to a gather.
    floor = _state(
        reason_data={
            "next_action": "CONCLUDE",
            "conclusion_readiness": 0.5,
            "hypotheses_evaluated": [],
            "claims": [],
        },
    )
    out = apply_post_reason_cascade(floor)
    assert out.reason_data["next_action"] != "CONCLUDE"
    assert overconfident_turn({"next_action": "CONCLUDE"}, 0.5) is True
    assert _shipped_false(out.reason_data) is True

    # Formulate probe: VERIFY_HYPOTHESIS at 0.5, three rows banked, never
    # formulated, turn 2 -> forced FORMULATE_HYPOTHESIS.
    form = _state(
        reason_data={
            "next_action": "VERIFY_HYPOTHESIS",
            "conclusion_readiness": 0.5,
            "hypotheses_evaluated": [{"statement": "the handler calls the filter"}],
            "claims": [],
        },
        evidence_banked_total=3,
        iteration=2,
    )
    out = apply_post_reason_cascade(form)
    assert out.reason_data["next_action"] == "FORMULATE_HYPOTHESIS"
    assert overconfident_turn({"next_action": "VERIFY_HYPOTHESIS"}, 0.5) is True
    assert _shipped_false(out.reason_data) is True


def test_t402_overconfident_turn_table() -> None:
    assert overconfident_turn({"next_action": "VERIFY_HYPOTHESIS"}, 0.1) is True
    assert overconfident_turn({"next_action": "CONCLUDE"}, None) is True
    assert overconfident_turn({"next_action": "get_code"}, 0.79) is False
    assert overconfident_turn({"next_action": "GET_CODE"}, 0.8) is True
    assert overconfident_turn({"next_action": "GET_CODE"}, "x") is False
    assert overconfident_turn({"next_action": "GET_CODE"}, float("nan")) is False
    assert overconfident_turn(None, 0.9) is True
    assert overconfident_turn("not-a-dict", 0.9) is True
    assert overconfident_turn("not-a-dict", 0.1) is False
    assert overconfident_turn({"next_action": "VERIFY_HYPOTHESIS"}, None) is True


def test_t403_stall_step_extends_only_on_the_full_condition() -> None:
    assert stall_step(
        0, overconfident=True, rows_banked=0, outcome="EMPTY",
        fingerprint="ab", previous_fingerprint="ab",
    ) == (1, False)
    assert stall_step(
        0, overconfident=True, rows_banked=0, outcome="DEGRADED",
        fingerprint="ab", previous_fingerprint="zz",
    ) == (1, False)
    assert stall_step(
        0, overconfident=False, rows_banked=0, outcome="EMPTY",
        fingerprint="ab", previous_fingerprint="ab",
    ) == (0, False)
    assert stall_step(
        0, overconfident=True, rows_banked=1, outcome="EMPTY",
        fingerprint="ab", previous_fingerprint="ab",
    ) == (0, False)
    assert stall_step(
        0, overconfident=True, rows_banked=None, outcome="EMPTY",
        fingerprint="ab", previous_fingerprint="ab",
    ) == (0, False)
    assert stall_step(
        0, overconfident=True, rows_banked=0, outcome="SUCCEEDED",
        fingerprint="ab", previous_fingerprint="zz",
    ) == (0, False)


def test_t404_trips_on_the_second_consecutive_stall() -> None:
    kw = dict(overconfident=True, rows_banked=0, outcome="EMPTY",
              fingerprint="ab", previous_fingerprint="ab")
    assert stall_step(0, **kw) == (1, False)  # type: ignore[arg-type]
    assert stall_step(1, **kw) == (2, True)  # type: ignore[arg-type]


def test_t405_a_progress_turn_resets_the_streak() -> None:
    assert stall_step(
        1, overconfident=True, rows_banked=2, outcome="SUCCEEDED",
        fingerprint="cd", previous_fingerprint="ab",
    ) == (0, False)


def test_t406_dispatch_fingerprint_is_stable_and_param_sensitive() -> None:
    a = {"next_action": "GET_CODE", "action_parameters": {"file_path": "x.py", "start_line": 1}}
    b = {"action_parameters": {"start_line": 1, "file_path": "x.py"}, "next_action": "get_code"}
    c = {"next_action": "GET_CODE", "action_parameters": {"file_path": "y.py"}}
    assert dispatch_fingerprint(a) == dispatch_fingerprint(b)
    assert dispatch_fingerprint(a) != dispatch_fingerprint(c)
    assert dispatch_fingerprint("not-a-dict") == ""
    assert dispatch_fingerprint(None) == ""


def _epistemic_ctx():  # type: ignore[no-untyped-def]
    from investigation_agent_platform.api.dependencies import AppContext, set_app_context
    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationContext,
        InvestigationRequest,
    )

    saved: dict = {}

    async def _save_conclusion(tenant_id, conclusion):
        saved["conclusion"] = conclusion
        return conclusion

    from datetime import UTC, datetime

    now = datetime.now(UTC)
    inv = Investigation(
        session_id="sess-t4",
        application_id="app",
        tenant_id="t",
        request=InvestigationRequest(
            application_id="app",
            problem_description="t4",
            session_id="sess-t4",
            requested_by="tester",
        ),
        context=InvestigationContext(environment="dev", time_window=(now, now)),
        status=InvestigationStatus.INVESTIGATING,
        metadata={"loop_version": "lv13"},
    )
    ctx = AppContext()
    ctx.investigation_repo = SimpleNamespace(
        get_by_id=AsyncMock(return_value=inv), save=AsyncMock()
    )
    ctx.evidence_repo = SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[]))
    ctx.hypothesis_repo = SimpleNamespace(
        find_by_investigation_id=AsyncMock(return_value=[]), save=AsyncMock()
    )
    ctx.finding_repo = SimpleNamespace(
        save_conclusion=AsyncMock(side_effect=_save_conclusion),
        save_finding_record=AsyncMock(return_value=None),
    )
    ctx.timeline_repo = SimpleNamespace(
        find_by_investigation_id=AsyncMock(return_value=[]),
        append=AsyncMock(return_value=None),
    )
    set_app_context(ctx)
    return ctx, saved, inv


@pytest.mark.asyncio
async def test_t412_conclude_maps_the_stall_to_an_abstention() -> None:
    from investigation_agent_platform.application.worker.activities.inputs import (
        ConcludeInvestigationInput,
    )
    from investigation_agent_platform.application.worker.activities.lifecycle import (
        conclude_investigation_activity,
    )
    from investigation_agent_platform.domain.finding.models import (
        ConclusionStatus,
        HandoffState,
    )

    ctx, saved, inv = _epistemic_ctx()
    result = await conclude_investigation_activity(
        ConcludeInvestigationInput(
            tenant_id="t",
            investigation_id=str(inv.id),
            status="FAILED",
            reason="overconfident_no_progress",
        )
    )
    assert result.success is True
    assert result.data.get("status") == InvestigationStatus.COMPLETED.value
    conclusion = saved.get("conclusion")
    assert conclusion is not None
    assert conclusion.status == ConclusionStatus.INSUFFICIENT_EVIDENCE
    assert "Change the retrieval approach" in (conclusion.next_check or "")
    assert conclusion.handoff is not None
    assert conclusion.handoff.state == HandoffState.FRONTIER_EXHAUSTED


def test_t413_new_telemetry_keys_are_stamped() -> None:
    from investigation_agent_platform.application.worker.activities.inputs import (
        _derive_loop_telemetry,
    )

    out = _derive_loop_telemetry(
        {"stall_streak_max": 2, "stall_exits": 1, "overconfident_turns": 4, "iterations": 5},
        {},
        0,
    )
    assert out is not None
    assert out["stall_streak_max"] == 2
    assert out["stall_exits"] == 1
    assert out["overconfident_turns"] == 4


def test_t414_agent_metrics_reports_the_per_turn_rate() -> None:
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "scripts"))
    from agent_metrics import summarize_loop_telemetry

    rows = [
        SimpleNamespace(metadata_json={"loop_telemetry": {
            "iterations": 10, "overconfident_turns": 4, "stall_exits": 1}}),
        SimpleNamespace(metadata_json={"loop_telemetry": {
            "iterations": 5, "overconfident_turns": 0, "stall_exits": 0}}),
        SimpleNamespace(metadata_json={}),
        SimpleNamespace(metadata_json=None),
    ]
    assert summarize_loop_telemetry(rows) == {
        "overconfident_turns": 4, "turns": 15, "stall_exits": 1,
    }


def test_t417_exit_contract_names_the_stall_break() -> None:
    import ast
    from pathlib import Path

    from investigation_agent_platform.application.worker.workflows import (
        run_investigation,
    )

    src = Path(run_investigation.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = [
        (ast.get_source_segment(src, node.test) or "")
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and "_stalled" in (ast.get_source_segment(src, node.test) or "")
    ]
    assert found, "the stall break condition must name _stalled"
    from investigation_agent_platform.application.worker.workflows import forcing

    fsrc = Path(forcing.__file__).read_text(encoding="utf-8")
    assert "rows_banked == 0" in fsrc
