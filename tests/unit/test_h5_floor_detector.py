# tests/unit/test_h5_floor_detector.py
"""H5: floor measurement plumbing + reopen detector (LV_H measurement, no behavior change)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _nc_payload(statement, evidence_ids=None):  # type: ignore[no-untyped-def]
    return {
        "observations": [],
        "conclusion_readiness": 0.0,
        "hypotheses_evaluated": [statement],
        "claims": [{"statement": statement, "claim_kind": "NOT_CHECKABLE", "evidence_ids": list(evidence_ids or [])}],
    }


def _coord(parsed, metadata):  # type: ignore[no-untyped-def]
    from test_h5_checkability_floor import _coord as _base_coord

    return _base_coord(parsed, metadata)


@pytest.mark.asyncio
async def test_floor_metadata_counts_what_it_kept() -> None:
    coord, state = _coord(
        {
            "observations": [],
            "conclusion_readiness": 0.0,
            "hypotheses_evaluated": ["a", "b", "c"],
            "claims": [
                {"statement": "Connection pool exhaustion causes the timeout", "claim_kind": "NOT_CHECKABLE", "evidence_ids": []},
                {"statement": "OrderServiceHandler is elegant", "claim_kind": "NOT_CHECKABLE", "evidence_ids": []},
                {"statement": "The design is elegant", "claim_kind": "NOT_CHECKABLE", "evidence_ids": []},
            ],
        },
        {"loop_version": "lv10"},
    )
    decision = await coord.reason("tenant-a", state)
    floor = decision.metadata["checkability_floor"]
    assert floor["causal_mechanism"] == 1
    assert floor["nc_kept"] == 2
    assert floor["nc_kept_with_identifier"] == 1


@pytest.mark.asyncio
async def test_floor_metadata_written_when_nothing_retyped() -> None:
    coord, state = _coord(_nc_payload("OrderServiceHandler is elegant"), {"loop_version": "lv10"})
    decision = await coord.reason("tenant-a", state)
    assert decision.metadata["checkability_floor"] == {"nc_kept": 1, "nc_kept_with_identifier": 1}


@pytest.mark.asyncio
async def test_lv2_pin_gets_no_floor_metadata() -> None:
    coord, state = _coord(_nc_payload("OrderServiceHandler is elegant"), {"loop_version": "lv2"})
    decision = await coord.reason("tenant-a", state)
    assert "checkability_floor" not in decision.metadata


def test_identifier_detector_table() -> None:
    from investigation_agent_platform.application.investigation.reasoning import nc_names_identifier

    assert nc_names_identifier("OrderServiceHandler.validateOrder", {}) is True
    assert nc_names_identifier("PoolExhaustionError", {}) is True
    assert nc_names_identifier("see src/alpha.py", {}) is True
    assert nc_names_identifier("under src/", {}) is True
    assert nc_names_identifier("is elegant", {}) is False
    assert nc_names_identifier("", None) is False
    assert nc_names_identifier(None, None) is False
    assert nc_names_identifier("x", {"symbol": "Foo.a"}) is True
    assert nc_names_identifier("x", {"file_path": "svc/a.py"}) is True


@pytest.mark.asyncio
async def test_reason_activity_forwards_measurement() -> None:
    from datetime import UTC, datetime, timedelta

    from investigation_agent_platform.api.dependencies import AppContext, set_app_context
    from investigation_agent_platform.application.worker.activities import (
        ReasonInput,
        reason_activity,
    )
    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationContext,
        InvestigationRequest,
    )

    now = datetime.now(UTC)
    inv = Investigation(
        session_id="sess-1", application_id="app", tenant_id="t",
        request=InvestigationRequest(application_id="app", problem_description="p", session_id="sess-1", requested_by="tester"),
        context=InvestigationContext(environment="production", time_window=(now - timedelta(hours=1), now)),
    )
    decision = SimpleNamespace(
        action=None, hypotheses_evaluated=[], observations=[], information_gaps=[],
        claims=[], contradictions_found=[], conclusion_readiness=0.0, reasoning_chain="",
        disposition=None, defer_gathering=None, action_parameters={},
        metadata={"checkability_floor": {"nc_kept": 2}, "resample_count": 3, "resample_outcome": "recovered"},
    )
    ctx = AppContext()
    ctx.investigation_repo = SimpleNamespace(get_by_id=AsyncMock(return_value=inv))  # type: ignore[attr-defined]
    ctx.reasoning_coordinator = SimpleNamespace(reason=AsyncMock(return_value=decision))  # type: ignore[attr-defined]
    ctx.evidence_repo = SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[]))  # type: ignore[attr-defined]
    ctx.hypothesis_repo = SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=[]))  # type: ignore[attr-defined]
    set_app_context(ctx)
    out = await reason_activity(ReasonInput(tenant_id="t", investigation_id=str(inv.id), loop_version="lv10"))
    assert out.success is True
    assert out.data["checkability_floor"] == {"nc_kept": 2}
    assert out.data["resample"] == {"count": 3, "outcome": "recovered"}


def test_telemetry_keeps_dropped_and_new_counters() -> None:
    from investigation_agent_platform.application.worker.activities.inputs import (
        _derive_loop_telemetry,
    )

    passed = {
        "d6_blocked_no_evidence_count": 1, "fetches_dispatched": 2, "forced_pivot_count": 3,
        "forced_smallest_fetch_definition_count": 0,
        "checkability_floor_retyped_total": 1, "nc_kept_total": 2,
        "nc_kept_with_identifier_total": 1, "claims_total": 9,
        "resample_attempts_total": 4, "resample_recovered_total": 3, "resample_exhausted_total": 1,
    }
    out = _derive_loop_telemetry(passed, {}, 0)
    assert out is not None
    for key, value in passed.items():
        assert out.get(key) == value, key


def test_malformed_telemetry_values_dropped() -> None:
    from investigation_agent_platform.application.worker.activities.inputs import (
        _derive_loop_telemetry,
    )

    out = _derive_loop_telemetry(
        {"nc_kept_total": "x", "claims_total": -1, "resample_attempts_total": None, "fetches_dispatched": 2},
        {}, 0,
    )
    assert out is not None
    assert out.get("nc_kept_total", 0) == 0
    assert out.get("fetches_dispatched") == 2


def test_reopen_verdict_thresholds() -> None:
    import sys

    sys.path.insert(0, "scripts")
    try:
        from h5_floor_reopen_probe import reopen_verdict
    finally:
        sys.path.pop(0)

    def rows(nc, typed, verified, runs=40):  # type: ignore[no-untyped-def]
        out = []
        for i in range(runs):
            out.append({"nc_with_identifier": 1 if i < nc else 0, "typed": 1 if i < typed else 0,
                        "would_verify": 1 if i < verified else 0})
        return out

    assert reopen_verdict(rows(0, 40, 0))["reopen"] is False
    ok = reopen_verdict(rows(8, 40, 3), min_runs=30, min_rate=0.10, min_would_verify=0.30)
    assert ok["reopen"] is True and ok["runs"] == 40
    low_rate = reopen_verdict(rows(3, 40, 3), min_runs=30, min_rate=0.10, min_would_verify=0.30)
    assert low_rate["reopen"] is False
    low_wv = reopen_verdict(rows(8, 40, 1), min_runs=30, min_rate=0.10, min_would_verify=0.30)
    assert low_wv["reopen"] is False
    few = reopen_verdict(rows(8, 40, 3)[:20], min_runs=30, min_rate=0.10, min_would_verify=0.30)
    assert few["reopen"] is False
    assert reopen_verdict([], min_runs=0, min_rate=0.0, min_would_verify=0.0)["reopen"] is True


def test_would_verify_replay() -> None:
    import sys

    sys.path.insert(0, "scripts")
    try:
        from h5_floor_reopen_probe import would_verify_if_retyped
    finally:
        sys.path.pop(0)
    from test_claim_binding_phase3 import _def_hyp, _def_read

    read = _def_read()
    base = _def_hyp(read, statement="The handler is defined")
    assert would_verify_if_retyped(base, [read]) is False


def test_live_db_layer_needs_owner_approval() -> None:
    import pytest

    pytest.skip("read-only live-DB tier: requires owner approval to run against any database")  # no NC-with-identifier assessments
