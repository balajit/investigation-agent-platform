# tests/unit/test_phase2_remaining.py
"""Phase 2 leftover items: per-kind claim binding, absence producer + gate."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from investigation_agent_platform.api.dependencies import AppContext, set_app_context
from investigation_agent_platform.application.worker.activities import (
    ExecuteActionInput,
    execute_action_activity,
)


def _ctx_with(gateway, *, evidence_rows=None, hyp_repo=None):
    ctx = AppContext()
    saved = []

    async def _save_batch(tenant_id, items, investigation_id):  # type: ignore[no-untyped-def]
        saved.extend(items)

    async def _save(tenant_id, item, investigation_id):  # type: ignore[no-untyped-def]
        saved.append(item)

    async def _find(tenant_id, investigation_id):  # type: ignore[no-untyped-def]
        return list(evidence_rows or [])

    async def _count(tenant_id, investigation_id):  # type: ignore[no-untyped-def]
        return 0

    service = SimpleNamespace(
        authorize=AsyncMock(return_value=None),
        execute=AsyncMock(return_value=None),
    )
    ctx.execute_action_service = lambda: service  # type: ignore[method-assign]
    ctx.action_repo = SimpleNamespace(count_by_investigation=_count)
    ctx.evidence_repo = SimpleNamespace(
        find_by_investigation_id=_find, save_batch=_save_batch, save=_save
    )
    ctx.profile_repo = SimpleNamespace(get_by_application_id=AsyncMock(return_value=None))
    ctx.hypothesis_repo = hyp_repo or SimpleNamespace(
        find_by_investigation_id=AsyncMock(return_value=[]),
        save=AsyncMock(),
    )
    ctx.evidence_gateway = gateway
    set_app_context(ctx)
    return ctx, saved


def _input(action, parameters):
    return ExecuteActionInput(
        tenant_id="tenant-a",
        investigation_id=str(uuid4()),
        action=action,
        parameters=parameters,
    )


class TestClaimKeying:
    @pytest.mark.asyncio
    async def test_same_statement_two_kinds_keep_both_bindings(self) -> None:
        e1, e2 = uuid4(), uuid4()
        rows = [SimpleNamespace(evidence_id=e1), SimpleNamespace(evidence_id=e2)]
        saved_hyps = []

        async def _save_hyp(tenant_id, hyp, investigation_id):  # type: ignore[no-untyped-def]
            saved_hyps.append(hyp)

        hyp_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[]), save=_save_hyp
        )
        _ctx_with(SimpleNamespace(), evidence_rows=rows, hyp_repo=hyp_repo)
        stmt = "price is computed in OfferSvc"
        action_input = _input(
            "FORMULATE_HYPOTHESIS", {"supporting_evidence_ids": [str(e1), str(e2)]}
        )
        action_input.hypotheses = [stmt]
        action_input.claims = [
            {
                "statement": stmt,
                "claim_kind": "DEFINITION",
                "evidence_ids": [str(e1)],
                "target": {"symbol": "price"},
            },
            {
                "statement": stmt,
                "claim_kind": "CALL_EDGE",
                "evidence_ids": [str(e2)],
                "target": {"symbol": "price", "counterpart": "checkout"},
            },
        ]
        await execute_action_activity(action_input)
        assert len(saved_hyps) == 1
        kinds = sorted(a.claim_kind.value for a in saved_hyps[0].assessments)
        assert kinds == ["CALL_EDGE", "DEFINITION"]
        by_kind = {a.claim_kind.value: a.evidence_id for a in saved_hyps[0].assessments}
        assert by_kind == {"DEFINITION": e1, "CALL_EDGE": e2}
        # Typed claims replace the batch list — never merged with it.
        assert sorted(saved_hyps[0].supporting_evidence_ids) == sorted([e1, e2])

    @pytest.mark.asyncio
    async def test_value_claim_semantics_round_trip_into_assessment(self) -> None:
        evidence_id = uuid4()
        saved_hyps = []

        async def _save_hyp(tenant_id, hyp, investigation_id):  # type: ignore[no-untyped-def]
            saved_hyps.append(hyp)

        hyp_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[]), save=_save_hyp
        )
        _ctx_with(
            SimpleNamespace(),
            evidence_rows=[SimpleNamespace(evidence_id=evidence_id)],
            hyp_repo=hyp_repo,
        )
        statement = "When orderType is null, normalize returns STANDARD"
        action_input = _input("FORMULATE_HYPOTHESIS", {})
        action_input.hypotheses = [statement]
        action_input.claims = [
            {
                "statement": statement,
                "claim_kind": "DEFINITION",
                "evidence_ids": [str(evidence_id)],
                "target": {
                    "symbol": "LegacyCodeMapper.normalize",
                    "fact_category": "SOURCE_VALUE_FACT",
                    "value_semantics": "GUARDED_RETURN_LITERAL",
                    "claimed_value": "STANDARD",
                },
                "fact_category": "SOURCE_VALUE_FACT",
                "value_semantics": "GUARDED_RETURN_LITERAL",
                "claimed_value": "STANDARD",
            }
        ]
        await execute_action_activity(action_input)
        assessment = saved_hyps[0].assessments[0]
        assert assessment.value_semantics == "GUARDED_RETURN_LITERAL"
        assert assessment.claimed_value == "STANDARD"
        assert assessment.fact_category.value == "SOURCE_VALUE_FACT"

    @pytest.mark.asyncio
    async def test_same_statement_multiple_callers_keep_distinct_claim_bindings(self) -> None:
        e1, e2 = uuid4(), uuid4()
        saved_hyps = []

        async def _save_hyp(tenant_id, hyp, investigation_id):  # type: ignore[no-untyped-def]
            saved_hyps.append(hyp)

        hyp_repo = SimpleNamespace(
            find_by_investigation_id=AsyncMock(return_value=[]), save=_save_hyp
        )
        _ctx_with(
            SimpleNamespace(),
            evidence_rows=[SimpleNamespace(evidence_id=e1), SimpleNamespace(evidence_id=e2)],
            hyp_repo=hyp_repo,
        )
        statement = "Two callers invoke OrderServiceHandler.handle"
        action_input = _input("FORMULATE_HYPOTHESIS", {})
        action_input.hypotheses = [statement]
        action_input.claims = [
            {
                "statement": statement,
                "claim_kind": "CALL_EDGE",
                "evidence_ids": [str(e1)],
                "target": {"symbol": "OrderServiceHandler.handle", "counterpart": "OrderQualifyHelper"},
            },
            {
                "statement": statement,
                "claim_kind": "CALL_EDGE",
                "evidence_ids": [str(e2)],
                "target": {"symbol": "OrderServiceHandler.handle", "counterpart": "OrderServiceRestProxy"},
            },
        ]
        await execute_action_activity(action_input)
        assessments = saved_hyps[0].assessments
        assert len(assessments) == 2
        assert {assessment.claim_target.counterpart for assessment in assessments} == {
            "OrderQualifyHelper",
            "OrderServiceRestProxy",
        }
        assert {assessment.evidence_id for assessment in assessments} == {e1, e2}


class TestAbsenceProducer:
    @pytest.mark.asyncio
    async def test_empty_search_banks_scoped_proof(self) -> None:
        async def _search_code(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            return SimpleNamespace(items=[], total_count=0, provider_metadata={})

        _, saved = _ctx_with(SimpleNamespace(search_code=_search_code))
        result = await execute_action_activity(
            _input("SEARCH_CODE", {"query": "nope", "revision": "abc123"})
        )
        assert result.data["items_count"] == 0
        assert len(saved) == 1
        row = saved[0]
        assert row.attributes["match_count"] == 0
        assert "nope" in row.attributes["search_scope"]
        assert "abc123" in row.attributes["search_scope"]
        assert row.attributes["discovery_only"] is True

    @pytest.mark.asyncio
    async def test_repeat_search_replays_without_rebanking(self) -> None:
        calls = []

        async def _search_code(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            calls.append(1)
            return SimpleNamespace(items=[], total_count=0, provider_metadata={})

        _, saved = _ctx_with(SimpleNamespace(search_code=_search_code))
        inv_id = str(uuid4())
        first = _input("SEARCH_CODE", {"query": "nope", "revision": "abc123"})
        first.investigation_id = inv_id
        second = _input("SEARCH_CODE", {"query": "nope", "revision": "abc123"})
        second.investigation_id = inv_id
        first_result = await execute_action_activity(first)
        second_result = await execute_action_activity(second)
        # Deterministic action_id (F-057) replays the identical repeat:
        # single provider call, single banked row, identical outcomes.
        assert len(calls) == 1
        assert len(saved) == 1
        assert first_result.data["items_count"] == 0
        assert second_result.data["items_count"] == 0
        assert second_result.data["action_outcome"] == first_result.data["action_outcome"]

    def test_absence_fingerprint_deterministic_per_scope(self) -> None:
        from investigation_agent_platform.application.worker.activities import (
            _absence_proof_row,
        )
        from investigation_agent_platform.domain.evidence.models import EvidenceType

        iid = uuid4()
        kwargs: dict = {
            "tenant_id": "tenant-a",
            "investigation_id": iid,
            "evidence_type": EvidenceType.SOURCE_CODE,
            "provider": "code-intelligence",
            "operation": "search_code",
        }
        row_a = _absence_proof_row(**kwargs, scope="repo@rev:/:query=nope")
        row_b = _absence_proof_row(**kwargs, scope="repo@rev:/:query=nope")
        row_c = _absence_proof_row(**kwargs, scope="repo@rev:/:query=other")
        assert row_a.fingerprint == row_b.fingerprint
        assert row_a.fingerprint != row_c.fingerprint
        assert row_a.attributes["match_count"] == 0

    @pytest.mark.asyncio
    async def test_empty_state_query_banks_proof(self) -> None:
        from investigation_agent_platform.domain.evidence.models import EvidenceType

        async def _get_state(tenant_id, investigation_id, application_id, req):  # type: ignore[no-untyped-def]
            return SimpleNamespace(items=[], total_count=0, provider_metadata={})

        _, saved = _ctx_with(SimpleNamespace(get_application_state=_get_state))
        result = await execute_action_activity(
            _input(
                "QUERY_STATE",
                {"environment": "production", "template_id": "t", "parameters": {}},
            )
        )
        assert result.data["items_count"] == 0
        assert len(saved) == 1
        assert saved[0].evidence_type == EvidenceType.DATABASE_STATE
        assert saved[0].attributes["match_count"] == 0


class TestAbsenceGate:
    def _proof_row(self, scope="audit_log/order-9"):
        from investigation_agent_platform.domain.evidence.models import (
            EvidenceType,
        )

        return SimpleNamespace(
            evidence_type=EvidenceType.DATABASE_STATE,
            content_snippet="0 rows",
            attributes={"search_scope": scope, "match_count": 0},
        )

    def test_scoped_proof_verifies(self) -> None:
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )
        from investigation_agent_platform.domain.hypothesis.models import ClaimTarget

        ok, _ = VerificationEngine.absence_proof(
            [self._proof_row()], ClaimTarget(scope="audit_log/order-9")
        )
        assert ok is True

    def test_unscoped_claim_fails(self) -> None:
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )
        from investigation_agent_platform.domain.hypothesis.models import ClaimTarget

        ok, reason = VerificationEngine.absence_proof([self._proof_row()], ClaimTarget())
        assert ok is False
        assert "scope" in reason

    def test_empty_search_alone_fails(self) -> None:
        from investigation_agent_platform.application.investigation.verification import (
            VerificationEngine,
        )
        from investigation_agent_platform.domain.hypothesis.models import ClaimTarget

        ok, _ = VerificationEngine.absence_proof([], ClaimTarget(scope="audit_log/order-9"))
        assert ok is False


class TestClaimCoverage:
    def _harness(self, parsed, obs=None):
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
        coord = ReasoningCoordinator(
            prompt_safety_policy=safety, llm_gateway=gateway, observability=obs
        )
        return coord, InvestigationState(investigation=inv)

    @pytest.mark.asyncio
    async def test_unclaimed_statement_becomes_explicit_causal(self) -> None:
        coord, state = self._harness(
            {
                "observations": [],
                "conclusion_readiness": 0.0,
                "hypotheses_evaluated": ["pool fails", "disk fails"],
                "claims": [
                    {
                        "statement": "pool fails",
                        "claim_kind": "BEHAVIOR",
                        "evidence_ids": [],
                        "target": None,
                    }
                ],
            }
        )
        decision = await coord.reason("tenant-a", state)
        by_stmt = {c.statement: c for c in decision.claims}
        assert by_stmt["pool fails"].claim_kind.value == "BEHAVIOR"
        auto = by_stmt["disk fails"]
        assert auto.claim_kind.value == "CAUSAL"
        assert list(auto.evidence_ids) == []

    @pytest.mark.asyncio
    async def test_fully_typed_decision_untouched(self) -> None:
        coord, state = self._harness(
            {
                "observations": [],
                "conclusion_readiness": 0.0,
                "hypotheses_evaluated": ["pool fails"],
                "claims": [
                    {
                        "statement": "pool fails",
                        "claim_kind": "BEHAVIOR",
                        "evidence_ids": [],
                        "target": None,
                    }
                ],
            }
        )
        decision = await coord.reason("tenant-a", state)
        assert len(decision.claims) == 1

    @pytest.mark.asyncio
    async def test_compliance_split_emitted(self) -> None:
        calls = []

        class _Obs:
            def record_claim(self, context, source):
                calls.append(source)

        coord, state = self._harness(
            {
                "observations": [],
                "conclusion_readiness": 0.0,
                "hypotheses_evaluated": ["a", "b"],
                "claims": [
                    {
                        "statement": "a",
                        "claim_kind": "DEFINITION",
                        "evidence_ids": [],
                        "target": None,
                    }
                ],
            },
            obs=_Obs(),
        )
        await coord.reason("tenant-a", state)
        assert sorted(calls) == ["auto", "model"]
