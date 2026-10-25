# tests/unit/test_reason_requery.py
"""SWE-agent loop §2+§3: bounded hidden requery + one-action cardinality."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from investigation_agent_platform.application.investigation.reasoning import (
    ReasoningCoordinator,
)
from investigation_agent_platform.domain.investigation.models import (
    Investigation,
    InvestigationContext,
    InvestigationRequest,
    InvestigationState,
)
from investigation_agent_platform.ports.reasoning.llm_gateway import LLMGatewayResponse


def _state():
    now = datetime.now(UTC)
    inv = Investigation(
        session_id="sess-1",
        application_id="app",
        tenant_id="tenant-a",
        request=InvestigationRequest(
            application_id="app",
            problem_description="test incident",
            session_id="sess-1",
            requested_by="tester",
        ),
        context=InvestigationContext(
            environment="production",
            time_window=(now - timedelta(seconds=3600), now),
        ),
    )
    return InvestigationState(investigation=inv)


def _resp(parsed):
    from investigation_agent_platform.ports.observability.telemetry import (
        LLMCallMetadata,
    )

    return LLMGatewayResponse(
        content="{}",
        parsed=parsed,
        metadata=LLMCallMetadata(
            model_name="test",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            estimated_cost_usd=0.0,
            latency_ms=1.0,
        ),
    )


def _coord(script):
    gateway = MagicMock()
    gateway.complete = AsyncMock(side_effect=[_resp(p) for p in script])
    safety = MagicMock()
    safety.validate_prompt_safety = AsyncMock(return_value=True)
    return ReasoningCoordinator(prompt_safety_policy=safety, llm_gateway=gateway), gateway


def _run(coord, state):
    import asyncio as _asyncio

    return _asyncio.run(coord.reason("tenant-a", state))


def test_schema_miss_resampled_hidden():
    coord, gateway = _coord(
        [
            {"observations": ["x"], "conclusion_readiness": "not-a-float"},
            {"observations": ["x"], "conclusion_readiness": 0.2},
        ]
    )
    decision = _run(coord, _state())
    assert gateway.complete.await_count == 2
    assert decision.conclusion_readiness == 0.2
    assert decision.metadata.get("resample_count") == 1


def test_multi_action_rejected_then_resampled():
    coord, gateway = _coord(
        [
            {
                "action": {"action_type": "SEARCH_LOGS", "parameters": {}},
                "next_action": "GET_CODE",
                "conclusion_readiness": 0.1,
            },
            {"observations": [], "conclusion_readiness": 0.1},
        ]
    )
    decision = _run(coord, _state())
    assert gateway.complete.await_count == 2
    assert decision.metadata.get("resample_count") == 1


def test_actions_plural_rejected():
    coord, _ = _coord(
        [
            {
                "actions": [{"action_type": "SEARCH_LOGS"}],
                "conclusion_readiness": 0.1,
            }
        ]
    )

    # Direct validator check: plural actions fail closed.
    import pytest

    with pytest.raises(ValueError, match="multiple actions"):
        coord._validate_llm_decision(
            {"actions": [{"action_type": "SEARCH_LOGS"}]},
            "tenant-a",
            _state().investigation.id,
        )


def test_persistent_invalid_exhausts_to_unavailable():
    bad = {"observations": ["x"], "conclusion_readiness": "not-a-float"}
    coord, gateway = _coord([bad, bad, bad])
    decision = _run(coord, _state())
    assert gateway.complete.await_count == 3  # initial + 2 hidden resamples
    assert decision.reasoning_chain == "REASONING_UNAVAILABLE"


def test_valid_defer_gathering_decision_survives_validation():
    coord, _ = _coord([])
    decision = coord._validate_llm_decision(
        {
            "disposition": "DEFER_GATHERING",
            "defer_gathering": {
                "reason": "mixed presence and empty evidence",
                "missing_proof_kind": "ABSENCE",
                "next_action": "GET_CODE",
                "next_action_parameters": {"file_path": "src/handler.py"},
                "target": "handler.py",
                "scope": "repo@rev:src/handler.py",
                "remaining_repair_budget": 2,
            },
            "hypotheses_evaluated": [],
            "claims": [],
            "conclusion_readiness": 0.1,
        },
        "tenant-a",
        _state().investigation.id,
    )

    assert decision.disposition.value == "DEFER_GATHERING"
    assert decision.defer_gathering is not None
    assert decision.defer_gathering.next_action == "GET_CODE"


def test_defer_gathering_rejects_terminal_action():
    coord, _ = _coord([])
    import pytest

    with pytest.raises(ValueError):
        coord._validate_llm_decision(
            {
                "disposition": "DEFER_GATHERING",
                "defer_gathering": {
                    "reason": "needs proof",
                    "missing_proof_kind": "ABSENCE",
                    "next_action": "CONCLUDE",
                    "next_action_parameters": {},
                    "target": "target",
                    "scope": "scope",
                    "remaining_repair_budget": 1,
                },
            },
            "tenant-a",
            _state().investigation.id,
        )


def test_defer_gathering_rejects_unknown_action():
    coord, _ = _coord([])
    import pytest

    with pytest.raises(ValueError, match="gathering action"):
        coord._validate_llm_decision(
            {
                "disposition": "DEFER_GATHERING",
                "defer_gathering": {
                    "reason": "needs proof",
                    "missing_proof_kind": "ABSENCE",
                    "next_action": "FOO",
                    "next_action_parameters": {},
                    "target": "target",
                    "scope": "scope",
                    "remaining_repair_budget": 1,
                },
            },
            "tenant-a",
            _state().investigation.id,
        )


def test_defer_gathering_requires_matching_disposition():
    coord, _ = _coord([])
    import pytest

    with pytest.raises(ValueError):
        coord._validate_llm_decision(
            {
                "defer_gathering": {
                    "reason": "needs proof",
                    "missing_proof_kind": "ABSENCE",
                    "next_action": "GET_CODE",
                    "next_action_parameters": {},
                    "target": "target",
                    "scope": "scope",
                    "remaining_repair_budget": 1,
                }
            },
            "tenant-a",
            _state().investigation.id,
        )
