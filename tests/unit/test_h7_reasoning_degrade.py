"""H7 + T7: reasoning-unavailable degrades, it does not fail.

The line (review part 1, "Where the transient/dead line should sit"):
- timeout / 5xx / rate-limit  -> retryable raise (Temporal retries)
- auth / not-configured       -> provider_blocked handoff
- malformed model output after the bounded resample -> INSUFFICIENT_EVIDENCE
  with a model-quality handoff reason, banked evidence preserved
- FAILED only when no evidence judgement is possible at all

One classifier: classify_provider_failure. These tests pin each branch through
the conclude path, the reason activity, and the coordinator.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from investigation_agent_platform.api.dependencies import AppContext, set_app_context
from investigation_agent_platform.application.worker.activities.inputs import (
    ConcludeInvestigationInput,
)
from investigation_agent_platform.application.worker.activities.lifecycle import (
    conclude_investigation_activity,
)
from investigation_agent_platform.domain.common.exceptions import (
    CredentialProviderError,
    ExecutionError,
    ProviderNotConfiguredError,
    ProviderTimeoutException,
    ProviderUnavailableException,
    RateLimitException,
)
from investigation_agent_platform.domain.finding.models import HandoffState
from investigation_agent_platform.domain.investigation.models import InvestigationStatus

# --- conclude path -----------------------------------------------------------


def _conclude_ctx(*, evidence_rows: list[Any]) -> tuple[AppContext, dict[str, Any], Any]:
    saved: dict[str, Any] = {}

    async def _save_conclusion(tenant_id: str, conclusion: Any) -> Any:
        saved["conclusion"] = conclusion
        return conclusion

    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationContext,
        InvestigationRequest,
    )

    now = datetime.now(UTC)
    inv = Investigation(
        session_id="sess-1",
        application_id="app",
        tenant_id="t",
        status=InvestigationStatus.INVESTIGATING,
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
    inv_repo = SimpleNamespace(get_by_id=AsyncMock(return_value=inv), save=AsyncMock())
    ctx = AppContext()
    ctx.investigation_repo = inv_repo
    # Only a read method: any attempt to delete/modify banked evidence raises.
    ctx.evidence_repo = SimpleNamespace(find_by_investigation_id=AsyncMock(return_value=evidence_rows))
    ctx.hypothesis_repo = SimpleNamespace(
        find_by_investigation_id=AsyncMock(return_value=[]), save=AsyncMock()
    )
    ctx.finding_repo = SimpleNamespace(
        save_conclusion=AsyncMock(side_effect=_save_conclusion),
        save_finding=AsyncMock(return_value=None),
    )
    ctx.timeline_repo = SimpleNamespace(
        find_by_investigation_id=AsyncMock(return_value=[]), append=AsyncMock(return_value=None)
    )
    set_app_context(ctx)
    return ctx, saved, inv_repo


async def _conclude(reason: str, *, evidence_rows: list[Any]) -> tuple[Any, dict[str, Any], Any]:
    _, saved, inv_repo = _conclude_ctx(evidence_rows=evidence_rows)
    result = await conclude_investigation_activity(
        ConcludeInvestigationInput(
            tenant_id="t", investigation_id=str(uuid4()), status="FAILED", reason=reason
        )
    )
    return result, saved, inv_repo


_ROW = SimpleNamespace(evidence_id=uuid4())


@pytest.mark.parametrize(
    "reason",
    [
        "reasoning_model_output_invalid:invalid_llm_schema:ValidationError",
        "reasoning_unavailable:llm_gateway_error:RuntimeError",
        "defer_gathering_invalid",
    ],
)
async def test_model_quality_endings_degrade_to_insufficient_evidence(reason: str) -> None:
    result, saved, _ = await _conclude(reason, evidence_rows=[_ROW])
    assert result.success is True
    assert result.data.get("status") == InvestigationStatus.COMPLETED.value
    conclusion = saved["conclusion"]
    assert str(conclusion.status) == "INSUFFICIENT_EVIDENCE"
    assert conclusion.root_cause is None and conclusion.confidence == 0.0
    # The ending is named for what it is, not as a generic verification miss.
    assert reason.split(":")[0] in " ".join(conclusion.limitations)
    assert "within budget" not in " ".join(conclusion.limitations)
    assert conclusion.handoff is not None and conclusion.next_check
    assert conclusion.handoff.next_fetch is None or conclusion.handoff.price is not None


async def test_model_output_invalid_handoff_names_the_model_quality_reason() -> None:
    _, saved, _ = await _conclude(
        "reasoning_model_output_invalid:invalid_llm_schema:ValidationError", evidence_rows=[_ROW]
    )
    conclusion = saved["conclusion"]
    assert "model" in conclusion.next_check.lower()
    assert any("model" in step.lower() for step in conclusion.recommended_next_steps)


async def test_provider_blocked_reasoning_is_a_deferred_handoff() -> None:
    result, saved, _ = await _conclude("provider_blocked:reasoning_auth_failed", evidence_rows=[])
    assert result.data.get("status") == InvestigationStatus.COMPLETED.value
    conclusion = saved["conclusion"]
    assert str(conclusion.status) == "INSUFFICIENT_EVIDENCE"
    assert conclusion.handoff.state == HandoffState.TELEMETRY_DEFERRED


async def test_provider_unavailable_with_banked_evidence_is_a_resumable_deferral() -> None:
    result, saved, _ = await _conclude(
        "reasoning_provider_unavailable:llm_gateway_error:TimeoutError", evidence_rows=[_ROW]
    )
    assert result.data.get("status") == InvestigationStatus.COMPLETED.value
    conclusion = saved["conclusion"]
    assert str(conclusion.status) == "INSUFFICIENT_EVIDENCE"
    assert conclusion.handoff.state == HandoffState.REASONING_DEFERRED
    assert conclusion.handoff.resume_pointer


async def test_provider_unavailable_with_nothing_banked_stays_failed() -> None:
    # No evidence judgement is possible at all: FAILED, with a stable code.
    result, saved, inv_repo = await _conclude(
        "reasoning_provider_unavailable:llm_gateway_error:TimeoutError", evidence_rows=[]
    )
    assert result.data.get("status") == InvestigationStatus.FAILED.value
    assert "conclusion" not in saved
    stamped = inv_repo.save.await_args.args[-1] if inv_repo.save.await_args else None
    metadata = getattr(stamped, "metadata", {}) or {}
    assert (metadata.get("failure") or {}).get("code") == "REASONING_UNAVAILABLE"


async def test_existing_epistemic_endings_are_unchanged() -> None:
    for reason in ("budget_exceeded", "defer_repair_budget_exhausted"):
        result, saved, _ = await _conclude(reason, evidence_rows=[_ROW])
        assert result.data.get("status") == InvestigationStatus.COMPLETED.value, reason
        assert "within budget" in " ".join(saved["conclusion"].limitations), reason


# --- pure mapping module -------------------------------------------------------


def test_last_error_encodes_each_kind_distinctly() -> None:
    from investigation_agent_platform.application.worker.reasoning_failure import (
        KIND_BLOCKED,
        KIND_MODEL_OUTPUT,
        KIND_OTHER,
        KIND_TRANSIENT,
        last_error_for,
    )

    assert last_error_for(KIND_BLOCKED, "llm_gateway_error:CredentialProviderError").startswith(
        "reasoning_provider_blocked:"
    )
    assert last_error_for(KIND_MODEL_OUTPUT, "invalid_llm_schema:X").startswith(
        "reasoning_model_output_invalid:"
    )
    assert last_error_for(KIND_TRANSIENT, "llm_gateway_error:TimeoutError").startswith(
        "reasoning_provider_unavailable:"
    )
    assert last_error_for(KIND_OTHER, "boom").startswith("reasoning_unavailable:")
    assert last_error_for(KIND_OTHER, "") == "reasoning_unavailable"
    assert len(last_error_for(KIND_OTHER, "x" * 5000)) <= 240


def test_unknown_kind_falls_back_to_the_generic_reasoning_reason() -> None:
    from investigation_agent_platform.application.worker.reasoning_failure import last_error_for

    assert last_error_for("nonsense", "r").startswith("reasoning_unavailable:")


# --- single classifier: classify_provider_failure is reused -----------------------


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (ProviderTimeoutException("t"), "transient"),
        (TimeoutError("t"), "transient"),
        (ProviderUnavailableException("down"), "transient"),
        (ConnectionError("refused"), "transient"),
        (RateLimitException("throttled", retry_after=12.0), "transient"),
        (ExecutionError("429 Too Many Requests"), "transient"),
        (CredentialProviderError("expired"), "blocked"),
        (ProviderNotConfiguredError("no llm"), "blocked"),
        # Policy of the shared classifier: a sniffed third-party auth message
        # degrades as provider_unavailable (it never blocks by itself), so it is
        # retried a bounded number of times and then degrades. Follow-up: the
        # classifier cannot tell a 401 from an outage.
        (ExecutionError("401 Unauthorized"), "transient"),
        (ValueError("bug"), "other"),
        (ExecutionError("boom"), "other"),
    ],
)
def test_reasoning_failure_kind_comes_from_classify_provider_failure(
    exc: BaseException, kind: str
) -> None:
    from investigation_agent_platform.application.worker.activities.common import (
        classify_reasoning_failure,
    )

    assert classify_reasoning_failure(exc)[0] == kind


def test_rate_limit_hint_is_carried() -> None:
    from investigation_agent_platform.application.worker.activities.common import (
        classify_reasoning_failure,
    )

    assert classify_reasoning_failure(RateLimitException("t", retry_after=12.0)) == (
        "transient",
        12.0,
    )
    assert classify_reasoning_failure(TimeoutError("t")) == ("transient", None)


# --- coordinator: the decision carries the failure kind -------------------------


def _coordinator(gateway: Any) -> tuple[Any, Any]:
    from investigation_agent_platform.application.investigation.reasoning import (
        ReasoningCoordinator,
    )
    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationContext,
        InvestigationRequest,
        InvestigationState,
    )

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
    return (
        ReasoningCoordinator(prompt_safety_policy=safety, llm_gateway=gateway),
        InvestigationState(investigation=inv),
    )


def _gateway(side_effect: list[Any]) -> Any:
    gateway = MagicMock()
    gateway.complete = AsyncMock(side_effect=side_effect)
    return gateway


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (TimeoutError("slow"), "transient"),
        (ProviderUnavailableException("503"), "transient"),
        (RateLimitException("429", retry_after=7.0), "transient"),
        (CredentialProviderError("bad key"), "blocked"),
        (RuntimeError("weird"), "other"),
    ],
)
async def test_gateway_failure_decision_carries_kind(exc: BaseException, kind: str) -> None:
    coord, state = _coordinator(_gateway([exc]))
    decision = await coord.reason("tenant-a", state)
    assert decision.metadata["status"] == "REASONING_UNAVAILABLE"
    assert decision.metadata["failure_kind"] == kind
    if isinstance(exc, RateLimitException):
        assert decision.metadata["retry_after"] == 7.0


async def test_missing_gateway_is_a_config_block() -> None:
    coord, state = _coordinator(None)
    decision = await coord.reason("tenant-a", state)
    assert decision.metadata["failure_kind"] == "blocked"


async def test_malformed_output_after_resamples_is_model_output() -> None:
    from investigation_agent_platform.ports.observability.telemetry import LLMCallMetadata
    from investigation_agent_platform.ports.reasoning.llm_gateway import LLMGatewayResponse

    def _resp() -> Any:
        return LLMGatewayResponse(
            content="{}",
            parsed={"observations": [], "conclusion_readiness": "high"},
            metadata=LLMCallMetadata(
                model_name="t",
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
                estimated_cost_usd=0.0,
                latency_ms=1.0,
            ),
        )

    gateway = _gateway([_resp(), _resp(), _resp()])
    coord, state = _coordinator(gateway)
    decision = await coord.reason("tenant-a", state)
    assert gateway.complete.call_count == 3  # bounded resample unchanged (H6 varies it)
    assert decision.metadata["failure_kind"] == "model_output"


async def test_transport_error_during_a_resample_is_not_misreported_as_model_output() -> None:
    from investigation_agent_platform.ports.observability.telemetry import LLMCallMetadata
    from investigation_agent_platform.ports.reasoning.llm_gateway import LLMGatewayResponse

    bad = LLMGatewayResponse(
        content="{}",
        parsed={"observations": [], "conclusion_readiness": "high"},
        metadata=LLMCallMetadata(
            model_name="t",
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            estimated_cost_usd=0.0,
            latency_ms=1.0,
        ),
    )
    coord, state = _coordinator(_gateway([bad, TimeoutError("slow on resample")]))
    decision = await coord.reason("tenant-a", state)
    assert decision.metadata["failure_kind"] == "transient"


# --- reason activity: raise vs return -----------------------------------------------


def _reason_ctx(decision: Any) -> None:
    now = datetime.now(UTC)
    from investigation_agent_platform.domain.investigation.models import (
        Investigation,
        InvestigationContext,
        InvestigationRequest,
    )

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
    ctx = AppContext()
    ctx.investigation_repo = SimpleNamespace(get_by_id=AsyncMock(return_value=inv))
    ctx.reasoning_coordinator = SimpleNamespace(reason=AsyncMock(return_value=decision))  # type: ignore[attr-defined]
    set_app_context(ctx)


def _decision(kind: str | None, reason: str = "llm_gateway_error:X", **extra: Any) -> Any:
    metadata: dict[str, Any] = {"status": "REASONING_UNAVAILABLE", "reason": reason}
    if kind is not None:
        metadata["failure_kind"] = kind
    metadata.update(extra)
    return SimpleNamespace(metadata=metadata, reasoning_chain="REASONING_UNAVAILABLE")


async def _reason() -> Any:
    from investigation_agent_platform.application.worker.activities import (
        ReasonInput,
        reason_activity,
    )

    return await reason_activity(ReasonInput(tenant_id="tenant-a", investigation_id=str(uuid4())))


async def test_transient_failure_raises_retryably_so_temporal_retries() -> None:
    from temporalio.exceptions import ApplicationError

    _reason_ctx(_decision("transient", "llm_gateway_error:TimeoutError"))
    with pytest.raises(ApplicationError) as exc:
        await _reason()
    assert exc.value.non_retryable is False
    assert exc.value.type == "ReasoningProviderTransient"
    assert "TimeoutError" in str(exc.value)


async def test_rate_limit_hint_becomes_the_retry_delay() -> None:
    from temporalio.exceptions import ApplicationError

    _reason_ctx(_decision("transient", "llm_gateway_error:RateLimitException", retry_after=7.0))
    with pytest.raises(ApplicationError) as exc:
        await _reason()
    assert exc.value.next_retry_delay == timedelta(seconds=7)


async def test_absurd_retry_after_is_clamped() -> None:
    from temporalio.exceptions import ApplicationError

    _reason_ctx(_decision("transient", "r", retry_after=86400.0))
    with pytest.raises(ApplicationError) as exc:
        await _reason()
    assert exc.value.next_retry_delay is not None
    assert exc.value.next_retry_delay <= timedelta(seconds=60)


@pytest.mark.parametrize("kind", ["blocked", "model_output", "other", None])
async def test_non_transient_failures_return_a_typed_result_not_a_raise(kind: str | None) -> None:
    _reason_ctx(_decision(kind))
    result = await _reason()
    assert result.success is True
    assert result.data["reasoning_unavailable"] is True
    assert result.data["failure_kind"] == (kind or "other")


async def test_unavailable_result_carries_the_spent_usage() -> None:
    _reason_ctx(
        _decision(
            "model_output",
            llm_usage={"prompt_tokens": 150, "completion_tokens": 15, "cost_usd": 0.3},
        )
    )
    result = await _reason()
    assert result.data["llm_usage"]["prompt_tokens"] == 150


# --- workflow mapping ------------------------------------------------------------


def test_transient_activity_error_is_recognised_through_the_cause_chain() -> None:
    from investigation_agent_platform.application.worker.workflows.forcing import (
        _caused_by_reasoning_transient,
    )

    class _Cause(Exception):
        type = "ReasoningProviderTransient"

    real = Exception("activity failed")
    real.__cause__ = _Cause("llm_gateway_error:TimeoutError")
    assert _caused_by_reasoning_transient(real) is True
    assert _caused_by_reasoning_transient(Exception("other")) is False
    assert _caused_by_reasoning_transient(ValueError("x")) is False


def test_workflow_maps_reasoning_failures_and_catches_exhausted_retries() -> None:
    import inspect

    from investigation_agent_platform.application.worker.workflows import run_investigation

    source = inspect.getsource(run_investigation)
    assert "last_error_for(" in source, "typed reasoning failure must set a typed last_error"
    assert "_caused_by_reasoning_transient" in source, (
        "an exhausted reason_activity retry must degrade, not fail the run"
    )
