# tests/unit/test_llm_response_format_mode.py
"""Pin the LLM response-format contract (B+F: non-strict json_schema + Pydantic boundary).

Background: ``InvestigationDecision`` carries open maps (``dict[str, Any]``
parameters/metadata) that provider strict mode cannot represent — strict
rejects the schema while non-strict accepts it byte-identical (verified live
against Azure). The adapter therefore sends non-strict ``json_schema`` first
and ``InvestigationDecision.model_validate`` (via
``ReasoningCoordinator._validate_llm_decision``) remains the hard boundary.

These tests pin: (1) the wire mode is non-strict json_schema with the full
schema, (2) the json_object fallback still engages when a provider rejects
json_schema outright, (3) a representative provider payload — including the
open maps strict mode cannot express — converts back to the Pydantic object
with parameters preserved.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic import SecretStr

from investigation_agent_platform.application.investigation.reasoning import (
    ReasoningCoordinator,
)
from investigation_agent_platform.infrastructure.configuration.config import LLMConfig
from investigation_agent_platform.infrastructure.reasoning.openai_adapter import (
    OpenAIGateway,
)
from investigation_agent_platform.ports.reasoning.llm_gateway import LLMGatewayRequest
from investigation_agent_platform.ports.reasoning.reasoner import InvestigationDecision


def _gateway() -> OpenAIGateway:
    return OpenAIGateway(
        LLMConfig(api_key=SecretStr("sk-test"), model_name="gpt-4o-mini", provider="openai")
    )


def _response(content: str) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content=content))]
    mock_resp.usage = MagicMock(prompt_tokens=10, completion_tokens=20)
    return mock_resp


class TestNonStrictJsonSchema:
    @pytest.mark.asyncio
    async def test_first_call_is_non_strict_json_schema_with_full_schema(self) -> None:
        """The full decision schema goes out as non-strict json_schema; no fallback."""
        import json as _json

        schema = InvestigationDecision.model_json_schema()
        calls: list[dict] = []

        async def capture(client: object, kwargs: dict) -> object:
            calls.append(kwargs)
            return _response('{"observations": ["o"]}')

        gw = _gateway()
        gw._client = MagicMock()
        gw._call_with_retry = capture  # type: ignore[method-assign]
        req = LLMGatewayRequest(
            prompt="hi", system_prompt="sys", response_schema=schema, temperature=0.1
        )
        resp = await gw.complete("t1", req)

        assert len(calls) == 1  # accepted first try — no fallback round-trip
        rf = calls[0]["response_format"]
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["name"] == "investigation_decision"
        assert rf["json_schema"]["strict"] is False
        assert rf["json_schema"]["schema"] == schema
        # Open maps survive the wire schema (strict mode cannot express these).
        defs = rf["json_schema"]["schema"].get("$defs", {})
        assert "parameters" in defs.get("InvestigationAction", {}).get("properties", {})
        assert resp.parsed == {"observations": ["o"]}

    @pytest.mark.asyncio
    async def test_json_object_fallback_still_engages_on_rejection(self) -> None:
        """Providers rejecting json_schema outright still get the json_object retry."""
        import json as _json

        calls: list[dict] = []

        async def reject_then_accept(client: object, kwargs: dict) -> object:
            calls.append({"response_format": _json.loads(_json.dumps(kwargs["response_format"]))})
            if len(calls) == 1:
                raise RuntimeError("Invalid response_format: json_schema is not supported")
            return _response('{"observations": ["o"]}')

        gw = _gateway()
        gw._client = MagicMock()
        gw._call_with_retry = reject_then_accept  # type: ignore[method-assign]
        req = LLMGatewayRequest(
            prompt="hi", system_prompt="sys", response_schema={"type": "object"}
        )
        resp = await gw.complete("t1", req)

        assert len(calls) == 2
        assert calls[0]["response_format"]["type"] == "json_schema"
        assert calls[1]["response_format"] == {"type": "json_object"}
        assert resp.parsed == {"observations": ["o"]}


class TestDecisionConversion:
    def test_open_maps_round_trip_to_pydantic(self) -> None:
        """A provider payload with populated open maps validates with params kept."""
        coord = ReasoningCoordinator(prompt_safety_policy=MagicMock())
        payload = {
            "observations": ["order handler called submit"],
            "information_gaps": [],
            "hypotheses_evaluated": ["proxy calls handler"],
            "claims": [],
            "contradictions_found": [],
            "conclusion_readiness": 0.4,
            "reasoning_chain": "r",
            "metadata": {"route": "fallback", "attempt": 2},
            "action": {
                "action_type": "SEARCH_LOGS",
                "parameters": {"resource": "orders-svc", "query": "handle"},
            },
            "defer_gathering": None,
            "disposition": None,
            # Advisory extra the model emits in free-form mode — dropped, not fatal.
            "next_action": "SEARCH_LOGS",
        }
        decision = coord._validate_llm_decision(payload, "t1", uuid4())
        assert isinstance(decision, InvestigationDecision)
        assert decision.action is not None
        assert decision.action.parameters["resource"] == "orders-svc"
        assert decision.action.parameters["query"] == "handle"
        assert decision.metadata["route"] == "fallback"
        assert decision.observations == ["order handler called submit"]

    def test_minimal_payload_gets_defaults(self) -> None:
        """The boundary never requires the model to fill every key."""
        coord = ReasoningCoordinator(prompt_safety_policy=MagicMock())
        decision = coord._validate_llm_decision({}, "t1", uuid4())
        assert isinstance(decision, InvestigationDecision)
        assert decision.action is None
        assert decision.metadata == {}
        assert decision.conclusion_readiness == 0.0
