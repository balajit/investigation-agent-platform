# tests/unit/test_h6_resample_outcome.py
"""H6: resample outcomes are stamped and counted (LV_H measurement, request text unchanged)."""

import pytest
from test_h6_varied_resample import _coord, _resp, _schema_invalid, _valid


@pytest.mark.asyncio
async def test_recovered_outcome_stamped() -> None:
    coord, state, _ = _coord([_resp(_schema_invalid()), _resp(_valid())])
    decision = await coord.reason("tenant-a", state)
    assert decision.metadata["resample_count"] == 1
    assert decision.metadata["resample_outcome"] == "recovered"


@pytest.mark.asyncio
async def test_exhausted_outcome_stamped() -> None:
    coord, state, _ = _coord([_resp(_schema_invalid()), _resp(_schema_invalid()), _resp(_schema_invalid())])
    decision = await coord.reason("tenant-a", state)
    assert decision.metadata["resample_count"] == 2
    assert decision.metadata["resample_outcome"] == "exhausted"


@pytest.mark.asyncio
async def test_no_resample_leaves_no_outcome() -> None:
    coord, state, _ = _coord([_resp(_valid())])
    decision = await coord.reason("tenant-a", state)
    assert "resample_count" not in decision.metadata
    assert "resample_outcome" not in decision.metadata


def test_workflow_sums_outcomes() -> None:
    from investigation_agent_platform.application.worker.workflows.run_investigation import (
        accumulate_resample_totals,
    )

    totals = {"attempts": 0, "recovered": 0, "exhausted": 0}
    accumulate_resample_totals(totals, {"resample": {"count": 1, "outcome": "recovered"}})
    accumulate_resample_totals(totals, {"resample": {"count": 2, "outcome": "exhausted"}})
    accumulate_resample_totals(totals, {})
    assert (totals["attempts"], totals["recovered"], totals["exhausted"]) == (3, 1, 1)
    accumulate_resample_totals(totals, None)
    accumulate_resample_totals({"attempts": "x"}, {"resample": {"count": -1}})
    assert totals["attempts"] == 3


def test_telemetry_keeps_outcome_totals() -> None:
    from investigation_agent_platform.application.worker.activities.inputs import (
        _derive_loop_telemetry,
    )

    out = _derive_loop_telemetry(
        {"resample_attempts_total": 4, "resample_recovered_total": 3, "resample_exhausted_total": 1},
        {}, 0,
    )
    assert out is not None
    assert out["resample_attempts_total"] == 4
    assert out["resample_recovered_total"] == 3
    assert out["resample_exhausted_total"] == 1


def test_passive_rate_query_needs_owner_approval() -> None:
    pytest.skip("read-only live-DB tier: requires owner approval to run against any database")


def test_live_hint_ab_needs_owner_approval() -> None:
    pytest.skip("live A/B: spends live model calls; requires owner approval")
