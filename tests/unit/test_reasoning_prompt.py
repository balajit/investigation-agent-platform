# tests/unit/test_reasoning_prompt.py
"""G2: system prompt steers explicit code targets to GET_CODE first."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from investigation_agent_platform.application.investigation.context_compiler import (
    REASONER_PROMPT_VERSION,
)
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


def _state(problem="test incident"):
    now = datetime.now(UTC)
    inv = Investigation(
        session_id="sess-1",
        application_id="app",
        tenant_id="tenant-a",
        request=InvestigationRequest(
            application_id="app",
            problem_description=problem,
            session_id="sess-1",
            requested_by="tester",
        ),
        context=InvestigationContext(
            environment="production",
            time_window=(now - timedelta(seconds=3600), now),
        ),
    )
    return InvestigationState(investigation=inv)


def _capture_gateway():
    from investigation_agent_platform.ports.observability.telemetry import (
        LLMCallMetadata,
    )

    gateway = MagicMock()
    gateway.complete = AsyncMock(
        return_value=LLMGatewayResponse(
            content="{}",
            parsed={"observations": [], "conclusion_readiness": 0.0},
            metadata=LLMCallMetadata(
                model_name="test",
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
                estimated_cost_usd=0.0,
                latency_ms=1.0,
            ),
        )
    )
    return gateway


def _system_prompt_for(problem):
    safety = MagicMock()
    safety.validate_prompt_safety = AsyncMock(return_value=True)
    gateway = _capture_gateway()
    coord = ReasoningCoordinator(prompt_safety_policy=safety, llm_gateway=gateway)
    import asyncio as _asyncio

    _asyncio.run(coord.reason("tenant-a", _state(problem)))
    call = gateway.complete.call_args
    request = call.kwargs.get("request") if call.kwargs else None
    if request is None and call.args:
        request = call.args[-1]
    return getattr(request, "system_prompt", "") or ""


def test_prompt_version_bumped_for_g1():
    assert REASONER_PROMPT_VERSION == "v3-claim-grounded-019"


def test_explicit_path_short_circuits_to_get_code_first():
    prompt = _system_prompt_for(
        "read the apply_effective_limits function in "
        "src/investigation_agent_platform/application/worker/activities.py at HEAD"
    )
    assert "EXCEPTION (explicit code target)" in prompt
    assert "the FIRST action MUST be GET_CODE" in prompt


def test_action_enum_lists_code_capabilities():
    prompt = _system_prompt_for("something failed")
    for action in (
        "SEARCH_CODE",
        "FIND_SYMBOL",
        "FIND_CALL_GRAPH",
        "GET_CODE_HISTORY",
        "PROBE_API",
    ):
        assert action in prompt, action


def test_logs_first_default_preserved():
    prompt = _system_prompt_for("latency spike with no path named")
    assert "use SEARCH_LOGS" in prompt


def test_definition_hypothesis_requires_get_code_first():
    prompt = _system_prompt_for("where is something defined")
    assert "MUST be followed by GET_CODE of the" in prompt
    assert "search matches alone never verify a definition" in prompt


def test_cite_range_read_not_search_match():
    prompt = _system_prompt_for("where is something defined")
    assert "cite the range-read evidence ID" in prompt


def test_caller_enumeration_requires_one_claim_per_caller():
    prompt = _system_prompt_for("what calls the order service handler")
    assert "one CALL_EDGE claim per caller" in prompt
    assert "single-caller answer to a caller-enumeration question is incomplete" in prompt


def test_method_return_questions_route_to_method_name_resolution():
    prompt = _system_prompt_for("which default applies when orderType is null")
    assert '"method_name": "normalize"' in prompt
    assert "SOURCE_VALUE_FACT" in prompt
    assert "GUARDED_RETURN_LITERAL" in prompt


def test_history_questions_use_a_typed_commit_claim():
    prompt = _system_prompt_for("What changed in OrderServiceImpl.java recently?")
    assert "HISTORY: for recent-change questions" in prompt
    assert "target.commit_id" in prompt
    assert "COMMIT_HISTORY row" in prompt


def test_application_context_includes_allowed_capabilities():
    import asyncio as _asyncio
    from types import SimpleNamespace

    from investigation_agent_platform.application.investigation.reasoning import (
        ReasoningCoordinator,
    )

    profile = SimpleNamespace(
        domain="d",
        planes=[],
        data_classification=None,
        services=[],
        datasources=[],
        allowed_capabilities=["SEARCH_CODE", "GET_CODE"],
    )

    class FakeRepo:
        async def get_by_application_id(self, tenant_id, application_id):
            return profile

    coordinator = ReasoningCoordinator.__new__(ReasoningCoordinator)
    coordinator._profile_repo = FakeRepo()
    block = _asyncio.run(coordinator._application_context_block("t", "app"))
    assert block["allowed_capabilities"] == ["SEARCH_CODE", "GET_CODE"]
