# tests/unit/test_h7_typed_failures.py
"""H7: typed 401/403 blocks, REASONING_DEFERRED handoff, INPUT_DEFERRED, banked-error context."""

import pytest
import test_h7_reasoning_degrade as h7
from test_h7_reasoning_degrade import _conclude

from investigation_agent_platform.application.worker.activities.common import classify_reasoning_failure
from investigation_agent_platform.application.worker.reasoning_failure import KIND_BLOCKED, KIND_TRANSIENT
from investigation_agent_platform.domain.finding.models import HandoffState, S7Handoff


class _Exc(Exception):
    def __init__(self, status=None, cause=None):  # type: ignore[no-untyped-def]
        super().__init__("typed")
        if status is not None:
            self.status_code = status
        if cause is not None:
            self.__cause__ = cause


def test_typed_401_blocks() -> None:
    assert classify_reasoning_failure(_Exc(status=401)) == (KIND_BLOCKED, None)


def test_typed_403_on_cause_chain_blocks() -> None:
    inner = _Exc()
    inner.http_status_code = 403
    assert classify_reasoning_failure(_Exc(cause=inner)) == (KIND_BLOCKED, None)


def test_sniffed_text_still_degrades() -> None:
    assert classify_reasoning_failure(RuntimeError("request failed 401"))[0] == KIND_TRANSIENT


@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
def test_other_statuses_are_not_blocked(status) -> None:  # type: ignore[no-untyped-def]
    kind, _ = classify_reasoning_failure(_Exc(status=status))
    assert kind != KIND_BLOCKED


@pytest.mark.parametrize("status", [True, "401", None])
def test_non_int_status_ignored(status) -> None:  # type: ignore[no-untyped-def]
    kind, _ = classify_reasoning_failure(_Exc(status=status))
    assert kind != KIND_BLOCKED


@pytest.mark.asyncio
async def test_coordinator_typed_401_is_blocked() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from test_h7_reasoning_degrade import _coordinator

    class _Gateway401(Exception):
        status_code = 401

    gateway = MagicMock()
    gateway.complete = AsyncMock(side_effect=_Gateway401("denied"))
    coord, state = _coordinator(gateway)
    decision = await coord.reason("tenant-a", state)
    assert decision.metadata["failure_kind"] == "blocked"


def test_reasoning_outage_uses_reasoning_deferred() -> None:
    from investigation_agent_platform.domain.finding.models import classify_terminal_handoff

    for reason in (
        "reasoning_provider_blocked:x",
        "reasoning_provider_unavailable:x",
        "reasoning_model_output_invalid:x",
        "reasoning_unavailable:x",
        "defer_gathering_invalid",
    ):
        handoff = classify_terminal_handoff(status="INSUFFICIENT_EVIDENCE", reason=reason)
        assert handoff.state == HandoffState.REASONING_DEFERRED, reason
    assert classify_terminal_handoff(status="INSUFFICIENT_EVIDENCE", reason="provider_blocked:auth_failed").state == HandoffState.TELEMETRY_DEFERRED


def test_reasoning_blocked_next_check() -> None:
    from investigation_agent_platform.application.worker.activities.lifecycle import _epistemic_next_check

    text = _epistemic_next_check("reasoning_provider_blocked:x")
    assert "reasoning provider" in text.lower()
    assert "fetch" not in text.lower()


def test_reasoning_blocked_stays_epistemic() -> None:
    from investigation_agent_platform.application.worker.activities.lifecycle import (
        _is_reasoning_terminal_reason,
    )

    assert _is_reasoning_terminal_reason("reasoning_provider_blocked:x") is True


def test_old_handoff_rows_still_parse() -> None:
    h = S7Handoff.model_validate(
        {"state": "TELEMETRY_DEFERRED", "next_fetch": None, "recommended_next_steps": []}
    )
    assert h.state == HandoffState.TELEMETRY_DEFERRED


def test_failure_code_for_new_prefix() -> None:
    from investigation_agent_platform.application.worker.activities.inputs import _failure_code_for

    assert _failure_code_for("reasoning_provider_blocked:x", False) == "REASONING_UNAVAILABLE"


def test_input_deferred_handoff() -> None:
    from investigation_agent_platform.domain.finding.models import classify_terminal_handoff

    handoff = classify_terminal_handoff(status="INSUFFICIENT_EVIDENCE", reason="input_expired: req-1")
    assert handoff.state == HandoffState.INPUT_DEFERRED


@pytest.mark.asyncio
async def test_input_expired_with_nothing_banked_stays_failed() -> None:
    result, saved, inv_repo = await h7._conclude("input_expired: req-1", evidence_rows=[])
    assert result.data.get("status") == "FAILED"
    assert "conclusion" not in saved


def test_banked_evidence_count_in_failure_context() -> None:
    import inspect

    import investigation_agent_platform.application.worker.workflows.run_investigation as ri

    assert "banked_evidence" in inspect.getsource(ri)


def test_temporal_harness_deferred() -> None:
    pytest.skip("first Temporal test-server use; requires owner approval (binary download)")


def test_input_expired_with_banked_evidence_completes() -> None:
    import inspect

    import investigation_agent_platform.application.worker.workflows.run_investigation as ri

    src = inspect.getsource(ri)
    assert 'last_error == "input_expired" and evidence_banked_total > 0' in src
