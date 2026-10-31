# tests/unit/test_h13_finding_type.py
"""H13: caller-edge answers publish ANSWERED with an OBSERVATION Finding (LV_H); metrics count ANSWERED."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import test_claim_binding_phase1 as fixtures
from test_claim_binding_phase1 import _activity_ctx, _attempt_event, _two_caller_hyp, _two_caller_rows

from investigation_agent_platform.application.worker.activities.lifecycle import (
    _answered_shape,
    _gate_conclusion_or_fail,
)


async def _conclude(metadata, hyp, rows, select="save_conclusion"):  # type: ignore[no-untyped-def]
    inv_id = uuid4()
    hyp = hyp.model_copy(update={"investigation_id": inv_id, "tenant_id": "t"})
    captured: dict = {}

    async def _save_conclusion(tenant_id, conclusion):  # type: ignore[no-untyped-def]
        captured["conclusion"] = conclusion

    async def _save_finding(tenant_id, finding):  # type: ignore[no-untyped-def]
        captured.setdefault("findings", []).append(finding)

    async def _noop(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    ctx = fixtures._activity_ctx(metadata, hyp, rows, [_attempt_event([str(r.evidence_id) for r in rows])])
    ctx.finding_repo = SimpleNamespace(
        save_conclusion=_save_conclusion, save=_noop, save_finding=_noop,
        save_finding_record=_save_finding,
    )
    status = await _gate_conclusion_or_fail("t", inv_id, ctx, shadow_notes=None, triage_route="")
    return status, captured


@pytest.mark.asyncio
async def test_caller_edge_status_under_lv5() -> None:
    row_a, row_b = _two_caller_rows()
    hyp = _two_caller_hyp(row_a, row_b)
    status, captured = await _conclude(
        {"claim_binding_version": "cbv2", "verifier_version": "v2", "loop_version": "lv5"},
        hyp, [row_a, row_b],
    )
    conclusion = captured.get("conclusion")
    assert conclusion is not None
    from investigation_agent_platform.domain.finding.models import ConclusionStatus

    assert conclusion.status == ConclusionStatus.ANSWERED, conclusion.status


@pytest.mark.asyncio
async def test_caller_edge_status_under_lv4() -> None:
    row_a, row_b = _two_caller_rows()
    hyp = _two_caller_hyp(row_a, row_b)
    status, captured = await _conclude(
        {"claim_binding_version": "cbv2", "verifier_version": "v2", "loop_version": "lv4"},
        hyp, [row_a, row_b],
    )
    conclusion = captured.get("conclusion")
    assert conclusion is not None
    from investigation_agent_platform.domain.finding.models import ConclusionStatus

    assert conclusion.status in (
        ConclusionStatus.ROOT_CAUSE_ESTABLISHED, ConclusionStatus.ROOT_CAUSE_LIKELY,
    ), conclusion.status


@pytest.mark.asyncio
async def test_caller_edge_finding_is_observation_under_lv_h() -> None:
    from investigation_agent_platform.domain.finding.models import FindingType

    row_a, row_b = _two_caller_rows()
    hyp = _two_caller_hyp(row_a, row_b)
    _, captured = await _conclude(
        {"claim_binding_version": "cbv2", "verifier_version": "v2", "loop_version": "lv10"},
        hyp, [row_a, row_b],
    )
    findings = captured.get("findings", [])
    assert findings, "a caller-edge ANSWERED run persists a Finding"
    assert all(f.finding_type == FindingType.OBSERVATION for f in findings)


@pytest.mark.asyncio
async def test_older_pin_keeps_root_cause_finding() -> None:
    from investigation_agent_platform.domain.finding.models import FindingType

    row_a, row_b = _two_caller_rows()
    hyp = _two_caller_hyp(row_a, row_b)
    _, captured = await _conclude(
        {"claim_binding_version": "cbv2", "verifier_version": "v2", "loop_version": "lv5"},
        hyp, [row_a, row_b],
    )
    findings = captured.get("findings", [])
    assert findings and all(f.finding_type == FindingType.ROOT_CAUSE for f in findings)


def test_status_and_finding_agree_for_every_shape() -> None:
    import itertools

    for bits in itertools.product([False, True], repeat=6):
        location, code_value, doc, history, state, caller = bits
        answered = _answered_shape(
            location=location, code_value=code_value, doc=doc, history=history,
            state=state, caller_edge=caller, extended=True,
        )
        assert answered == bool(location or code_value or doc or history or state or caller)


def test_calibration_counts_answered() -> None:
    sys.path.insert(0, "scripts")
    try:
        from agent_metrics import readiness_calibration
    finally:
        sys.path.pop(0)
    peaks = {"a": 0.9, "b": 0.9, "c": 0.9}
    statuses = {"a": "ROOT_CAUSE_ESTABLISHED", "b": "ANSWERED", "c": "INSUFFICIENT_EVIDENCE"}
    assert readiness_calibration(peaks, statuses) == (2, 1, 1)


def test_calibration_edge_cases() -> None:
    sys.path.insert(0, "scripts")
    try:
        from agent_metrics import readiness_calibration
    finally:
        sys.path.pop(0)
    assert readiness_calibration({"a": 0.9}, {"a": None}) == (0, 0, 1)
    assert readiness_calibration({"a": 0.5}, {"a": "INSUFFICIENT_EVIDENCE"}) == (0, 0, 0)
    assert readiness_calibration({}, {}) == (0, 0, 0)


@pytest.mark.asyncio
async def test_journey_fusion_with_caller_edge_observation() -> None:
    from unittest.mock import AsyncMock

    from investigation_agent_platform.application.worker.activities.execution import (
        _fuse_journey_findings,
    )
    from investigation_agent_platform.domain.evidence.models import EvidenceType
    from investigation_agent_platform.domain.finding.models import Finding, FindingType

    inv_id = uuid4()
    f1 = Finding(
        tenant_id="t", investigation_id=inv_id, finding_type=FindingType.OBSERVATION,
        title="t1", statement="caller edge observed", evidence_ids=[uuid4()],
        related_hypothesis_ids=[], confidence=0.8,
    )
    f2 = Finding(
        tenant_id="t", investigation_id=inv_id, finding_type=FindingType.OBSERVATION,
        title="t2", statement="execute-time observation", evidence_ids=[uuid4()],
        related_hypothesis_ids=[], confidence=0.7,
    )
    journey_row = SimpleNamespace(evidence_type=EvidenceType.JOURNEY)
    saved: list = []
    async def _save_finding_record(tenant_id, finding):  # type: ignore[no-untyped-def]
        saved.append(finding)

    finding_repo = SimpleNamespace(
        list_findings=AsyncMock(return_value=([f1, f2], 2)),
        save_finding_record=_save_finding_record,
    )
    evidence_repo = SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[journey_row]))
    await _fuse_journey_findings(
        tenant_id="t", investigation_id=inv_id, finding_repo=finding_repo,
        evidence_repo=evidence_repo,
    )
    assert len(saved) == 1
    assert saved[0].finding_type == FindingType.OBSERVATION
