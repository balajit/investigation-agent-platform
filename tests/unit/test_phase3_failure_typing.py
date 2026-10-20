# tests/unit/test_phase3_failure_typing.py
"""Phase 3.1/3.3: typed provider outcomes, replan alignment, auth block.

3.1: timeouts (after gateway retries), auth blocks, outages, and
unconfigured providers surface as distinct miss reasons — never collapsed
into "provider absent". Auth failure blocks the investigation loudly.
3.3: _should_force_replan matches the production call shape (history holds
completed enriched turns only; the proposal arrives via reason_data).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from investigation_agent_platform.api.dependencies import AppContext, set_app_context
from investigation_agent_platform.application.evidence.gateway import (
    _is_retryable_gateway_failure,
)
from investigation_agent_platform.application.worker.activities import (
    ExecuteActionInput,
    classify_provider_failure,
    execute_action_activity,
)
from investigation_agent_platform.application.worker.workflows import (
    _is_auth_blocked,
    _should_force_replan,
)
from investigation_agent_platform.domain.common.exceptions import (
    CredentialProviderError,
    ExecutionError,
    ProviderNotConfiguredError,
    ProviderTimeoutException,
    ProviderUnavailableException,
)


def _ctx_with(gateway: SimpleNamespace) -> tuple[AppContext, list]:
    ctx = AppContext()
    saved: list = []

    async def _save_batch(tenant_id, items, investigation_id):  # type: ignore[no-untyped-def]
        saved.extend(items)

    async def _save(tenant_id, item, investigation_id):  # type: ignore[no-untyped-def]
        saved.append(item)

    async def _count(tenant_id, investigation_id):  # type: ignore[no-untyped-def]
        return len(saved)

    service = SimpleNamespace(
        authorize=AsyncMock(return_value=None),
        execute=AsyncMock(return_value=None),
    )
    ctx.execute_action_service = lambda: service  # type: ignore[method-assign]
    ctx.action_repo = SimpleNamespace(count_by_investigation=_count)
    ctx.evidence_repo = SimpleNamespace(
        find_by_investigation_id=AsyncMock(return_value=[]),
        save_batch=_save_batch,
        save=_save,
    )
    ctx.profile_repo = SimpleNamespace(get_by_application_id=AsyncMock(return_value=None))
    ctx.evidence_gateway = gateway
    set_app_context(ctx)
    return ctx, saved


def _input(action: str, parameters: dict) -> ExecuteActionInput:
    return ExecuteActionInput(
        tenant_id="tenant-a",
        investigation_id=str(uuid4()),
        action=action,
        parameters=parameters,
    )


def _outcome(result) -> dict:  # type: ignore[no-untyped-def]
    return result.data.get("action_outcome") or {}


class TestClassifyProviderFailure:
    def test_timeout_types(self) -> None:
        assert classify_provider_failure(ProviderTimeoutException("t"), "x")[0] == "timeout"
        assert classify_provider_failure(TimeoutError("t"), "x")[0] == "timeout"

    def test_auth_blocks(self) -> None:
        reason, status, blocking = classify_provider_failure(
            CredentialProviderError("expired"), "x"
        )
        assert (reason, status, blocking) == ("auth_failed", "BLOCKED", True)

    def test_unconfigured_keeps_label(self) -> None:
        reason, status, blocking = classify_provider_failure(
            ProviderNotConfiguredError("missing"), "code_provider_not_configured"
        )
        assert (reason, status, blocking) == (
            "code_provider_not_configured",
            "DEGRADED",
            False,
        )

    def test_unavailable(self) -> None:
        assert classify_provider_failure(ProviderUnavailableException("down"), "x")[0] == (
            "provider_unavailable"
        )
        assert classify_provider_failure(ConnectionError("refused"), "x")[0] == (
            "provider_unavailable"
        )

    def test_message_sniffing_never_blocks(self) -> None:
        reason, _, blocking = classify_provider_failure(
            ExecutionError("request timed out after 30s"), "x"
        )
        assert reason == "timeout" and blocking is False
        reason, _, blocking = classify_provider_failure(
            ExecutionError("401 Unauthorized"), "x"
        )
        assert reason == "provider_unavailable" and blocking is False

    def test_generic_execution_error_is_internal(self) -> None:
        reason, status, _ = classify_provider_failure(ExecutionError("boom"), "x")
        assert (reason, status) == ("internal_error", "DEGRADED")

    def test_unknown_reraises(self) -> None:
        with pytest.raises(ValueError):
            classify_provider_failure(ValueError("bug"), "x")


class TestGatewayRetryPredicate:
    def test_builtin_timeout_retries(self) -> None:
        assert _is_retryable_gateway_failure(TimeoutError()) is True
        assert _is_retryable_gateway_failure(ProviderTimeoutException("t")) is True

    def test_non_retryable_does_not(self) -> None:
        assert _is_retryable_gateway_failure(ValueError("bug")) is False
        assert _is_retryable_gateway_failure(ProviderNotConfiguredError("m")) is False
        assert _is_retryable_gateway_failure(CredentialProviderError("m")) is False


class TestTypedHandlerOutcomes:
    @pytest.mark.asyncio
    async def test_timeout_is_not_absence(self) -> None:
        async def _search_code(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            raise ProviderTimeoutException("code index timed out")

        _, saved = _ctx_with(SimpleNamespace(search_code=_search_code))
        result = await execute_action_activity(_input("SEARCH_CODE", {"query": "price"}))
        assert result.data["items_count"] == 0
        assert saved == []
        assert _outcome(result)["miss_reason"] == "timeout"
        assert _outcome(result)["status"] == "DEGRADED"

    @pytest.mark.asyncio
    async def test_auth_blocks_loudly(self) -> None:
        async def _search_code(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            raise CredentialProviderError("token expired")

        _, saved = _ctx_with(SimpleNamespace(search_code=_search_code))
        result = await execute_action_activity(_input("SEARCH_CODE", {"query": "price"}))
        assert result.data["items_count"] == 0
        assert saved == []
        assert _outcome(result)["miss_reason"] == "auth_failed"
        assert _outcome(result)["status"] == "BLOCKED"

    @pytest.mark.asyncio
    async def test_unconfigured_still_degrades(self) -> None:
        async def _search_code(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            raise ProviderNotConfiguredError("no provider")

        _, _ = _ctx_with(SimpleNamespace(search_code=_search_code))
        result = await execute_action_activity(_input("SEARCH_CODE", {"query": "price"}))
        assert _outcome(result)["miss_reason"] == "code_provider_not_configured"
        assert _outcome(result)["status"] == "DEGRADED"

    @pytest.mark.asyncio
    async def test_trace_logic_timeout_not_symbol_miss(self) -> None:
        async def _find_symbol(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            raise ProviderTimeoutException("symbol index timed out")

        _, _ = _ctx_with(SimpleNamespace(find_symbol=_find_symbol))
        result = await execute_action_activity(
            _input("TRACE_LOGIC", {"symbol": "price"})
        )
        assert _outcome(result)["miss_reason"] == "timeout"

    @pytest.mark.asyncio
    async def test_get_code_unavailable(self) -> None:
        async def _get_source(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            raise ProviderUnavailableException("git host down")

        _, _ = _ctx_with(SimpleNamespace(get_source=_get_source))
        result = await execute_action_activity(
            _input("GET_CODE", {"file_path": "a.py", "start_line": 1, "end_line": 5})
        )
        assert _outcome(result)["miss_reason"] == "provider_unavailable"


class TestAuthBlockHelper:
    def test_blocked_auth(self) -> None:
        assert (
            _is_auth_blocked(
                {"action_outcome": {"status": "BLOCKED", "miss_reason": "auth_failed"}}
            )
            is True
        )

    def test_other_blocked_keeps_looping(self) -> None:
        assert (
            _is_auth_blocked(
                {
                    "action_outcome": {
                        "status": "BLOCKED",
                        "miss_reason": "authorization_denied",
                    }
                }
            )
            is False
        )

    def test_degraded_never_blocks(self) -> None:
        assert (
            _is_auth_blocked(
                {"action_outcome": {"status": "DEGRADED", "miss_reason": "timeout"}}
            )
            is False
        )

    def test_missing_outcome_never_blocks(self) -> None:
        assert _is_auth_blocked({}) is False
        assert _is_auth_blocked(None) is False


class TestReplanProductionShape:
    def test_two_enriched_empties_fire(self) -> None:
        history = [
            {"action": "SEARCH_CODE", "evidence_added": 0},
            {"action": "SEARCH_CODE", "evidence_added": 0},
        ]
        hit = _should_force_replan(
            "Show recent errors",
            {"next_action": "SEARCH_CODE", "action_parameters": {}},
            history,
            0,
        )
        assert hit is not None and hit["next_action"] == "SEARCH_LOGS"

    def test_single_prior_turn_does_not_fire(self) -> None:
        history = [{"action": "SEARCH_CODE", "evidence_added": 0}]
        assert (
            _should_force_replan(
                "Show recent errors",
                {"next_action": "SEARCH_CODE", "action_parameters": {}},
                history,
                0,
            )
            is None
        )


class _FakeIdempotencyStore:
    def __init__(self, cached=None):
        self.cached = cached
        self.completed: dict = {}

    async def reserve_or_get(self, tenant_id, key, request_hash, operation=None):  # type: ignore[no-untyped-def]
        if self.cached is not None:
            return self.cached, False
        return None, f"active-{key}"

    async def complete(self, tenant_id, key, response, operation=None):  # type: ignore[no-untyped-def]
        self.completed = response


class TestIdempotencyOutcomeFidelity:
    @pytest.mark.asyncio
    async def test_stored_response_matches_live_return(self) -> None:
        async def _search_code(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            raise ProviderTimeoutException("code index timed out")

        ctx, _ = _ctx_with(SimpleNamespace(search_code=_search_code))
        store = _FakeIdempotencyStore()
        ctx.idempotency_store = store  # type: ignore[attr-defined]
        result = await execute_action_activity(_input("SEARCH_CODE", {"query": "price"}))
        stored = store.completed["data"]
        live = result.data["action_outcome"]
        assert stored["action_outcome"] == live
        assert stored["action_outcome"]["miss_reason"] == "timeout"
        assert stored["action_outcome"]["status"] == "DEGRADED"

    @pytest.mark.asyncio
    async def test_modern_replay_returns_identical_outcome(self) -> None:
        outcome = {
            "status": "DEGRADED",
            "miss_reason": "timeout",
            "evidence_ids": [],
            "item_count": 0,
        }
        cached = {"success": True, "data": {"status": "EXECUTED", "items_count": 0, "action_outcome": outcome}}

        async def _never(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            raise AssertionError("replayed action must not re-execute")

        ctx, _ = _ctx_with(SimpleNamespace(search_code=_never))
        ctx.idempotency_store = _FakeIdempotencyStore(cached=cached)  # type: ignore[attr-defined]
        result = await execute_action_activity(_input("SEARCH_CODE", {"query": "price"}))
        assert result.data["action_outcome"] == outcome
        assert "idempotency_replay_legacy" not in result.data

    @pytest.mark.asyncio
    async def test_legacy_replay_tagged_not_invented(self) -> None:
        cached = {"success": True, "data": {"status": "EXECUTED", "items_count": 0}}

        async def _never(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            raise AssertionError("replayed action must not re-execute")

        ctx, _ = _ctx_with(SimpleNamespace(search_code=_never))
        ctx.idempotency_store = _FakeIdempotencyStore(cached=cached)  # type: ignore[attr-defined]
        result = await execute_action_activity(_input("SEARCH_CODE", {"query": "price"}))
        assert result.data.get("idempotency_replay_legacy") is True
        assert result.data.get("action_outcome") is None

    @pytest.mark.asyncio
    async def test_budget_replay_is_not_legacy(self) -> None:
        cached = {
            "success": True,
            "data": {"status": "BUDGET_EXCEEDED", "budget_exceeded": True, "items_count": 0},
        }

        async def _never(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            raise AssertionError("replayed action must not re-execute")

        ctx, _ = _ctx_with(SimpleNamespace(search_code=_never))
        ctx.idempotency_store = _FakeIdempotencyStore(cached=cached)  # type: ignore[attr-defined]
        result = await execute_action_activity(_input("SEARCH_CODE", {"query": "price"}))
        assert "idempotency_replay_legacy" not in result.data
        assert result.data.get("budget_exceeded") is True


class TestReasoningUnavailable:
    def _reason_ctx(self, decision):  # type: ignore[no-untyped-def]
        from datetime import UTC, datetime, timedelta

        from investigation_agent_platform.domain.investigation.models import (
            Investigation,
            InvestigationContext,
            InvestigationRequest,
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
        ctx = AppContext()
        ctx.investigation_repo = SimpleNamespace(
            get_by_id=AsyncMock(return_value=inv)
        )
        ctx.reasoning_coordinator = SimpleNamespace(  # type: ignore[attr-defined]
            reason=AsyncMock(return_value=decision)
        )
        set_app_context(ctx)
        return ctx

    @pytest.mark.asyncio
    async def test_outage_returns_typed_failure_not_default_action(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            ReasonInput,
            reason_activity,
        )

        decision = SimpleNamespace(
            metadata={"status": "REASONING_UNAVAILABLE", "reason": "llm gateway down"},
            reasoning_chain="REASONING_UNAVAILABLE",
        )
        self._reason_ctx(decision)
        result = await reason_activity(
            ReasonInput(tenant_id="tenant-a", investigation_id=str(uuid4()))
        )
        assert result.success is True
        assert result.data.get("reasoning_unavailable") is True
        assert result.data.get("reason") == "llm gateway down"
        assert "next_action" not in result.data

    @pytest.mark.asyncio
    async def test_chain_marker_also_detected(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            ReasonInput,
            reason_activity,
        )

        decision = SimpleNamespace(metadata={}, reasoning_chain="REASONING_UNAVAILABLE")
        self._reason_ctx(decision)
        result = await reason_activity(
            ReasonInput(tenant_id="tenant-a", investigation_id=str(uuid4()))
        )
        assert result.data.get("reasoning_unavailable") is True


class TestRateLimited:
    def test_classifier_maps_typed_throttle(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import (
            RateLimitException,
        )

        exc = RateLimitException("throttled", retry_after=30.0)
        reason, status, blocking = classify_provider_failure(exc, "x")
        assert (reason, status, blocking) == ("rate_limited", "DEGRADED", False)
        assert exc.retry_after == 30.0
        assert exc.details["retry_after"] == 30.0
        assert exc.error_code == "RATE_LIMITED"
        assert exc.retryable is True

    def test_classifier_sniffs_untyped_429(self) -> None:
        reason, status, blocking = classify_provider_failure(
            ExecutionError("429 Too Many Requests"), "x"
        )
        assert (reason, status, blocking) == ("rate_limited", "DEGRADED", False)

    def test_domain_defaults_without_hint(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import (
            RateLimitException,
        )

        exc = RateLimitException("throttled")
        assert exc.retry_after is None
        assert "retry_after" not in exc.details

    @pytest.mark.asyncio
    async def test_handler_records_code_and_hint(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import (
            RateLimitException,
        )

        async def _search_code(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            raise RateLimitException("index throttled", retry_after=45.0)

        _, saved = _ctx_with(SimpleNamespace(search_code=_search_code))
        result = await execute_action_activity(_input("SEARCH_CODE", {"query": "price"}))
        assert result.data["items_count"] == 0
        assert saved == []
        outcome = _outcome(result)
        assert outcome["miss_reason"] == "rate_limited"
        assert outcome["status"] == "DEGRADED"
        assert outcome["error_code"] == "RATE_LIMITED"

    def test_openai_translate_429_with_hint(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import (
            RateLimitException,
        )
        from investigation_agent_platform.infrastructure.reasoning.openai_adapter import (
            _translate_rate_limit,
        )

        raw = SimpleNamespace(status_code=429, headers={"Retry-After": "120"})
        out = _translate_rate_limit(raw, "openai")
        assert isinstance(out, RateLimitException)
        assert out.retry_after == 120.0
        assert "openai" in str(out)

    def test_openai_translate_ignores_non_429(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import (
            RateLimitException,
        )
        from investigation_agent_platform.infrastructure.reasoning.openai_adapter import (
            _translate_rate_limit,
        )

        raw = SimpleNamespace(status_code=503)
        assert _translate_rate_limit(raw, "openai") is raw
        typed = RateLimitException("t")
        assert _translate_rate_limit(typed, "openai") is typed

    def test_anthropic_translate_429(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import (
            RateLimitException,
        )
        from investigation_agent_platform.infrastructure.reasoning.anthropic_adapter import (
            _translate_rate_limit,
        )

        raw = SimpleNamespace(status_code=429, headers={})
        out = _translate_rate_limit(raw, "anthropic")
        assert isinstance(out, RateLimitException)
        assert out.retry_after is None

    @pytest.mark.asyncio
    async def test_coordinator_preserves_throttle_cause(self) -> None:
        from datetime import UTC, datetime, timedelta
        from unittest.mock import AsyncMock, MagicMock

        from investigation_agent_platform.application.investigation.reasoning import (
            ReasoningCoordinator,
        )
        from investigation_agent_platform.domain.common.exceptions import (
            RateLimitException,
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
        gateway = MagicMock()
        gateway.complete = AsyncMock(
            side_effect=RateLimitException("throttled", retry_after=20.0)
        )
        coord = ReasoningCoordinator(prompt_safety_policy=safety, llm_gateway=gateway)
        decision = await coord.reason("tenant-a", InvestigationState(investigation=inv))
        assert decision.metadata["status"] == "REASONING_UNAVAILABLE"
        assert "rate_limited" in decision.metadata["reason"]


def _retry_state(exc=None, attempt=1):
    return SimpleNamespace(
        attempt_number=attempt,
        outcome=SimpleNamespace(exception=lambda: exc),
    )


class TestRetryAfterWait:
    def test_hint_honored_up_to_cap(self) -> None:
        from investigation_agent_platform.application.evidence.gateway import (
            MAX_RETRY_AFTER_WAIT,
            _wait_with_retry_after,
        )
        from investigation_agent_platform.domain.common.exceptions import (
            RateLimitException,
        )

        assert MAX_RETRY_AFTER_WAIT == 15.0
        assert (
            _wait_with_retry_after(
                _retry_state(RateLimitException("t", retry_after=120.0))
            )
            == 15.0
        )
        assert (
            _wait_with_retry_after(_retry_state(RateLimitException("t", retry_after=5.0)))
            == 5.0
        )

    def test_no_hint_falls_back_to_exponential(self) -> None:
        from investigation_agent_platform.application.evidence.gateway import (
            _wait_with_retry_after,
        )
        from investigation_agent_platform.domain.common.exceptions import (
            ProviderTimeoutException,
        )

        assert _wait_with_retry_after(_retry_state(ProviderTimeoutException("t"))) == 2.0
        assert _wait_with_retry_after(_retry_state(None)) == 2.0

    def test_garbage_hint_ignored(self) -> None:
        from investigation_agent_platform.application.evidence.gateway import (
            _wait_with_retry_after,
        )
        from investigation_agent_platform.domain.common.exceptions import (
            RateLimitException,
        )

        exc = RateLimitException("t")
        exc.retry_after = "soon"
        assert _wait_with_retry_after(_retry_state(exc)) == 2.0

    def test_adapters_honor_hint(self) -> None:
        from investigation_agent_platform.domain.common.exceptions import (
            RateLimitException,
        )
        from investigation_agent_platform.infrastructure.reasoning.anthropic_adapter import (
            _wait_with_retry_after as anthropic_wait,
        )
        from investigation_agent_platform.infrastructure.reasoning.openai_adapter import (
            _wait_with_retry_after as openai_wait,
        )

        for wait in (anthropic_wait, openai_wait):
            assert wait(_retry_state(RateLimitException("t", retry_after=60.0))) == 15.0
            assert wait(_retry_state(ValueError("bug"))) == 2.0


class TestCallEdgePackets:
    @pytest.mark.asyncio
    async def test_edges_bank_with_file_line_provenance(self) -> None:
        from investigation_agent_platform.domain.evidence.models import EvidenceType

        async def _find_call_graph(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                items=[],
                total_count=2,
                provider_metadata={
                    "callers": [
                        {
                            "caller_symbol": "CheckoutController",
                            "callee_symbol": "reserve",
                            "location": {
                                "file_path": "src/CheckoutController.java",
                                "line_number": 88,
                                "revision": "abc123",
                                "revision_supported": True,
                            },
                        },
                        {
                            "caller_symbol": "Ghost",
                            "callee_symbol": "reserve",
                            "location": {},
                        },
                    ],
                    "callees": [],
                },
            )

        _, saved = _ctx_with(SimpleNamespace(find_call_graph=_find_call_graph))
        result = await execute_action_activity(
            _input("FIND_CALL_GRAPH", {"symbol": "reserve"})
        )
        # Summary + one edge packet (the location-less edge is skipped).
        assert result.data["items_count"] == 2
        edges = [
            e for e in saved if e.evidence_type == EvidenceType.CALL_GRAPH
            and e.attributes.get("caller_symbol") == "CheckoutController"
        ]
        assert len(edges) == 1
        loc = edges[0].provenance.source_location
        assert loc.file_path == "src/CheckoutController.java"
        assert loc.line_start == 88 and loc.line_end == 88
        assert loc.revision == "abc123"
        assert edges[0].attributes["discovery_only"] is True
        assert edges[0].attributes["callee_symbol"] == "reserve"


def _reason_harness(parsed_list, obs=None):
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
    from investigation_agent_platform.ports.observability.telemetry import LLMCallMetadata
    from investigation_agent_platform.ports.reasoning.llm_gateway import LLMGatewayResponse

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

    def _resp(parsed):
        return LLMGatewayResponse(
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

    gateway.complete = AsyncMock(side_effect=[_resp(p) for p in parsed_list])
    coord = ReasoningCoordinator(
        prompt_safety_policy=safety, llm_gateway=gateway, observability=obs
    )
    return coord, InvestigationState(investigation=inv), gateway


class TestInvalidSchemaRetry:
    @staticmethod
    def _valid():
        return {
            "observations": [],
            "conclusion_readiness": 0.0,
            "hypotheses_evaluated": ["pool fails"],
        }

    @staticmethod
    def _invalid():
        return {"observations": [], "conclusion_readiness": "high"}

    @pytest.mark.asyncio
    async def test_resample_recovers_valid_decision(self) -> None:
        coord, state, gateway = _reason_harness([self._invalid(), self._valid()])
        decision = await coord.reason("tenant-a", state)
        assert gateway.complete.call_count == 2
        assert list(decision.hypotheses_evaluated) == ["pool fails"]
        assert decision.metadata.get("status") != "REASONING_UNAVAILABLE"

    @pytest.mark.asyncio
    async def test_persistent_invalid_abstains_typed(self) -> None:
        coord, state, gateway = _reason_harness([self._invalid(), self._invalid()])
        decision = await coord.reason("tenant-a", state)
        assert gateway.complete.call_count == 2
        assert decision.metadata.get("status") == "REASONING_UNAVAILABLE"
        assert "invalid_llm_schema" in decision.metadata.get("reason", "")

    @pytest.mark.asyncio
    async def test_non_validation_error_fails_fast(self) -> None:
        coord, state, gateway = _reason_harness(
            [{"observations": [], "action": {"action_type": "NOPE"}}]
        )
        decision = await coord.reason("tenant-a", state)
        assert gateway.complete.call_count == 1
        assert decision.metadata.get("status") == "REASONING_UNAVAILABLE"


class TestClaimSanitize:
    """Live L3: claims[] items are extra=forbid — one invented key or an
    off-vocabulary enum failed the whole reason step twice (byte-identical
    resample) into terminal REASONING_UNAVAILABLE. Sanitizer degrades."""

    @pytest.mark.asyncio
    async def test_history_shaped_claim_validates_first_try(self) -> None:
        coord, state, gateway = _reason_harness(
            [
                {
                    "observations": [],
                    "conclusion_readiness": 0.0,
                    "claims": [
                        {
                            "statement": "OrderServiceImpl changed recently",
                            "claim_kind": "HISTORY",
                            "evidence_ids": None,
                            "commit": "abc123",
                            "revision": "r2",
                        },
                        {"statement": "   ", "claim_kind": "CAUSAL"},
                        "not-a-claim",
                    ],
                }
            ]
        )
        decision = await coord.reason("tenant-a", state)
        assert gateway.complete.call_count == 1
        assert decision.metadata.get("status") != "REASONING_UNAVAILABLE"
        assert len(decision.claims) == 1
        assert decision.claims[0].statement == "OrderServiceImpl changed recently"
        assert decision.claims[0].claim_kind.value == "CAUSAL"
        assert list(decision.claims[0].evidence_ids) == []

    @pytest.mark.asyncio
    async def test_invalid_fact_category_defaults_none(self) -> None:
        coord, state, gateway = _reason_harness(
            [
                {
                    "observations": [],
                    "claims": [
                        {
                            "statement": "handler routes orders",
                            "fact_category": "GIT_FACT",
                            "target": {
                                "symbol": "handle",
                                "line_start": 0,
                                "bogus_key": 1,
                            },
                        }
                    ],
                }
            ]
        )
        decision = await coord.reason("tenant-a", state)
        assert gateway.complete.call_count == 1
        assert decision.metadata.get("status") != "REASONING_UNAVAILABLE"
        assert decision.claims[0].fact_category is None
        assert decision.claims[0].target is not None
        assert decision.claims[0].target.line_start is None


class TestCallerChainTargets:
    """L2 Helper-drop: call-site chaining read only the first caller edge,
    so the second caller never got a citable range row."""

    @staticmethod
    def _edge(role, file, line):
        return {"role": role, "file": file, "line": line}

    def test_banks_every_caller_file_once(self):
        from investigation_agent_platform.application.worker.activities import (
            _caller_chain_targets,
        )

        edges = [
            self._edge("caller", "proxy/OrderServiceRestProxy.java", 11),
            self._edge("caller", "helper/OrderQualifyHelper.java", 12),
            self._edge("callee", "handler/OrderServiceHandler.java", 4),
        ]
        targets = _caller_chain_targets(edges)
        assert [(t["file"], t["line"]) for t in targets] == [
            ("proxy/OrderServiceRestProxy.java", 11),
            ("helper/OrderQualifyHelper.java", 12),
        ]

    def test_same_file_dedupes_and_malformed_skip(self):
        from investigation_agent_platform.application.worker.activities import (
            _caller_chain_targets,
        )

        edges = [
            self._edge("caller", "a.java", 3),
            self._edge("caller", "a.java", 9),
            self._edge("caller", "", 4),
            self._edge("caller", "b.java", "x"),
            "not-a-dict",
        ]
        assert [t["line"] for t in _caller_chain_targets(edges)] == [3]

    def test_limit_bounds_hub_symbols(self):
        from investigation_agent_platform.application.worker.activities import (
            _caller_chain_targets,
        )

        edges = [self._edge("caller", f"f{i}.java", i + 1) for i in range(10)]
        assert len(_caller_chain_targets(edges, limit=3)) == 3


class TestCallSiteChaining:
    @pytest.mark.asyncio
    async def test_caller_window_read_banked(self) -> None:
        calls = {}

        async def _find_call_graph(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                items=[],
                total_count=1,
                provider_metadata={
                    "callers": [
                        {
                            "caller_symbol": "CheckoutController",
                            "callee_symbol": "reserve",
                            "location": {"file_path": "Checkout.java", "line_number": 88},
                        }
                    ],
                    "callees": [
                        {
                            "caller_symbol": "reserve",
                            "callee_symbol": "inventory",
                            "location": {"file_path": "Reserve.java", "line_number": 10},
                        }
                    ],
                },
            )

        async def _get_source(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            calls["req"] = req
            return SimpleNamespace(
                evidence_id=uuid4(),
                evidence_type="SOURCE_CODE",
                provenance=SimpleNamespace(
                    source_location=SimpleNamespace(
                        file_path=req.file_path,
                        line_start=req.start_line,
                        line_end=req.end_line,
                        revision="abc",
                    )
                ),
                content_snippet="x",
            )

        _, saved = _ctx_with(
            SimpleNamespace(find_call_graph=_find_call_graph, get_source=_get_source)
        )
        result = await execute_action_activity(
            _input("FIND_CALL_GRAPH", {"symbol": "reserve"})
        )
        # Summary + 2 edges + 1 chained caller window.
        assert result.data["items_count"] == 4
        assert calls["req"].file_path == "Checkout.java"
        assert calls["req"].start_line == 48
        assert calls["req"].end_line == 128

    @pytest.mark.asyncio
    async def test_no_location_no_extra_read(self) -> None:
        calls = []

        async def _find_call_graph(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                items=[],
                total_count=1,
                provider_metadata={
                    "callers": [{"caller_symbol": "A", "callee_symbol": "b", "location": {}}],
                    "callees": [{"caller_symbol": "b", "callee_symbol": "c", "location": {}}],
                },
            )

        async def _get_source(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            calls.append(1)
            raise AssertionError("no located edge: must not read")

        _, _ = _ctx_with(
            SimpleNamespace(find_call_graph=_find_call_graph, get_source=_get_source)
        )
        result = await execute_action_activity(
            _input("FIND_CALL_GRAPH", {"symbol": "reserve"})
        )
        assert result.data["items_count"] == 1
        assert calls == []
