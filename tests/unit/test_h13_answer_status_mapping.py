# tests/unit/test_h13_answer_status_mapping.py
"""H13: history, state and caller-edge answers publish ANSWERED, not a root cause.

The ConclusionStatus contract says ANSWERED covers "location answers (where-defined,
caller listings, histories) backed by validated code citations - not causal claims".
The conclude path only honoured that for location, code-value and doc answers, so a
verified history, state or caller-edge answer shipped as ROOT_CAUSE_ESTABLISHED (or
LIKELY). lv5 maps every answer shape to ANSWERED; lv4-and-older pins keep the old status.

Also: ROOT_CAUSE_LIKELY now carries the same evidence invariant as ESTABLISHED.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_history_absence import _hyp as _history_hyp
from test_history_absence import _row as _history_row
from test_history_absence import _target as _history_target

from investigation_agent_platform.application.worker.activities.lifecycle import (
    _answered_shape,
)
from investigation_agent_platform.domain.finding.models import (
    ConclusionStatus,
    InvestigationConclusion,
)
from investigation_agent_platform.domain.investigation.loop_version import (
    LOOP_VERSION_CURRENT,
    loop_version_at_least,
)

# --- the pure mapping ---------------------------------------------------------------------------

_FLAGS = ("location", "code_value", "doc", "history", "state", "caller_edge")


def _shape(lv5: bool, **on: bool) -> bool:
    return _answered_shape(
        location=on.get("location", False),
        code_value=on.get("code_value", False),
        doc=on.get("doc", False),
        history=on.get("history", False),
        state=on.get("state", False),
        caller_edge=on.get("caller_edge", False),
        extended=lv5,
    )


@pytest.mark.parametrize("flag", ["location", "code_value", "doc"])
def test_the_original_answer_shapes_are_answered_under_every_pin(flag: str) -> None:
    assert _shape(False, **{flag: True}) is True
    assert _shape(True, **{flag: True}) is True


@pytest.mark.parametrize("flag", ["history", "state", "caller_edge"])
def test_the_extended_shapes_are_answered_only_from_lv5(flag: str) -> None:
    assert _shape(False, **{flag: True}) is False, "an older pin keeps the old status"
    assert _shape(True, **{flag: True}) is True


def test_no_answer_shape_is_never_answered() -> None:
    assert _shape(False) is False
    assert _shape(True) is False


# --- the loop version ---------------------------------------------------------------------------


def test_lv5_is_current_and_includes_lv4() -> None:
    assert LOOP_VERSION_CURRENT == "lv5"
    assert loop_version_at_least({"loop_version": "lv5"}, "lv5") is True
    assert loop_version_at_least({}, "lv5") is True
    assert loop_version_at_least({"loop_version": "lv4"}, "lv5") is False
    assert loop_version_at_least({"loop_version": "lv5"}, "lv4") is True


# --- the evidence invariant ---------------------------------------------------------------------


def test_a_likely_conclusion_requires_a_statement_and_evidence() -> None:
    with pytest.raises(ValidationError):
        InvestigationConclusion(
            tenant_id="t",
            investigation_id=uuid4(),
            status=ConclusionStatus.ROOT_CAUSE_LIKELY,
            root_cause="a cause",
            supporting_evidence_ids=[],
        )
    with pytest.raises(ValidationError):
        InvestigationConclusion(
            tenant_id="t",
            investigation_id=uuid4(),
            status=ConclusionStatus.ROOT_CAUSE_LIKELY,
            root_cause="",
            supporting_evidence_ids=[uuid4()],
        )


def test_a_likely_conclusion_with_evidence_is_valid() -> None:
    conclusion = InvestigationConclusion(
        tenant_id="t",
        investigation_id=uuid4(),
        status=ConclusionStatus.ROOT_CAUSE_LIKELY,
        root_cause="a cause",
        supporting_evidence_ids=[uuid4()],
    )
    assert conclusion.status == ConclusionStatus.ROOT_CAUSE_LIKELY


@pytest.mark.parametrize(
    "status",
    [ConclusionStatus.INSUFFICIENT_EVIDENCE, ConclusionStatus.NO_ROOT_CAUSE_FOUND],
)
def test_abstentions_stay_unconstrained(status) -> None:
    InvestigationConclusion(
        tenant_id="t", investigation_id=uuid4(), status=status, supporting_evidence_ids=[]
    )


# --- end to end: a verified history answer ---------------------------------------------------------


async def _conclude_history(metadata: dict):  # type: ignore[no-untyped-def]
    import test_claim_binding_phase1 as fixtures

    from investigation_agent_platform.application.worker.activities.lifecycle import (
        _gate_conclusion_or_fail,
    )

    row = _history_row()
    hyp = _history_hyp(_history_target(), row.evidence_id)
    inv_id = uuid4()
    hyp = hyp.model_copy(update={"investigation_id": inv_id, "tenant_id": "t"})
    captured: dict = {"conclusion": None}

    async def _save_conclusion(tenant_id, conclusion):  # type: ignore[no-untyped-def]
        captured["conclusion"] = conclusion

    async def _noop(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    ctx = fixtures._activity_ctx(metadata, hyp, [row], [])
    ctx.finding_repo = SimpleNamespace(
        save_conclusion=_save_conclusion,
        save=_noop,
        save_finding=_noop,
        save_finding_record=_noop,
    )
    await _gate_conclusion_or_fail("t", inv_id, ctx, shadow_notes=None, triage_route="")
    return captured["conclusion"]


@pytest.mark.asyncio
async def test_a_history_answer_publishes_answered_on_the_current_pin() -> None:
    conclusion = await _conclude_history(
        {"claim_binding_version": "cbv2", "verifier_version": "v2"}
    )
    assert conclusion is not None
    assert conclusion.status == ConclusionStatus.ANSWERED, conclusion.status


@pytest.mark.asyncio
async def test_a_history_answer_on_an_lv4_pin_keeps_the_old_status() -> None:
    conclusion = await _conclude_history(
        {"claim_binding_version": "cbv2", "verifier_version": "v2", "loop_version": "lv4"}
    )
    assert conclusion is not None
    assert conclusion.status in {
        ConclusionStatus.ROOT_CAUSE_ESTABLISHED,
        ConclusionStatus.ROOT_CAUSE_LIKELY,
    }, conclusion.status


@pytest.mark.asyncio
async def test_a_history_answer_is_never_marked_provisional_by_the_rival_bar() -> None:
    conclusion = await _conclude_history(
        {"claim_binding_version": "cbv2", "verifier_version": "v2"}
    )
    assert conclusion is not None
    assert conclusion.provisional is False
