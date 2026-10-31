"""H6: the hidden resample varies, covers every malformed-output class, and sums usage.

Review H6: the resample re-sent a byte-identical request (so it mostly re-rolled
noise), only ValidationError and the one-action cardinality errors resampled
(defer_gathering shape errors, a disallowed action_type and a non-object failed
on the first try), and token usage counted only the last response.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from investigation_agent_platform.application.investigation.reasoning import (
    ReasoningCoordinator,
    is_resamplable_decision_error,
)
from investigation_agent_platform.domain.investigation.models import (
    ActionType,
    Investigation,
    InvestigationContext,
    InvestigationRequest,
    InvestigationState,
)
from investigation_agent_platform.ports.observability.telemetry import LLMCallMetadata
from investigation_agent_platform.ports.reasoning.llm_gateway import LLMGatewayResponse

SECRET = "SECRET-MODEL-VALUE-123"


def _resp(parsed: dict[str, Any] | None, prompt_tokens: int = 10, cost: float = 0.01) -> Any:
    return LLMGatewayResponse(
        content="{}",
        parsed=parsed,
        metadata=LLMCallMetadata(
            model_name="t",
            prompt_tokens=prompt_tokens,
            completion_tokens=5,
            total_tokens=prompt_tokens + 5,
            estimated_cost_usd=cost,
            latency_ms=1.0,
        ),
    )


def _coord(responses: list[Any]) -> tuple[ReasoningCoordinator, InvestigationState, Any]:
    now = datetime.now(UTC)
    inv = Investigation(
        session_id="sess-1",
        application_id="app",
        tenant_id="tenant-a",
        request=InvestigationRequest(
            application_id="app",
            problem_description="p",
            session_id="sess-1",
            requested_by="tester",
        ),
        context=InvestigationContext(
            environment="production", time_window=(now - timedelta(hours=1), now)
        ),
    )
    safety = MagicMock()
    safety.validate_prompt_safety = AsyncMock(return_value=True)
    gateway = MagicMock()
    gateway.complete = AsyncMock(side_effect=responses)
    return (
        ReasoningCoordinator(prompt_safety_policy=safety, llm_gateway=gateway),
        InvestigationState(investigation=inv),
        gateway,
    )


def _valid() -> dict[str, Any]:
    return {"observations": [], "conclusion_readiness": 0.0, "hypotheses_evaluated": ["pool fails"]}


def _schema_invalid() -> dict[str, Any]:
    return {"observations": [SECRET], "conclusion_readiness": "high"}


def _bad_defer() -> dict[str, Any]:
    return {
        "observations": [],
        "conclusion_readiness": 0.0,
        "disposition": "DEFER_GATHERING",
        "defer_gathering": SECRET,  # must be an object
    }


def _disallowed_action() -> dict[str, Any]:
    return {
        "observations": [],
        "conclusion_readiness": 0.0,
        "action": {"action_type": SECRET, "parameters": {}},
    }


def _prompts(gateway: Any) -> list[str]:
    return [call.args[1].prompt for call in gateway.complete.call_args_list]


# --- the resample varies ----------------------------------------------------------------


async def test_the_resample_request_differs_from_the_first() -> None:
    coord, state, gateway = _coord([_resp(_schema_invalid()), _resp(_valid())])
    await coord.reason("tenant-a", state)
    first, second = _prompts(gateway)
    assert first != second
    assert second.startswith(first), "the original prompt is preserved; the hint is appended"


async def test_the_first_request_is_untouched() -> None:
    coord, state, gateway = _coord([_resp(_valid())])
    await coord.reason("tenant-a", state)
    assert gateway.complete.call_count == 1
    assert "previous reply" not in _prompts(gateway)[0].lower()


async def test_each_resample_gets_its_own_hint_and_nothing_loops_forever() -> None:
    coord, state, gateway = _coord([_resp(_schema_invalid()) for _ in range(5)])
    decision = await coord.reason("tenant-a", state)
    assert gateway.complete.call_count == 3  # one call plus two resamples, as before
    assert decision.metadata["failure_kind"] == "model_output"


async def test_the_hint_names_fields_never_model_values() -> None:
    coord, state, gateway = _coord([_resp(_schema_invalid()), _resp(_valid())])
    await coord.reason("tenant-a", state)
    hint_prompt = _prompts(gateway)[1]
    assert SECRET not in hint_prompt
    assert "conclusion_readiness" in hint_prompt, "the failing field name is the repair signal"


# --- coverage widened ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bad", "marker"),
    [
        (_bad_defer(), "defer_gathering"),
        (_disallowed_action(), "action_type"),
    ],
)
async def test_previously_fail_fast_classes_now_resample_and_recover(
    bad: dict[str, Any], marker: str
) -> None:
    coord, state, gateway = _coord([_resp(bad), _resp(_valid())])
    decision = await coord.reason("tenant-a", state)
    assert gateway.complete.call_count == 2
    assert decision.metadata.get("status") != "REASONING_UNAVAILABLE"
    assert marker in _prompts(gateway)[1]
    assert SECRET not in _prompts(gateway)[1]


async def test_a_disallowed_action_hint_lists_the_allowed_vocabulary() -> None:
    coord, state, gateway = _coord([_resp(_disallowed_action()), _resp(_valid())])
    await coord.reason("tenant-a", state)
    hint_prompt = _prompts(gateway)[1]
    assert ActionType.GET_CODE.value in hint_prompt


async def test_persistent_defer_shape_error_ends_as_model_output() -> None:
    coord, state, gateway = _coord([_resp(_bad_defer()) for _ in range(3)])
    decision = await coord.reason("tenant-a", state)
    assert gateway.complete.call_count == 3
    assert decision.metadata["failure_kind"] == "model_output"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ValueError("LLM decision proposes more than one action"), True),
        (ValueError("defer_gathering must be an object"), True),
        (ValueError("defer_gathering.next_action must be a gathering action"), True),
        (TypeError("LLM decision is not an object: str"), True),
        (ValueError("LLM decision contains disallowed action_type: X"), True),
        (ValueError("LLM decision action.parameters is not an object"), True),
        (ValueError("something unrelated"), False),
        (RuntimeError("transport"), False),
        (KeyError("k"), False),
    ],
)
def test_resamplable_classification(exc: BaseException, expected: bool) -> None:
    assert is_resamplable_decision_error(exc) is expected


def test_pydantic_validation_errors_are_resamplable() -> None:
    from pydantic import ValidationError

    from investigation_agent_platform.ports.reasoning.reasoner import InvestigationDecision

    with pytest.raises(ValidationError) as caught:
        InvestigationDecision.model_validate({"observations": [], "conclusion_readiness": "high"})
    assert is_resamplable_decision_error(caught.value) is True


# --- usage is summed across attempts ----------------------------------------------------------


async def test_usage_sums_every_attempt_not_just_the_last() -> None:
    coord, state, _ = _coord(
        [
            _resp(_schema_invalid(), prompt_tokens=100, cost=0.10),
            _resp(_schema_invalid(), prompt_tokens=200, cost=0.20),
            _resp(_valid(), prompt_tokens=300, cost=0.30),
        ]
    )
    decision = await coord.reason("tenant-a", state)
    usage = decision.metadata["llm_usage"]
    assert usage["prompt_tokens"] == 600
    assert usage["completion_tokens"] == 15
    assert usage["cost_usd"] == pytest.approx(0.60)
    assert decision.metadata["resample_count"] == 2


async def test_single_call_usage_is_unchanged() -> None:
    coord, state, _ = _coord([_resp(_valid(), prompt_tokens=40, cost=0.04)])
    decision = await coord.reason("tenant-a", state)
    assert decision.metadata["llm_usage"] == {
        "prompt_tokens": 40,
        "completion_tokens": 5,
        "cost_usd": pytest.approx(0.04),
    }


async def test_usage_of_failed_attempts_is_not_lost_when_the_decision_is_unavailable() -> None:
    coord, state, _ = _coord([_resp(_schema_invalid(), prompt_tokens=50) for _ in range(3)])
    decision = await coord.reason("tenant-a", state)
    assert decision.metadata["status"] == "REASONING_UNAVAILABLE"
    assert decision.metadata["llm_usage"]["prompt_tokens"] == 150
