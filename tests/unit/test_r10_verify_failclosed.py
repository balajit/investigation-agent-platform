"""R10 (c) + (h): a failed or absent durable read authorizes nothing; the real budget.

T08: timeline read failure at LV_R yields no directive. T09 amends the
existing fallback test in place (see test_g3_retrieval_benefit.py).
T10 pins the healthy merge. T19 pins the environment budget thread.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


def _failed_event(attempt_id, query="SampleOrderFilter", scope=None):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        event_type="RETRIEVAL_ATTEMPT",
        attributes={
            "attempt_id": attempt_id,
            "action_type": "SEARCH_CODE",
            "evidence_ids": [],
            "attempt": {
                "search_query": query,
                "scope": scope,
                "outcome": "SEARCH_FAILED",
                "action_type": "SEARCH_CODE",
            },
        },
    )


def _harness(*, inv_metadata, timeline_events, recent):  # type: ignore[no-untyped-def]
    from unittest.mock import AsyncMock

    from investigation_agent_platform.api.dependencies import AppContext, set_app_context

    ctx = AppContext()

    async def _get(tenant_id: str, investigation_id: object) -> object:
        return SimpleNamespace(metadata=dict(inv_metadata))

    ctx.investigation_repo = SimpleNamespace(get_by_id=_get)  # type: ignore[attr-defined]
    ctx.evidence_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(return_value=[]),
    )
    return ctx, set_app_context


def _run_verify(inv_id, recent):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.application.worker.activities import (
        VerifyRootCauseInput,
        verify_root_cause_activity,
    )

    return verify_root_cause_activity(
        VerifyRootCauseInput(
            tenant_id="t",
            investigation_id=str(inv_id),
            recent_actions=list(recent),
        )
    )


def _claim_hypothesis(inv_id):  # type: ignore[no-untyped-def]
    from investigation_agent_platform.domain.hypothesis.models import (
        AssessmentStrength,
        AssessmentType,
        Hypothesis,
        HypothesisEvidenceAssessment,
        HypothesisStatus,
    )

    missing = uuid4()
    return Hypothesis(
        tenant_id="t",
        investigation_id=inv_id,
        title="SampleOrderFilter",
        statement="SampleOrderFilter",
        description="SampleOrderFilter",
        status=HypothesisStatus.SUPPORTED,
        supporting_evidence_ids=[],
        assessments=[
            HypothesisEvidenceAssessment(
                hypothesis_id=uuid4(),
                evidence_id=missing,
                assessment=AssessmentType.NEUTRAL,
                strength=AssessmentStrength.WEAK,
                reason="unresolved citation",
            )
        ],
    )


def _recent_search_code():  # type: ignore[no-untyped-def]
    return [
        {
            "iteration": 0,
            "action": "SEARCH_CODE",
            "parameters": {"path_prefix": "/"},
            "attempt_id": "abc123",
            "evidence_ids": [],
            "retrieval_attempt": {
                "search_query": "SampleOrderFilter",
                "scope": None,
                "outcome": "SEARCH_FAILED",
            },
        }
    ]


@pytest.mark.asyncio
async def test_r10_t08_timeline_read_failure_authorizes_nothing() -> None:
    from unittest.mock import AsyncMock

    from investigation_agent_platform.api.dependencies import get_app_context

    inv_id = uuid4()
    ctx, _ = _harness(inv_metadata={}, timeline_events=[], recent=[])
    from investigation_agent_platform.domain.hypothesis.models import (
        Hypothesis,
    )

    ctx.hypothesis_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(
            return_value=[_claim_hypothesis(inv_id)]
        ),
        save=AsyncMock(),
    )

    async def _raise(tenant_id, investigation_id):  # type: ignore[no-untyped-def]
        raise RuntimeError("timeline store down")

    ctx.timeline_repo = SimpleNamespace(find_by_investigation_id=_raise)  # type: ignore[attr-defined]
    from investigation_agent_platform.api.dependencies import set_app_context as _set

    _set(ctx)
    assert isinstance(_claim_hypothesis(inv_id), Hypothesis)
    result = await _run_verify(inv_id, _recent_search_code() * 12)
    assert result.success is True
    assert result.data["ledger_requery"] is None
    assert get_app_context() is ctx


@pytest.mark.asyncio
async def test_r10_t10_healthy_timeline_merges_with_summaries() -> None:
    from unittest.mock import AsyncMock

    from investigation_agent_platform.api.dependencies import set_app_context as _set
    from investigation_agent_platform.application.evidence.retrieval_ledger import (
        merge_recorded_attempts,
        recorded_attempt_summaries,
        recorded_retrieval_attempts,
    )

    inv_id = uuid4()
    ctx, _ = _harness(inv_metadata={}, timeline_events=[], recent=[])
    ctx.hypothesis_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(
            return_value=[_claim_hypothesis(inv_id)]
        ),
        save=AsyncMock(),
    )
    ctx.timeline_repo = SimpleNamespace(  # type: ignore[attr-defined]
        find_by_investigation_id=AsyncMock(
            return_value=[_failed_event("abc123")]
        ),
    )
    _set(ctx)
    summaries = recorded_attempt_summaries(_recent_search_code())
    assert summaries is not None
    merged = merge_recorded_attempts(
        recorded_retrieval_attempts([_failed_event("abc123")]), summaries
    )
    assert len(merged) == 1
    result = await _run_verify(inv_id, _recent_search_code())
    assert result.success is True
    assert result.data["ledger_requery"] is not None


@pytest.mark.asyncio
async def test_r10_t19_environment_budget_is_honored(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from investigation_agent_platform.api.dependencies import set_app_context as _set
    from investigation_agent_platform.domain.hypothesis.models import (
        AssessmentStrength,
        AssessmentType,
        ClaimKind,
        ClaimTarget,
        Hypothesis,
        HypothesisEvidenceAssessment,
        HypothesisStatus,
    )

    def _stored_hyp(inv_id):  # type: ignore[no-untyped-def]
        return Hypothesis(
            tenant_id="t",
            investigation_id=inv_id,
            title="SampleOrderFilter",
            statement="SampleOrderFilter",
            description="SampleOrderFilter",
            status=HypothesisStatus.SUPPORTED,
            supporting_evidence_ids=[],
            assessments=[
                HypothesisEvidenceAssessment(
                    hypothesis_id=uuid4(),
                    evidence_id=None,
                    assessment=AssessmentType.SUPPORTS,
                    strength=AssessmentStrength.MODERATE,
                    reason="claim unresolved: no usable evidence",
                    claim_kind=ClaimKind.DEFINITION,
                    claim_target=ClaimTarget(symbol="SampleOrderFilter"),
                    search_query="SampleOrderFilter",
                    search_scope="s",
                    reason_code="CLAIM_UNRESOLVED",
                    schema_version="v2",
                )
            ],
        )

    def _recent():  # type: ignore[no-untyped-def]
        return [
            {
                "iteration": 0,
                "action": "SEARCH_CODE",
                "parameters": {"query": "SampleOrderFilter", "scope": "s"},
                "attempt_id": "sum-0",
                "evidence_ids": [],
                "retrieval_attempt": {
                    "search_query": "SampleOrderFilter",
                    "scope": "s",
                    "outcome": "SEARCH_FAILED",
                },
            }
        ]

    async def _three_attempts_case(env_value, monkeypatch):  # type: ignore[no-untyped-def]
        if env_value is None:
            monkeypatch.delenv("IAP_WP_A3_MAX_ATTEMPTS", raising=False)
        else:
            monkeypatch.setenv("IAP_WP_A3_MAX_ATTEMPTS", env_value)
        inv_id = uuid4()
        ctx, _ = _harness(inv_metadata={}, timeline_events=[], recent=[])
        ctx.hypothesis_repo = SimpleNamespace(  # type: ignore[attr-defined]
            find_by_investigation_id=AsyncMock(
                return_value=[_stored_hyp(inv_id)]
            ),
            save=AsyncMock(),
        )
        ctx.timeline_repo = SimpleNamespace(  # type: ignore[attr-defined]
            find_by_investigation_id=AsyncMock(
                return_value=[
                    SimpleNamespace(
                        event_type="RETRIEVAL_ATTEMPT",
                        attributes={
                            "attempt_id": f"env-{i}",
                            "action_type": "SEARCH_CODE",
                            "evidence_ids": [],
                            "attempt": {
                                "search_query": "SampleOrderFilter",
                                "scope": "s",
                                "outcome": "SEARCH_FAILED",
                                "action_type": "SEARCH_CODE",
                            },
                        },
                    )
                    for i in range(3)
                ]
            ),
        )
        _set(ctx)
        result = await _run_verify(inv_id, _recent())
        assert result.success is True
        return result.data["ledger_requery"]

    # Three persisted attempts plus the same-turn summary make four
    # durable: a directive is still issued at a budget of 5, stops at 4,
    # and the default budget of 3 stays stopped.
    assert await _three_attempts_case("5", monkeypatch) is not None
    assert await _three_attempts_case("4", monkeypatch) is None
    assert await _three_attempts_case(None, monkeypatch) is None
