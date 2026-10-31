"""H5: a deterministic checkability floor under the model's NOT_CHECKABLE label.

Review H5: checkability was classified by the LLM alone, so a causal claim the
model labelled NOT_CHECKABLE forced a zero-evidence CONCLUDE (D6). The floor
re-types a NOT_CHECKABLE claim as checkable when it (a) states a causal
mechanism or (b) cites evidence. It errs toward checkable: a wrongly re-typed
opinion only costs retrieval, it can never publish a verdict.

Deliberately NOT floored (probe, see the ledger): a claim that merely names a
code identifier or path. "OrderServiceHandler is the best-designed handler" is a
taste judgment about named code and stays NOT_CHECKABLE under Phase 4 D5.

Version-pinned (lv3): a run pinned lv2 keeps the old trust-the-label behavior.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from investigation_agent_platform.application.investigation.reasoning import (
    ReasoningCoordinator,
    checkability_floor_reason,
)
from investigation_agent_platform.application.worker.workflows.forcing import (
    _should_force_conclude_not_checkable,
)
from investigation_agent_platform.domain.investigation.models import (
    Investigation,
    InvestigationContext,
    InvestigationRequest,
    InvestigationState,
)
from investigation_agent_platform.ports.observability.telemetry import LLMCallMetadata
from investigation_agent_platform.ports.reasoning.llm_gateway import LLMGatewayResponse

# --- the pure floor ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "Connection pool exhaustion in HikariPool causes the order timeout under load",
        "SampleOrderFilter.doFilter throws NullPointerException because orderType is null",
        "The retry loop leads to duplicate charges",
        "A missing index results in full table scans",
        "The cache stampede triggers the outage",
        "The handler fails when the queue is full",
        "Requests time out due to lock contention",
        "The pool exhausts under burst traffic",
    ],
)
def test_a_causal_mechanism_cannot_be_not_checkable(statement: str) -> None:
    assert checkability_floor_reason(statement, []) == "causal_mechanism"


@pytest.mark.parametrize(
    "statement",
    [
        "The implementation is beautiful",
        "This is best practice",
        "OrderServiceHandler is the best-designed handler in the service",
        "The code in service/src/main/java/com/sample/orders/filter/SampleOrderFilter.java is ugly",
        "Users prefer the older checkout flow",
        "Editors are better than IDEs",
    ],
)
def test_taste_and_named_code_judgments_stay_not_checkable(statement: str) -> None:
    # Includes identifiers and paths: naming code does not make a taste claim checkable.
    assert checkability_floor_reason(statement, []) is None


def test_an_nc_claim_that_cites_evidence_is_not_opinion_only() -> None:
    assert checkability_floor_reason("The design is elegant", ["e1"]) == "cites_evidence"


def test_the_floor_never_raises_on_bad_input() -> None:
    assert checkability_floor_reason(None, None) is None  # type: ignore[arg-type]
    assert checkability_floor_reason("", ["", None]) is None  # type: ignore[list-item]


def test_cites_evidence_ignores_blank_ids() -> None:
    assert checkability_floor_reason("The design is elegant", ["", "  "]) is None


# --- through the coordinator ----------------------------------------------------------------


def _coord(parsed: dict[str, Any], metadata: dict[str, Any] | None) -> tuple[Any, Any]:
    now = datetime.now(UTC)
    kwargs: dict[str, Any] = {}
    if metadata is not None:
        kwargs["metadata"] = metadata
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
        **kwargs,
    )
    safety = MagicMock()
    safety.validate_prompt_safety = AsyncMock(return_value=True)
    gateway = MagicMock()
    gateway.complete = AsyncMock(
        return_value=LLMGatewayResponse(
            content="{}",
            parsed=parsed,
            metadata=LLMCallMetadata(
                model_name="t",
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
                estimated_cost_usd=0.0,
                latency_ms=1.0,
            ),
        )
    )
    return (
        ReasoningCoordinator(prompt_safety_policy=safety, llm_gateway=gateway),
        InvestigationState(investigation=inv),
    )


def _nc_payload(statement: str, evidence_ids: list[str] | None = None) -> dict[str, Any]:
    return {
        "observations": [],
        "conclusion_readiness": 0.0,
        "hypotheses_evaluated": [statement],
        "claims": [
            {
                "statement": statement,
                "claim_kind": "NOT_CHECKABLE",
                "evidence_ids": list(evidence_ids or []),
            }
        ],
    }


def _kinds(decision: Any) -> list[str]:
    return [c.claim_kind.value for c in decision.claims]


def _force_conclude(decision: Any) -> bool:
    return _should_force_conclude_not_checkable(
        {
            "hypotheses_evaluated": list(decision.hypotheses_evaluated),
            "claims": [
                {"statement": c.statement, "claim_kind": c.claim_kind.value}
                for c in decision.claims
            ],
        }
    )


CAUSAL = "Connection pool exhaustion in HikariPool causes the order timeout under load"


async def test_causal_claim_labelled_nc_is_retyped_and_d6_no_longer_forces_conclude() -> None:
    coord, state = _coord(_nc_payload(CAUSAL), {"loop_version": "lv3"})
    decision = await coord.reason("tenant-a", state)
    assert _kinds(decision) == ["CAUSAL"]
    assert _force_conclude(decision) is False, "the zero-evidence CONCLUDE is no longer forced"
    assert decision.metadata["checkability_floor"] == {"causal_mechanism": 1}


async def test_unmarked_investigation_resolves_to_current_and_gets_the_floor() -> None:
    coord, state = _coord(_nc_payload(CAUSAL), None)
    decision = await coord.reason("tenant-a", state)
    assert _kinds(decision) == ["CAUSAL"]


async def test_lv2_pinned_run_keeps_trusting_the_label() -> None:
    coord, state = _coord(_nc_payload(CAUSAL), {"loop_version": "lv2"})
    decision = await coord.reason("tenant-a", state)
    assert _kinds(decision) == ["NOT_CHECKABLE"]
    assert _force_conclude(decision) is True
    assert "checkability_floor" not in decision.metadata


async def test_genuine_opinion_still_forces_the_honest_abstention() -> None:
    coord, state = _coord(_nc_payload("The implementation is beautiful"), {"loop_version": "lv3"})
    decision = await coord.reason("tenant-a", state)
    assert _kinds(decision) == ["NOT_CHECKABLE"]
    assert _force_conclude(decision) is True
    assert "checkability_floor" not in decision.metadata


async def test_nc_claim_citing_evidence_is_retyped() -> None:
    coord, state = _coord(
        _nc_payload("The design is elegant", ["11111111-1111-1111-1111-111111111111"]),
        {"loop_version": "lv3"},
    )
    decision = await coord.reason("tenant-a", state)
    assert _kinds(decision) == ["CAUSAL"]
    assert decision.metadata["checkability_floor"] == {"cites_evidence": 1}


async def test_checkable_claims_are_never_touched() -> None:
    payload = {
        "observations": [],
        "conclusion_readiness": 0.0,
        "hypotheses_evaluated": ["Foo is defined in Foo.java"],
        "claims": [{"statement": "Foo is defined in Foo.java", "claim_kind": "DEFINITION"}],
    }
    coord, state = _coord(payload, {"loop_version": "lv3"})
    decision = await coord.reason("tenant-a", state)
    assert _kinds(decision) == ["DEFINITION"]
    assert "checkability_floor" not in decision.metadata


async def test_mixed_decision_only_retypes_the_offending_claim() -> None:
    payload = {
        "observations": [],
        "conclusion_readiness": 0.0,
        "hypotheses_evaluated": ["The design is elegant", CAUSAL],
        "claims": [
            {"statement": "The design is elegant", "claim_kind": "NOT_CHECKABLE"},
            {"statement": CAUSAL, "claim_kind": "NOT_CHECKABLE"},
        ],
    }
    coord, state = _coord(payload, {"loop_version": "lv3"})
    decision = await coord.reason("tenant-a", state)
    assert _kinds(decision) == ["NOT_CHECKABLE", "CAUSAL"]
    assert _force_conclude(decision) is False
